from fastapi.testclient import TestClient

from app.provider import MockVideoProvider
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
    response = client.post("/v1/generations", json={"shot_id": shot_id, "mock_mode": mode})
    assert response.status_code == 202
    job = response.json()
    refreshed = client.get(f"/v1/generations/{job['id']}")
    assert refreshed.status_code == 200
    return refreshed.json()


def test_mock_success_produces_playable_mp4(client: TestClient) -> None:
    job = generate(client, create_shot(client)["id"], "success")
    assert job["status"] == "SUCCEEDED"
    assert len(job["outputs"]) == 1
    response = client.get(f"/v1/outputs/{job['outputs'][0]['id']}/content")
    assert response.status_code == 200
    assert response.content[4:8] == b"ftyp"


def test_failure_timeout_corrupt_and_duplicate_are_explicit(client: TestClient) -> None:
    shot = create_shot(client)
    failed = generate(client, shot["id"], "failure")
    assert (failed["status"], failed["error_code"]) == ("FAILED_FINAL", "MOCK_PROVIDER_FAILED")
    timed_out = generate(client, shot["id"], "timeout")
    assert (timed_out["status"], timed_out["error_code"]) == ("FAILED_FINAL", "MOCK_TIMEOUT")
    corrupt = generate(client, shot["id"], "corrupt")
    assert (corrupt["status"], corrupt["error_code"]) == ("FAILED_FINAL", "OUTPUT_INVALID_MP4")
    assert corrupt["outputs"][0]["is_valid"] is False
    duplicate = generate(client, shot["id"], "duplicate")
    assert duplicate["status"] == "SUCCEEDED"
    assert len(duplicate["outputs"]) == 1


def test_cancelled_job_is_terminal(client: TestClient, monkeypatch) -> None:
    async def stay_queued(self, job_id):
        return None

    monkeypatch.setattr(MockVideoProvider, "submit", stay_queued)
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
