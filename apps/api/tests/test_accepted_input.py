import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import GenerationAttempt, GenerationJob, Project, ProjectAsset, Shot, ShotReference
from app.provider import FailureCode, ProviderFailure
from app.provider_execution import GenerationExecutionService
from app.routing import SIMULATED_RUNPOD_ROUTE_VERSION, get_provider_registry, get_route_registry
from app.storage import LocalObjectStorage
from tests.test_provider_routing import _claim_payload, _quoted_shot

pytestmark = pytest.mark.database


@pytest.fixture
def simulated_route():
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
    routes.set_enabled(SIMULATED_RUNPOD_ROUTE_VERSION, True)
    yield
    routes.activate(previous)


def executor():
    settings = get_settings()
    return GenerationExecutionService(
        LocalObjectStorage(
            settings.storage_root, settings.storage_claim_secret.get_secret_value().encode()
        )
    )


def submit(client, shot, quote):
    response = client.post("/v1/generations", json={"shot_id": shot["id"], "quote_id": quote["id"]})
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["id"])


def submitted_request(job_id):
    with SessionLocal() as db:
        attempt = db.scalar(
            select(GenerationAttempt)
            .where(GenerationAttempt.job_id == job_id)
            .order_by(GenerationAttempt.attempt_no.desc())
        )
        return get_provider_registry().get("runpod-simulator").submission(attempt.provider_job_id)


def test_accepted_input_survives_edits_unbind_restart_and_retry(raw_client, simulated_route):
    shot, quote, asset = _quoted_shot(raw_client, with_reference=True)
    job_id = submit(raw_client, shot, quote)
    with SessionLocal() as db:
        original = db.get(GenerationJob, job_id).input_snapshot_json
        asset_key = db.get(ProjectAsset, uuid.UUID(asset["id"])).object_key
    references = raw_client.get(f"/v1/shots/{shot['id']}").json()["references"]
    assert (
        raw_client.delete(f"/v1/shots/{shot['id']}/references/{references[0]['id']}").status_code
        == 204
    )
    assert (
        raw_client.patch(f"/v1/shots/{shot['id']}", json={"prompt": "new prompt"}).status_code
        == 200
    )
    upload = raw_client.post(
        f"/v1/projects/{shot['project_id']}/assets",
        files={"file": ("new.png", b"\x89PNG\r\n\x1a\nnew", "image/png")},
    )
    new_asset = upload.json()
    assert (
        raw_client.post(
            f"/v1/shots/{shot['id']}/references",
            json={"asset_id": new_asset["id"], "reference_role": "FIRST_FRAME"},
        ).status_code
        == 201
    )
    asyncio.run(executor().execute(job_id))
    first = submitted_request(job_id)
    assert first.prompt == shot["prompt"]
    assert _claim_payload(first.input_claim)["key"] == asset_key
    service = executor()
    context = service._load_active_attempt(job_id)
    assert service._fail_attempt(context, ProviderFailure(FailureCode.NETWORK_TIMEOUT, "retry"))
    asyncio.run(executor().execute(job_id))
    retry = submitted_request(job_id)
    assert retry.attempt_id != first.attempt_id
    assert retry.prompt == first.prompt
    assert _claim_payload(retry.input_claim)["key"] == asset_key
    new_quote = raw_client.post(
        "/v1/quotes", json={"shot_id": shot["id"], "tier": "FAST", "resolution": "720P"}
    ).json()
    new_id = submit(raw_client, shot, new_quote)
    asyncio.run(executor().execute(new_id))
    assert submitted_request(new_id).prompt == "new prompt"
    with SessionLocal() as db:
        new_key = db.get(ProjectAsset, uuid.UUID(new_asset["id"])).object_key
        assert db.get(GenerationJob, job_id).input_snapshot_json == original
    assert _claim_payload(submitted_request(new_id).input_claim)["key"] == new_key


def test_batch_captures_each_shot_and_rejects_ambiguous_reference(raw_client):
    shot, quote, asset = _quoted_shot(raw_client, with_reference=True)
    second = raw_client.post(
        f"/v1/projects/{shot['project_id']}/shots",
        json={
            "title": "second",
            "prompt": "second input",
            "duration_seconds": 5,
            "aspect_ratio": "16:9",
        },
    ).json()
    second_quote = raw_client.post(
        "/v1/quotes", json={"shot_id": second["id"], "tier": "FAST", "resolution": "720P"}
    ).json()
    response = raw_client.post(
        "/v1/batches", json={"items": [{"quote_id": quote["id"]}, {"quote_id": second_quote["id"]}]}
    )
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        jobs = db.scalars(select(GenerationJob)).all()
        assert {j.input_snapshot_json["prompt"] for j in jobs} == {shot["prompt"], "second input"}
        assert sorted(len(j.input_snapshot_json["references"]) for j in jobs) == [0, 1]
    upload = raw_client.post(
        f"/v1/projects/{shot['project_id']}/assets",
        files={"file": ("two.png", b"\x89PNG\r\n\x1a\n2", "image/png")},
    )
    raw_client.post(
        f"/v1/shots/{shot['id']}/references",
        json={"asset_id": upload.json()["id"], "reference_role": "FIRST_FRAME"},
    )
    quote_response = raw_client.post(
        "/v1/quotes", json={"shot_id": shot["id"], "tier": "FAST", "resolution": "720P"}
    )
    # Some quote paths reject ambiguous input immediately; generation must never guess.
    if quote_response.status_code == 201:
        rejected = raw_client.post(
            "/v1/generations", json={"shot_id": shot["id"], "quote_id": quote_response.json()["id"]}
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "REFERENCE_AMBIGUOUS"
    else:
        assert quote_response.status_code == 422


def test_submission_reads_whole_input_after_concurrent_project_edit(raw_client):
    shot, quote, _ = _quoted_shot(raw_client, with_reference=True)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with SessionLocal() as editor:
            editor.scalar(
                select(Project).where(Project.id == uuid.UUID(shot["project_id"])).with_for_update()
            )
            editor.get(Shot, uuid.UUID(shot["id"])).prompt = "committed together"
            ref = editor.scalar(
                select(ShotReference).where(ShotReference.shot_id == uuid.UUID(shot["id"]))
            )
            editor.delete(ref)
            editor.flush()
            pending = pool.submit(submit, raw_client, shot, quote)
            editor.commit()
        job_id = pending.result(timeout=15)
        with SessionLocal() as db:
            snapshot = db.get(GenerationJob, job_id).input_snapshot_json
            assert (snapshot["prompt"], snapshot["references"]) == ("committed together", [])
    finally:
        pool.shutdown(wait=True)
