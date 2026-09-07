import pytest

from app.db import SessionLocal
from app.models import Project
from tests.test_provider_routing import _quoted_shot

pytestmark = pytest.mark.database


def test_asset_listing_and_atomic_binding(client):
    shot, _, first = _quoted_shot(client, with_reference=True)
    path = f"/v1/projects/{shot['project_id']}/assets"
    second = client.post(
        path, files={"file": ("second.png", b"\x89PNG\r\n\x1a\nsecond", "image/png")}
    ).json()
    page = client.get(path, params={"limit": 1}).json()
    following = client.get(path, params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert {page["items"][0]["id"], following["items"][0]["id"]} == {first["id"], second["id"]}
    assert following["next_cursor"] is None
    assert client.get(path, headers={"x-test-user": "other"}).status_code == 404
    binding = f"/v1/shots/{shot['id']}/input"
    result = client.put(binding, json={"asset_id": second["id"]})
    assert result.status_code == 200
    assert [r["asset_id"] for r in result.json()["references"]] == [second["id"]]
    foreign, _, foreign_asset = _quoted_shot(client, with_reference=True)
    assert foreign["project_id"] != shot["project_id"]
    assert client.put(binding, json={"asset_id": foreign_asset["id"]}).status_code == 404
    assert client.get(f"/v1/shots/{shot['id']}").json()["references"][0]["asset_id"] == second["id"]
    assert client.put(binding, json={"asset_id": None}).json()["references"] == []


def test_reading_generation_options_does_not_bind_project(client):
    import uuid

    shot, _, _ = _quoted_shot(client, with_reference=False)
    response = client.get(f"/v1/projects/{shot['project_id']}/generation-options")
    assert response.status_code == 200
    assert response.json()["requires_reference_image"] is False
    with SessionLocal() as db:
        assert db.get(Project, uuid.UUID(shot["project_id"])).route_binding_status == "UNBOUND"
