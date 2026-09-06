import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import (
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobEvent,
    LedgerPosting,
    LedgerTransaction,
    OutboxStatus,
    Project,
    ProjectAsset,
    ProjectAssetStatus,
    ProjectStatus,
    Shot,
    StorageCleanupEvent,
    StorageCleanupObject,
    StorageCleanupObjectStatus,
)
from app.outbox import DispatchResult
from app.project_cleanup import (
    ClaimedStorageCleanupEvent,
    StorageCleanupDispatcher,
    StorageCleanupRequest,
)
from tests.test_mock_jobs import create_shot, generate
from tests.test_projects_permissions import create_project
from tests.test_quote_ledger import grant, quote, submit


class SimulatedCrash(BaseException):
    pass


@dataclass
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeCleanupStorage:
    def __init__(self, keys: set[str], *, fail_once: set[str] | None = None) -> None:
        self.keys = set(keys)
        self.fail_once = set(fail_once or ())
        self.calls: list[str] = []

    def delete(self, key: str) -> None:
        self.calls.append(key)
        if key in self.fail_once:
            self.fail_once.remove(key)
            raise TimeoutError("simulated storage timeout")
        self.keys.discard(key)


class CrashAfterDeleteDispatcher(StorageCleanupDispatcher):
    def after_delete(
        self,
        event: ClaimedStorageCleanupEvent,
        request: StorageCleanupRequest,
    ) -> None:
        raise SimulatedCrash


def upload_asset(client: TestClient, project_id: str, name: str) -> dict:
    response = client.post(
        f"/v1/projects/{project_id}/assets",
        files={"file": (name, b"\x89PNG\r\n\x1a\nfixture", "image/png")},
    )
    assert response.status_code == 201
    return response.json()


def cleanup_state(project_id: str):
    with SessionLocal() as db:
        event = db.scalar(
            select(StorageCleanupEvent).where(
                StorageCleanupEvent.project_id == uuid.UUID(project_id)
            )
        )
        assert event is not None
        objects = list(
            db.scalars(
                select(StorageCleanupObject)
                .where(StorageCleanupObject.event_id == event.id)
                .order_by(StorageCleanupObject.object_key)
            )
        )
        db.expunge(event)
        for cleanup_object in objects:
            db.expunge(cleanup_object)
        return event, objects


def test_project_delete_is_soft_idempotent_and_preserves_audit_history(
    client: TestClient,
) -> None:
    shot = create_shot(client)
    job = generate(client, shot["id"], "success")
    project_id = shot["project_id"]
    project_uuid = uuid.UUID(project_id)

    with SessionLocal() as db:
        output_keys = set(
            db.scalars(
                select(GenerationOutput.object_key)
                .join(GenerationJob, GenerationJob.id == GenerationOutput.job_id)
                .where(GenerationJob.project_id == project_uuid)
            )
        )
        before = {
            "shots": db.scalar(
                select(func.count()).select_from(Shot).where(Shot.project_id == project_uuid)
            ),
            "jobs": db.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(GenerationJob.project_id == project_uuid)
            ),
            "attempts": db.scalar(select(func.count()).select_from(GenerationAttempt)),
            "outputs": db.scalar(select(func.count()).select_from(GenerationOutput)),
            "job_events": db.scalar(select(func.count()).select_from(JobEvent)),
            "ledger_transactions": db.scalar(select(func.count()).select_from(LedgerTransaction)),
            "ledger_postings": db.scalar(select(func.count()).select_from(LedgerPosting)),
        }

    assert client.delete(f"/v1/projects/{project_id}").status_code == 204
    assert client.delete(f"/v1/projects/{project_id}").status_code == 204
    assert client.get(f"/v1/projects/{project_id}").status_code == 404
    assert project_id not in {item["id"] for item in client.get("/v1/projects").json()["items"]}

    with SessionLocal() as db:
        project = db.get(Project, uuid.UUID(project_id))
        assert project is not None
        assert project.status == ProjectStatus.DELETED
        assert project.deleted_at is not None
        assert (
            db.scalar(select(func.count()).select_from(Shot).where(Shot.project_id == project.id))
            == before["shots"]
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(GenerationJob.project_id == project.id)
            )
            == before["jobs"]
        )
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == before["attempts"]
        assert db.scalar(select(func.count()).select_from(GenerationOutput)) == before["outputs"]
        assert db.scalar(select(func.count()).select_from(JobEvent)) == before["job_events"]
        assert (
            db.scalar(select(func.count()).select_from(LedgerTransaction))
            == before["ledger_transactions"]
        )
        assert (
            db.scalar(select(func.count()).select_from(LedgerPosting)) == before["ledger_postings"]
        )
        assert db.get(GenerationJob, uuid.UUID(job["id"])) is not None
        assert (
            db.scalar(
                select(func.count())
                .select_from(StorageCleanupEvent)
                .where(StorageCleanupEvent.project_id == project.id)
            )
            == 1
        )

    event, objects = cleanup_state(project_id)
    assert event.status == OutboxStatus.PENDING
    assert {item.object_key for item in objects} == output_keys


def test_project_with_active_job_cannot_be_deleted(raw_client: TestClient) -> None:
    shot = create_shot(raw_client)
    grant(raw_client, 10_000, "grant:active-project-delete")
    quoted = quote(raw_client, shot["id"])
    response = submit(raw_client, shot["id"], quoted["id"])
    assert response.status_code == 202

    deleted = raw_client.delete(f"/v1/projects/{shot['project_id']}")
    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "PROJECT_HAS_ACTIVE_JOBS"
    assert raw_client.get(f"/v1/projects/{shot['project_id']}").status_code == 200


