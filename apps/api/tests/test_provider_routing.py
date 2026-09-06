import asyncio
import base64
import io
import json
import uuid
from dataclasses import FrozenInstanceError

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import GenerationAttempt, GenerationBatch, GenerationJob, ProjectAsset
from app.provider_execution import GenerationExecutionService
from app.provider_registry import ProviderNotConfiguredError
from app.routing import (
    MOCK_ROUTE_VERSION,
    SIMULATED_RUNPOD_ROUTE,
    SIMULATED_RUNPOD_ROUTE_VERSION,
    get_callback_claim_issuer,
    get_provider_registry,
    get_route_registry,
)
from app.simulators import DeterministicRunPodSimulator
from app.storage import LocalObjectStorage
from tests.test_projects_permissions import create_project


def _claim_payload(token: str) -> dict[str, object]:
    encoded = token.split(".", 1)[0]
    encoded += "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded))


def _quoted_shot(
    client: TestClient,
    *,
    with_reference: bool,
) -> tuple[dict, dict, dict | None]:
    project = create_project(client)
    shot_response = client.post(
        f"/v1/projects/{project['id']}/shots",
        json={
            "title": "Provider route contract",
            "prompt": "A paper boat crosses a calm lake at sunrise.",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
        },
    )
    assert shot_response.status_code == 201
    shot = shot_response.json()
    asset = None
    if with_reference:
        upload = client.post(
            f"/v1/projects/{project['id']}/assets",
            files={
                "file": (
                    "reference.png",
                    io.BytesIO(b"\x89PNG\r\n\x1a\nroute-reference"),
                    "image/png",
                )
            },
        )
        assert upload.status_code == 201
        asset = upload.json()
        attached = client.post(
            f"/v1/shots/{shot['id']}/references",
            json={"asset_id": asset["id"], "reference_role": "FIRST_FRAME"},
        )
        assert attached.status_code == 201
    grant = client.post(
        "/v1/wallet/test-grants",
        json={
            "tier": "FAST",
            "amount_ms": 10_000,
            "idempotency_key": f"route-grant:{uuid.uuid4()}",
            "reason": "provider route test",
        },
    )
    assert grant.status_code == 201
    quote = client.post(
        "/v1/quotes",
        json={"shot_id": shot["id"], "tier": "FAST", "resolution": "720P"},
    )
    assert quote.status_code == 201
    return shot, quote.json(), asset


def test_provider_registry_is_an_allowlist() -> None:
    providers = get_provider_registry()
    assert providers.codes == {"mock", "runpod-simulator"}
    assert not get_settings().runpod_provider_enabled
    with pytest.raises(ProviderNotConfiguredError):
        providers.get("runpod-or-user-supplied-url")


def test_real_runpod_cannot_be_enabled_without_an_adapter() -> None:
    with pytest.raises(ValidationError, match="RUNPOD_PROVIDER_ENABLED"):
        Settings(runpod_provider_enabled=True)


def test_route_versions_are_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        SIMULATED_RUNPOD_ROUTE.workflow_id = "user-workflow"  # type: ignore[misc]


def test_simulated_route_submits_bound_claim_only_worker_request(
    raw_client: TestClient,
) -> None:
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
    routes.set_enabled(SIMULATED_RUNPOD_ROUTE_VERSION, True)
    try:
        shot, quote, asset = _quoted_shot(raw_client, with_reference=True)
        assert asset is not None
        created = raw_client.post(
            "/v1/generations",
            json={
                "shot_id": shot["id"],
                "quote_id": quote["id"],
            },
        )
        assert created.status_code == 202
        job_id = uuid.UUID(created.json()["id"])
        settings = get_settings()
        executor = GenerationExecutionService(
            LocalObjectStorage(
                settings.storage_root,
                settings.storage_claim_secret.get_secret_value().encode(),
            )
        )
        asyncio.run(executor.execute(job_id))

        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            attempt = db.scalar(
                select(GenerationAttempt).where(GenerationAttempt.job_id == job_id)
            )
            stored_asset = db.get(ProjectAsset, uuid.UUID(asset["id"]))
            assert job is not None and attempt is not None
            assert stored_asset is not None
            assert job.status.value in {"SUBMITTED", "RUNNING"}
            assert attempt.provider_code == "runpod-simulator"
            assert attempt.provider_job_id is not None
            provider_job_id = attempt.provider_job_id
            asset_key = stored_asset.object_key

        provider = get_provider_registry().get("runpod-simulator")
        assert isinstance(provider, DeterministicRunPodSimulator)
        request = provider.submission(provider_job_id)
        assert (request.job_id, request.attempt_id) == (job_id, attempt.id)
        assert request.workflow_id == "fast_wan_i2v_720_v1"
        assert request.resolution == "720p"
        assert request.input_claim is not None
        input_payload = _claim_payload(request.input_claim)
        output_payload = _claim_payload(request.output_claim)
        assert input_payload["op"] == "READ"
        assert input_payload["key"] == asset_key
        assert output_payload["op"] == "WRITE"
        assert str(output_payload["key"]).startswith(f"provider-outputs/{job_id}/")
        get_callback_claim_issuer().verify(
            request.callback_claim,
            job_id=job_id,
            attempt_id=attempt.id,
            route=SIMULATED_RUNPOD_ROUTE,
        )
    finally:
        routes.activate(previous)


def test_route_kill_switch_rejects_new_jobs_immediately(raw_client: TestClient) -> None:
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(MOCK_ROUTE_VERSION)
    routes.set_enabled(MOCK_ROUTE_VERSION, False)
    try:
        shot, quote, _ = _quoted_shot(raw_client, with_reference=False)
        rejected = raw_client.post(
            "/v1/generations",
            json={
                "shot_id": shot["id"],
                "quote_id": quote["id"],
            },
        )
        assert rejected.status_code == 503
        assert rejected.json()["error"]["code"] == "ROUTE_DISABLED"
        batch_rejected = raw_client.post(
            "/v1/batches",
            json={"items": [{"quote_id": quote["id"]}]},
        )
        assert batch_rejected.status_code == 503
        assert batch_rejected.json()["error"]["code"] == "ROUTE_DISABLED"
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
            assert db.scalar(select(func.count()).select_from(GenerationBatch)) == 0
    finally:
        routes.set_enabled(MOCK_ROUTE_VERSION, True)
        routes.activate(previous)


@pytest.mark.parametrize(
    "injected",
    [
        {"url": "https://attacker.invalid/input.png"},
        {"workflow": {"nodes": []}},
        {"workflow_id": "attacker-workflow"},
        {"workflow_version": "attacker-v2"},
        {"model": "attacker/model"},
        {"model_version": "latest"},
    ],
)
def test_generation_rejects_execution_configuration_injection(
    raw_client: TestClient,
    injected: dict[str, object],
) -> None:
    payload = {
        "shot_id": str(uuid.uuid4()),
        "quote_id": str(uuid.uuid4()),
        **injected,
    }
    response = raw_client.post("/v1/generations", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
