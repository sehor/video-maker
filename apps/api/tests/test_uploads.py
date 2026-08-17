import io

from fastapi.testclient import TestClient

from tests.test_projects_permissions import create_project


def test_upload_and_private_download(client: TestClient) -> None:
    project = create_project(client, "owner")
    png = b"\x89PNG\r\n\x1a\n" + b"test-content"
    response = client.post(
        f"/v1/projects/{project['id']}/assets",
        headers={"x-test-user": "owner"},
        files={"file": ("reference.png", io.BytesIO(png), "image/png")},
    )
    assert response.status_code == 201
    asset = response.json()
    assert asset["original_filename"] == "reference.png"
    assert len(asset["sha256"]) == 64
    assert (
        client.get(
            f"/v1/assets/{asset['id']}/content", headers={"x-test-user": "owner"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/v1/assets/{asset['id']}/content", headers={"x-test-user": "other"}
        ).status_code
        == 404
    )


def test_rejects_mime_spoofing(client: TestClient) -> None:
    project = create_project(client)
    response = client.post(
        f"/v1/projects/{project['id']}/assets",
        files={"file": ("fake.png", io.BytesIO(b"not-png"), "image/png")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UPLOAD_CONTENT_INVALID"
