import uuid

import pytest
from pydantic import ValidationError

from app.input_snapshot import JobInputSnapshot


def snapshot_payload():
    return {
        "version": 1,
        "prompt": "accepted prompt",
        "negative_prompt": None,
        "references": [
            {
                "asset_id": str(uuid.uuid4()),
                "object_key": "assets/original.png",
                "reference_role": "FIRST_FRAME",
            }
        ],
        "duration_ms": 2000,
        "resolution": "720P",
        "aspect_ratio": "16:9",
        "mode": "success",
    }


def test_snapshot_roundtrip_is_frozen_and_has_no_implicit_inputs():
    payload = snapshot_payload()
    snapshot = JobInputSnapshot.model_validate(payload)
    assert snapshot.model_dump(mode="json") == payload
    assert JobInputSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    payload["references"][0]["object_key"] = "assets/replaced.png"
    assert snapshot.references[0].object_key == "assets/original.png"
    with pytest.raises(ValidationError):
        snapshot.prompt = "changed"
    with pytest.raises(ValidationError):
        snapshot.references[0].object_key = "assets/replaced.png"
    for key in snapshot_payload():
        incomplete = snapshot_payload()
        del incomplete[key]
        with pytest.raises(ValidationError):
            JobInputSnapshot.model_validate(incomplete)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 1.0),
        ("version", 2),
        ("prompt", ""),
        ("duration_ms", True),
        ("duration_ms", "2000"),
        ("duration_ms", 0),
        ("resolution", "4K"),
        ("aspect_ratio", "1:1"),
        ("mode", "arbitrary"),
        ("references", None),
        ("extra", "unexpected"),
    ],
)
def test_snapshot_rejects_invalid_values(field, value):
    payload = snapshot_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        JobInputSnapshot.model_validate(payload)


@pytest.mark.parametrize("key", ["../a", "/a", "a/../b", "a//b", "a/", "a\\b", "C:/a", "."])
def test_snapshot_requires_canonical_object_key(key):
    payload = snapshot_payload()
    payload["references"][0]["object_key"] = key
    with pytest.raises(ValidationError):
        JobInputSnapshot.model_validate(payload)


def test_snapshot_v1_has_explicit_reference_cardinality_and_role():
    payload = snapshot_payload()
    payload["references"] = []
    assert JobInputSnapshot.model_validate(payload).references == ()
    payload = snapshot_payload()
    payload["references"] *= 2
    with pytest.raises(ValidationError):
        JobInputSnapshot.model_validate(payload)
    payload = snapshot_payload()
    payload["references"][0]["reference_role"] = "STYLE"
    with pytest.raises(ValidationError):
        JobInputSnapshot.model_validate(payload)
