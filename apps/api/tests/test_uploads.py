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
    assert asset["media_type"] == "image/png"
    assert asset["status"] == "READY"
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


def test_shot_reference_requires_an_asset_from_the_same_project(client: TestClient) -> None:
    project = create_project(client, "owner")
    other_project = create_project(client, "owner")
    shot = client.post(
        f"/v1/projects/{project['id']}/shots",
        headers={"x-test-user": "owner"},
        json={
            "title": "引用镜头",
            "prompt": "保持角色一致",
            "duration_seconds": 3,
            "aspect_ratio": "16:9",
        },
    ).json()

    def upload(project_id: str) -> dict:
        response = client.post(
            f"/v1/projects/{project_id}/assets",
            headers={"x-test-user": "owner"},
            files={
                "file": (
                    "reference.png",
                    io.BytesIO(b"\x89PNG\r\n\x1a\nreference"),
                    "image/png",
                )
            },
        )
        assert response.status_code == 201
        return response.json()

    asset = upload(project["id"])
    other_asset = upload(other_project["id"])
    response = client.post(
        f"/v1/shots/{shot['id']}/references",
        headers={"x-test-user": "owner"},
        json={"asset_id": asset["id"], "reference_role": "FIRST_FRAME"},
    )
    assert response.status_code == 201
    assert response.json()["asset_id"] == asset["id"]
    refreshed = client.get(f"/v1/shots/{shot['id']}", headers={"x-test-user": "owner"})
    assert refreshed.json()["references"][0]["reference_role"] == "FIRST_FRAME"

    response = client.post(
        f"/v1/shots/{shot['id']}/references",
        headers={"x-test-user": "owner"},
        json={"asset_id": other_asset["id"], "reference_role": "STYLE"},
    )
    assert response.status_code == 404

    reference_id = refreshed.json()["references"][0]["id"]
    deleted = client.delete(
        f"/v1/shots/{shot['id']}/references/{reference_id}",
        headers={"x-test-user": "owner"},
    )
    assert deleted.status_code == 204
    assert client.get(
        f"/v1/assets/{asset['id']}/content", headers={"x-test-user": "owner"}
    ).status_code == 200
    assert (
        client.get(f"/v1/shots/{shot['id']}", headers={"x-test-user": "owner"})
        .json()["references"]
        == []
    )
