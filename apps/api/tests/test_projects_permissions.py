from fastapi.testclient import TestClient


def create_project(client: TestClient, user: str = "user-a") -> dict:
    response = client.post(
        "/v1/projects",
        headers={"x-test-user": user},
        json={"name": "第一个项目", "description": "阶段一"},
    )
    assert response.status_code == 201
    return response.json()


def test_project_and_shot_crud(client: TestClient) -> None:
    project = create_project(client)
    response = client.post(
        f"/v1/projects/{project['id']}/shots",
        headers={"x-test-user": "user-a"},
        json={
            "title": "镜头 1",
            "prompt": "雨夜中的城市街道",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
        },
    )
    assert response.status_code == 201
    shot = response.json()
    assert shot["duration_seconds"] == 5
    response = client.patch(
        f"/v1/shots/{shot['id']}",
        headers={"x-test-user": "user-a"},
        json={"duration_seconds": 6},
    )
    assert response.status_code == 200
    assert response.json()["duration_seconds"] == 6


def test_other_user_cannot_access_project_or_shot(client: TestClient) -> None:
    project = create_project(client, "owner")
    shot = client.post(
        f"/v1/projects/{project['id']}/shots",
        headers={"x-test-user": "owner"},
        json={"title": "秘密", "prompt": "不可见", "duration_seconds": 3, "aspect_ratio": "9:16"},
    ).json()

    assert (
        client.get(f"/v1/projects/{project['id']}", headers={"x-test-user": "other"}).status_code
        == 404
    )
    assert (
        client.get(f"/v1/shots/{shot['id']}", headers={"x-test-user": "other"}).status_code == 404
    )


def test_error_has_machine_code_and_request_id(client: TestClient) -> None:
    response = client.get("/v1/projects/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "PROJECT_NOT_FOUND"
    assert body["request_id"]
