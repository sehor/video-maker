import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import AttemptStatus, GenerationJob, JobStatus

FORBIDDEN_PUBLIC_TERMS = (
    "provider",
    "workflow",
    "gpu",
    "model_hash",
    "mock_mode",
)


def _create_queued_job(client: TestClient) -> dict:
    project = client.post("/v1/projects", json={"name": "public-contract"}).json()
    shot = client.post(
        f"/v1/projects/{project['id']}/shots",
        json={
            "title": "contract",
            "prompt": "public contract",
            "duration_seconds": 1,
            "aspect_ratio": "16:9",
        },
    ).json()
    grant = client.post(
        "/v1/wallet/test-grants",
        json={
            "tier": "FAST",
            "amount_ms": 1_000,
            "idempotency_key": f"contract-grant:{uuid.uuid4()}",
            "reason": "contract test",
        },
    )
    assert grant.status_code == 201
    quote = client.post(
        "/v1/quotes",
        json={"shot_id": shot["id"], "tier": "FAST", "resolution": "720P"},
    ).json()
    response = client.post(
        "/v1/generations",
        headers={"x-test-generation-modes": "failure"},
        json={"shot_id": shot["id"], "quote_id": quote["id"]},
    )
    assert response.status_code == 202
    return response.json()


def test_public_openapi_excludes_internal_and_admin_contracts() -> None:
    schema = app.openapi()
    serialized = json.dumps(schema).lower()

    assert "/v1/admin/" not in serialized
    assert "/v1/provider-webhooks/" not in serialized
    assert "/v1/wallet/test-grants" not in serialized
    for term in FORBIDDEN_PUBLIC_TERMS:
        assert term not in serialized

    generation_properties = schema["components"]["schemas"]["GenerationCreate"][
        "properties"
    ]
    assert set(generation_properties) == {"shot_id", "quote_id"}
    batch_item_properties = schema["components"]["schemas"]["BatchItemCreate"][
        "properties"
    ]
    assert set(batch_item_properties) == {"quote_id"}


def test_public_requests_cannot_select_internal_execution_options(
    raw_client: TestClient,
) -> None:
    payload = {
        "shot_id": str(uuid.uuid4()),
        "quote_id": str(uuid.uuid4()),
    }
    for field, value in {
        "provider_code": "runpod",
        "provider_url": "https://example.invalid",
        "workflow": "arbitrary",
        "model": "arbitrary",
        "mock_mode": "success",
    }.items():
        response = raw_client.post("/v1/generations", json={**payload, field: value})
        assert response.status_code == 422


def test_public_json_hides_internal_diagnostics_but_admin_can_read_them(
    raw_client: TestClient,
) -> None:
    created = _create_queued_job(raw_client)
    job_id = uuid.UUID(created["id"])
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        job.status = JobStatus.FAILED_FINAL
        job.failure_code = "MODEL_LOAD_FAILED"
        job.error_message = "Provider GPU model workflow failed"
        attempt = job.attempts[0]
        attempt.status = AttemptStatus.FAILED_FINAL
        attempt.failure_code = "WORKFLOW_FAILED"
        attempt.provider_job_id = "provider-job-secret"
        attempt.worker_version = "worker:v1"
        attempt.workflow_hash = "a" * 64
        attempt.model_hashes_json = {"model": "b" * 64}
        attempt.gpu_type = "H100"
        attempt.cost_minor = 123
        attempt.cost_currency = "USD"
        attempt.cost_source = "ACTUAL"
        db.commit()

    public_response = raw_client.get(f"/v1/generations/{job_id}")
    assert public_response.status_code == 200
    public_body = public_response.json()
    assert public_body["failure_code"] == "GENERATION_FAILED"
    assert public_body["error_message"] == "生成失败，请稍后重试"
    public_serialized = json.dumps(public_body).lower()
    for term in FORBIDDEN_PUBLIC_TERMS:
        assert term not in public_serialized
    assert "h100" not in public_serialized
    assert "usd" not in public_serialized

    forbidden = raw_client.get(f"/v1/admin/generations/{job_id}")
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "ADMIN_REQUIRED"

    admin_response = raw_client.get(
        f"/v1/admin/generations/{job_id}",
        headers={"x-test-user": "admin-user"},
    )
    assert admin_response.status_code == 200
    admin_body = admin_response.json()
    assert admin_body["mock_mode"] == "failure"
    admin_attempt = admin_body["attempts"][0]
    assert admin_attempt["provider_job_id"] == "provider-job-secret"
    assert admin_attempt["workflow_hash"] == "a" * 64
    assert admin_attempt["model_hashes_json"] == {"model": "b" * 64}
    assert admin_attempt["gpu_type"] == "H100"
    assert admin_attempt["cost_minor"] == 123


pytestmark = pytest.mark.database
