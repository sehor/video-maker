import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.models import GenerationAttempt, GenerationOutput
from app.provider_execution import GenerationExecutionService, ProviderExecutionStep
from app.storage import LocalObjectStorage
from tests.test_projects_permissions import create_project


def create_shot(client: TestClient) -> dict:
    project = create_project(client)
    response = client.post(
        f"/v1/projects/{project['id']}/shots",
        json={
            "title": "生成镜头",
            "prompt": "海边日落",
            "duration_seconds": 2,
            "aspect_ratio": "16:9",
        },
    )
    return response.json()


def generate(client: TestClient, shot_id: str, mode: str) -> dict:
    grant = client.post(
        "/v1/wallet/test-grants",
        json={
            "tier": "FAST",
            "amount_ms": 10_000,
            "idempotency_key": f"mock-job-grant:{uuid.uuid4()}",
            "reason": "mock job test",
        },
    )
    assert grant.status_code == 201
    quoted = client.post(
        "/v1/quotes",
        json={"shot_id": shot_id, "tier": "FAST", "resolution": "720P"},
    )
    assert quoted.status_code == 201
    response = client.post(
        "/v1/generations",
        headers={"x-test-generation-modes": mode},
        json={"shot_id": shot_id, "quote_id": quoted.json()["id"]},
    )
    assert response.status_code == 202
    job = response.json()
    refreshed = client.get(f"/v1/generations/{job['id']}")
    assert refreshed.status_code == 200
    return refreshed.json()


def test_mock_success_produces_playable_mp4(client: TestClient) -> None:
    job = generate(client, create_shot(client)["id"], "success")
    assert job["status"] == "SUCCEEDED"
    assert len(job["outputs"]) == 1
    assert job["final_output_id"] == job["outputs"][0]["id"]
    response = client.get(f"/v1/outputs/{job['outputs'][0]['id']}/content")
    assert response.status_code == 200
    assert response.content[4:8] == b"ftyp"

    with SessionLocal() as db:
        output = db.get(GenerationOutput, uuid.UUID(job["outputs"][0]["id"]))
        assert output is not None
        attempt = db.get(GenerationAttempt, output.attempt_id)
        assert attempt is not None
        assert attempt.job_id == output.job_id == uuid.UUID(job["id"])
        settings = get_settings()
        store = LocalObjectStorage(
            settings.storage_root,
            settings.storage_claim_secret.get_secret_value().encode(),
        )
        stored = store.stat(output.object_key)
        assert (stored.size_bytes, stored.sha256) == (output.size_bytes, output.sha256)


def test_failure_timeout_corrupt_and_duplicate_are_explicit(client: TestClient) -> None:
    shot = create_shot(client)
    failed = generate(client, shot["id"], "failure")
    assert (failed["status"], failed["failure_code"]) == (
        "FAILED_FINAL",
        "GENERATION_FAILED",
    )
    assert len(failed["attempts"]) == 1
    timed_out = generate(client, shot["id"], "timeout")
    assert (timed_out["status"], timed_out["failure_code"]) == (
        "FAILED_FINAL",
        "GENERATION_FAILED",
    )
    assert len(timed_out["attempts"]) == 2
    assert {attempt["failure_code"] for attempt in timed_out["attempts"]} == {
        "GENERATION_FAILED"
    }
    corrupt = generate(client, shot["id"], "corrupt")
    assert (corrupt["status"], corrupt["failure_code"]) == (
        "FAILED_FINAL",
        "OUTPUT_INVALID_MEDIA",
    )
    assert corrupt["outputs"][0]["validation_status"] == "INVALID"
    assert corrupt["final_output_id"] is None
    duplicate = generate(client, shot["id"], "duplicate")
    assert duplicate["status"] == "SUCCEEDED"
    assert len(duplicate["outputs"]) == 1


def test_cancelled_job_is_terminal(client: TestClient, monkeypatch) -> None:
    async def stay_queued(self, job_id):
        return ProviderExecutionStep(is_complete=True, poll_count=0)

    monkeypatch.setattr(GenerationExecutionService, "execute", stay_queued)
    shot = create_shot(client)
    job = generate(client, shot["id"], "delayed")
    assert job["status"] == "QUEUED"
    response = client.post(f"/v1/generations/{job['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_other_user_cannot_access_job_or_output(client: TestClient) -> None:
    job = generate(client, create_shot(client)["id"], "success")
    assert (
        client.get(f"/v1/generations/{job['id']}", headers={"x-test-user": "other"}).status_code
        == 404
    )
    assert (
        client.get(
            f"/v1/outputs/{job['outputs'][0]['id']}/content", headers={"x-test-user": "other"}
        ).status_code
        == 404
    )


pytestmark = pytest.mark.database
