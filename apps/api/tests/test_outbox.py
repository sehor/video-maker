import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.ledger import utcnow
from app.models import Job, LedgerTransaction, OutboxEvent, WorkflowRun
from app.outbox import (
    DispatcherCrash,
    LocalWorkflowRunner,
    OutboxDispatcher,
)
from app.storage import LocalObjectStorage
from tests.test_mock_jobs import create_shot
from tests.test_stage_two_ledger import grant, quote, submit


async def no_drain(_storage) -> None:
    return None


def queued_generation(client: TestClient, monkeypatch) -> tuple[dict, dict]:
    monkeypatch.setattr("app.api.drain_local_tasks", no_drain)
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    response = submit(client, shot["id"], item["id"])
    assert response.status_code == 202
    return response.json(), item


def run_local_workflow() -> None:
    store = LocalObjectStorage(get_settings().storage_root)
    asyncio.run(LocalWorkflowRunner(store).run_all())


def test_job_reservation_and_outbox_commit_together(client: TestClient, monkeypatch) -> None:
    job, _ = queued_generation(client, monkeypatch)
    with SessionLocal() as db:
        stored = db.get(Job, uuid.UUID(job["id"]))
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == job["id"]))
        assert stored is not None and stored.settlement_status.value == "RESERVED"
        assert event is not None
        assert event.idempotency_key == f"generation-job:{job['id']}:v1"
        assert event.sent_at is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(
                    LedgerTransaction.kind == "RESERVE",
                    LedgerTransaction.reference_id == job["id"],
                )
            )
            == 1
        )


def test_failed_reservation_creates_neither_job_nor_outbox(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr("app.api.drain_local_tasks", no_drain)
    shot = create_shot(client)
    grant(client, 1_000)
    item = quote(client, shot["id"])
    response = submit(client, shot["id"], item["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WALLET_INSUFFICIENT"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(LedgerTransaction.kind == "RESERVE")
            )
            == 0
        )


def test_failure_before_publish_retries_without_losing_job(client: TestClient, monkeypatch) -> None:
    job, _ = queued_generation(client, monkeypatch)

    class FailingLauncher:
        def start(self, workflow_key: str, payload: dict[str, str]) -> bool:
            raise ConnectionError("launcher unavailable")

    assert OutboxDispatcher(FailingLauncher()).dispatch_one() is True
    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == job["id"]))
        assert event is not None
        assert event.sent_at is None and event.locked_by is None
        assert event.attempt_count == 1
        assert "launcher unavailable" in (event.last_error or "")
        event.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert OutboxDispatcher().dispatch_one() is True
    run_local_workflow()
    refreshed = client.get(f"/v1/generations/{job['id']}").json()
    assert refreshed["status"] == "SUCCEEDED"
    assert refreshed["settlement_status"] == "SETTLED"


def test_crash_after_publish_reuses_same_workflow_key(client: TestClient, monkeypatch) -> None:
    job, _ = queued_generation(client, monkeypatch)

    def crash(_event: OutboxEvent) -> None:
        raise DispatcherCrash("process died after launcher accepted workflow")

    with pytest.raises(DispatcherCrash):
        OutboxDispatcher(after_publish=crash).dispatch_one()
    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == job["id"]))
        assert event is not None and event.sent_at is None and event.locked_by is not None
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        event.locked_until = utcnow() - timedelta(seconds=1)
        db.commit()
    assert OutboxDispatcher().dispatch_one() is True
    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == job["id"]))
        assert event is not None and event.sent_at is not None
        assert event.attempt_count == 2
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
    run_local_workflow()
    assert client.get(f"/v1/generations/{job['id']}").json()["status"] == "SUCCEEDED"


def test_concurrent_dispatchers_claim_event_once(client: TestClient, monkeypatch) -> None:
    queued_generation(client, monkeypatch)

    class RecordingLauncher:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.calls: list[str] = []

        def start(self, workflow_key: str, payload: dict[str, str]) -> bool:
            with self.lock:
                self.calls.append(workflow_key)
            return True

    launcher = RecordingLauncher()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: OutboxDispatcher(launcher).dispatch_one(), range(2)))
    assert sorted(results) == [False, True]
    assert len(launcher.calls) == 1
    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent))
        assert event is not None and event.sent_at is not None and event.attempt_count == 1
