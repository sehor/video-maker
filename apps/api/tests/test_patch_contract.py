import pytest

from app.schemas import ProjectUpdate, ShotUpdate
from tests.test_provider_routing import _quoted_shot

pytestmark = pytest.mark.database


@pytest.mark.parametrize("field", ["name", "title", "prompt", "duration_seconds", "aspect_ratio"])
def test_patch_rejects_explicit_null_without_modifying_record(client, field):
    shot, _, _ = _quoted_shot(client, with_reference=False)
    path = f"/v1/projects/{shot['project_id']}" if field == "name" else f"/v1/shots/{shot['id']}"
    before = client.get(path).json()
    response = client.patch(path, json={field: None})
    assert response.status_code == 422
    assert response.json()["error"]["request_id"]
    assert client.get(path).json() == before
    assert client.patch(path, json={}).json() == before
    schema = (ProjectUpdate if field == "name" else ShotUpdate).model_json_schema()
    assert "anyOf" not in schema["properties"][field]


def test_patch_nullable_description_and_valid_values(client):
    shot, _, _ = _quoted_shot(client, with_reference=False)
    path = f"/v1/projects/{shot['project_id']}"
    assert client.patch(path, json={"description": None}).json()["description"] is None
    assert client.patch(path, json={"name": "Renamed"}).json()["name"] == "Renamed"
    changed = client.patch(f"/v1/shots/{shot['id']}", json={"duration_seconds": 6})
    assert changed.status_code == 200
    assert changed.json()["duration_seconds"] == 6