def test_deleted_project_rejects_new_resources(raw_client: TestClient) -> None:
    shot = create_shot(raw_client)
    project_id = shot["project_id"]
    quoted = quote(raw_client, shot["id"])
    assert raw_client.delete(f"/v1/projects/{project_id}").status_code == 204

    assert (
        raw_client.patch(f"/v1/projects/{project_id}", json={"name": "revive"}).status_code == 404
    )
    assert (
        raw_client.post(
            f"/v1/projects/{project_id}/shots",
            json={
                "title": "blocked",
                "prompt": "blocked",
                "duration_seconds": 2,
                "aspect_ratio": "16:9",
            },
        ).status_code
        == 404
    )
    assert raw_client.patch(f"/v1/shots/{shot['id']}", json={"title": "blocked"}).status_code == 404
    assert (
        raw_client.post(
            f"/v1/projects/{project_id}/assets",
            files={"file": ("blocked.png", b"\x89PNG\r\n\x1a\nfixture", "image/png")},
        ).status_code
        == 404
    )
    assert (
        raw_client.post(
            "/v1/quotes",
            json={"shot_id": shot["id"], "tier": "FAST", "resolution": "720P"},
        ).status_code
        == 404
    )
    assert (
        raw_client.post(
            "/v1/generations",
            json={"shot_id": shot["id"], "quote_id": quoted["id"]},
        ).status_code
        == 404
    )
    batch = raw_client.post("/v1/batches", json={"items": [{"quote_id": quoted["id"]}]})
    assert batch.status_code == 404
    assert batch.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_storage_cleanup_retries_partial_failure_and_records_each_object(
    raw_client: TestClient,
) -> None:
    project = create_project(raw_client)
    upload_asset(raw_client, project["id"], "a.png")
    upload_asset(raw_client, project["id"], "b.png")
    assert raw_client.delete(f"/v1/projects/{project['id']}").status_code == 204
    _, cleanup_objects = cleanup_state(project["id"])
    keys = [item.object_key for item in cleanup_objects]
    assert len(keys) == 2

    clock = MutableClock(datetime.now(UTC))
    storage = FakeCleanupStorage(set(keys), fail_once={keys[1]})
    dispatcher = StorageCleanupDispatcher(
        SessionLocal,
        storage,  # type: ignore[arg-type]
        retry_delay=timedelta(seconds=2),
        clock=clock,
    )

    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.RETRY_SCHEDULED
    event, failed_objects = cleanup_state(project["id"])
    assert event.status == OutboxStatus.PENDING
    assert [item.status for item in failed_objects] == [
        StorageCleanupObjectStatus.DELETED,
        StorageCleanupObjectStatus.PENDING,
    ]
    assert failed_objects[1].last_error == "TimeoutError: simulated storage timeout"

    clock.advance(timedelta(seconds=2))
    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.PUBLISHED
    event, completed_objects = cleanup_state(project["id"])
    assert event.status == OutboxStatus.PUBLISHED
    assert all(item.status == StorageCleanupObjectStatus.DELETED for item in completed_objects)
    assert [item.attempt_count for item in completed_objects] == [1, 2]
    assert storage.calls == [keys[0], keys[1], keys[1]]
    assert storage.keys == set()
    with SessionLocal() as db:
        assert set(
            db.scalars(
                select(ProjectAsset.status).where(
                    ProjectAsset.project_id == uuid.UUID(project["id"])
                )
            )
        ) == {ProjectAssetStatus.DELETED}


def test_cleanup_reclaims_expired_lease_after_delete_crash(
    raw_client: TestClient,
) -> None:
    project = create_project(raw_client)
    upload_asset(raw_client, project["id"], "missing-on-replay.png")
    assert raw_client.delete(f"/v1/projects/{project['id']}").status_code == 204
    _, cleanup_objects = cleanup_state(project["id"])
    object_key = cleanup_objects[0].object_key
    clock = MutableClock(datetime.now(UTC))
    storage = FakeCleanupStorage({object_key})

    with pytest.raises(SimulatedCrash):
        asyncio.run(
            CrashAfterDeleteDispatcher(
                SessionLocal,
                storage,  # type: ignore[arg-type]
                lease_duration=timedelta(seconds=10),
                clock=clock,
            ).dispatch_once()
        )
    event, crashed_objects = cleanup_state(project["id"])
    assert event.status == OutboxStatus.PROCESSING
    assert crashed_objects[0].status == StorageCleanupObjectStatus.PENDING
    assert storage.keys == set()

    clock.advance(timedelta(seconds=11))
    result = asyncio.run(
        StorageCleanupDispatcher(
            SessionLocal,
            storage,  # type: ignore[arg-type]
            lease_duration=timedelta(seconds=10),
            clock=clock,
        ).dispatch_once()
    )
    assert result == DispatchResult.PUBLISHED
    event, completed_objects = cleanup_state(project["id"])
    assert event.status == OutboxStatus.PUBLISHED
    assert completed_objects[0].status == StorageCleanupObjectStatus.DELETED
    assert completed_objects[0].attempt_count == 2
    assert storage.calls == [object_key, object_key]
