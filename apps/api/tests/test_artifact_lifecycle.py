import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.artifact_lifecycle import (
    ArtifactCleanupDispatcher,
    inventory_local_sources,
    register_artifact,
)
from app.config import get_settings
from app.db import SessionLocal
from app.errors import ApiError
from app.models import (
    GenerationAttempt,
    OutboxStatus,
    Project,
    ProviderArtifact,
    StorageCleanupEvent,
    StorageCleanupObject,
)
from app.outbox import DispatchResult
from app.project_cleanup import StorageCleanupDispatcher, request_project_deletion
from app.storage import LocalObjectStorage
from tests.test_accepted_input import executor
from tests.test_provider_routing import _quoted_shot

pytestmark = pytest.mark.database


@pytest.fixture(params=["success", "failure"])
def artifacts(raw_client, monkeypatch, request):
    monkeypatch.setattr(get_settings(), "provider_artifact_retention_seconds", 60)
    shot, quote, _ = _quoted_shot(raw_client, with_reference=False)
    response = raw_client.post(
        "/v1/generations",
        json={"shot_id": shot["id"], "quote_id": quote["id"]},
        headers={"x-test-generation-modes": request.param},
    )
    assert response.status_code == 202
    job = uuid.UUID(response.json()["id"])
    with SessionLocal() as db:
        attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job)).id
    current = [datetime.now(UTC)]

    def clock():
        return current[0]

    settings = get_settings()
    store = LocalObjectStorage(
        settings.storage_root,
        settings.storage_claim_secret.get_secret_value().encode(),
        clock=clock,
    )
    return shot, job, attempt, current, clock, store


def source(fixture):
    _shot, job, attempt, _current, clock, store = fixture
    claim = store.write_claim(
        f"provider-outputs/{job}/{attempt}",
        mime_type="video/mp4",
        max_bytes=64,
        expires_in=timedelta(seconds=30),
    )
    register_artifact(
        SessionLocal,
        job,
        attempt,
        claim.object_key,
        "SOURCE",
        expires_at=claim.expires_at,
        clock=clock,
    )
    store.put(claim, b"remote-artifact", "video/mp4")
    return claim.object_key


def test_retention_active_job_project_deletion_and_late_source(artifacts):
    shot, job, attempt, current, clock, store = artifacts
    key = source(artifacts)
    cleanup = ArtifactCleanupDispatcher(SessionLocal, store, clock=clock)
    current[0] += timedelta(seconds=120)
    assert asyncio.run(cleanup.dispatch_once()) == DispatchResult.IDLE
    assert store.stat(key).size_bytes > 0  # Active inputs/work cannot be collected.
    asyncio.run(executor().execute(job))
    with SessionLocal() as db:
        project = db.scalar(
            select(Project).where(Project.id == uuid.UUID(shot["project_id"])).with_for_update()
        )
        request_project_deletion(db, project, clock=lambda: datetime.now(UTC))
        db.commit()
    project_cleanup = StorageCleanupDispatcher(SessionLocal, store, clock=lambda: datetime.now(UTC))
    assert asyncio.run(project_cleanup.dispatch_once()) == DispatchResult.RETRY_SCHEDULED
    with SessionLocal() as db:
        assert db.scalar(select(StorageCleanupEvent)).status == OutboxStatus.PENDING
        assert key in set(db.scalars(select(StorageCleanupObject.object_key)))
    current[0] += timedelta(hours=1)
    project_cleanup = StorageCleanupDispatcher(SessionLocal, store, clock=clock)
    assert asyncio.run(project_cleanup.dispatch_once()) == DispatchResult.PUBLISHED
    with pytest.raises(Exception, match="不存在"):
        store.stat(key)
    # A late but correctly owned output reopens the durable project manifest.
    late = source(artifacts)
    with SessionLocal() as db:
        assert db.scalar(select(StorageCleanupEvent)).status == OutboxStatus.PENDING
    current[0] += timedelta(minutes=2)
    assert asyncio.run(project_cleanup.dispatch_once()) == DispatchResult.PUBLISHED
    with pytest.raises(Exception, match="不存在"):
        store.stat(late)


def test_orphan_cleanup_recovers_crash_after_delete_and_retry(artifacts, monkeypatch):
    _shot, job, _attempt, current, clock, store = artifacts
    key = source(artifacts)
    asyncio.run(executor().execute(job))
    current[0] += timedelta(hours=1)
    cleanup = ArtifactCleanupDispatcher(SessionLocal, store, clock=clock)
    original_delete = store.delete

    def fail_once(target):
        monkeypatch.setattr(store, "delete", original_delete)
        raise OSError("temporary storage failure")

    monkeypatch.setattr(store, "delete", fail_once)
    assert asyncio.run(cleanup.dispatch_once()) == DispatchResult.RETRY_SCHEDULED
    current[0] += timedelta(seconds=3)

    def crash(_identity):
        raise RuntimeError("crash after delete")

    monkeypatch.setattr(cleanup, "after_delete", crash)
    with pytest.raises(RuntimeError, match="crash after delete"):
        asyncio.run(cleanup.dispatch_once())
    current[0] += timedelta(seconds=31)
    recovered = ArtifactCleanupDispatcher(SessionLocal, store, clock=clock)
    while asyncio.run(recovered.dispatch_once()) == DispatchResult.PUBLISHED:
        pass
    with SessionLocal() as db:
        artifact = db.scalar(select(ProviderArtifact).where(ProviderArtifact.object_key == key))
        assert artifact.status == OutboxStatus.PUBLISHED
        assert artifact.cleaned_at is not None


def test_artifact_cleanup_rejects_other_project_or_path(artifacts):
    _shot, job, attempt, _current, clock, _store = artifacts
    for key in [
        f"provider-outputs/{uuid.uuid4()}/{attempt}/x.mp4",
        f"provider-outputs/{job}/{attempt}/../x.mp4",
        "assets/private.png",
    ]:
        with pytest.raises((ValueError, ApiError)):
            register_artifact(SessionLocal, job, attempt, key, "SOURCE", clock=clock)
    with SessionLocal() as db:
        assert list(db.scalars(select(ProviderArtifact))) == []


def test_legacy_inventory_registers_only_known_owned_namespace(artifacts):
    _shot, job, attempt, _current, _clock, store = artifacts
    claim = store.write_claim(
        f"provider-outputs/{job}/{attempt}", mime_type="video/mp4", max_bytes=32
    )
    store.put(claim, b"legacy source", "video/mp4")
    unknown = store.write_claim(
        f"provider-outputs/{uuid.uuid4()}/{uuid.uuid4()}", mime_type="video/mp4", max_bytes=32
    )
    store.put(unknown, b"unknown source", "video/mp4")
    assert {item["status"] for item in inventory_local_sources(store.root, SessionLocal)} == {
        "ELIGIBLE",
        "REVIEW_REQUIRED",
    }
    with SessionLocal() as db:
        assert list(db.scalars(select(ProviderArtifact))) == []
    report = inventory_local_sources(store.root, SessionLocal, apply=True)
    assert {item["status"] for item in report} == {"REGISTERED", "REVIEW_REQUIRED"}
    assert store.stat(unknown.object_key).size_bytes > 0
    with SessionLocal() as db:
        assert [item.object_key for item in db.scalars(select(ProviderArtifact))] == [
            claim.object_key
        ]
