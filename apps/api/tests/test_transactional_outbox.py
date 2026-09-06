import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.db import SessionLocal
from app.models import (
    GenerationJob,
    LedgerTransaction,
    OutboxEvent,
    OutboxStatus,
    Quote,
    QuoteStatus,
)
from app.outbox import DispatchResult, OutboxDispatcher, generation_workflow_key
from app.workflow import WorkflowStartRequest, WorkflowStartResult
from tests.test_mock_jobs import create_shot


@pytest.fixture
def client(raw_client: TestClient) -> TestClient:
    """Outbox tests inspect pending rows before any dispatcher runs."""

    return raw_client


class SimulatedCrash(BaseException):
    pass


class FakeWorkflowStarter:
    def __init__(self, *behaviors: str) -> None:
        self.behaviors = list(behaviors)
        self.calls: list[str] = []
        self.workflows: dict[str, WorkflowStartResult] = {}
        self._lock = threading.Lock()

    @property
    def created_count(self) -> int:
        return len(self.workflows)

    async def start(self, request: WorkflowStartRequest) -> WorkflowStartResult:
        with self._lock:
            self.calls.append(request.idempotency_key)
            existing = self.workflows.get(request.idempotency_key)
            if existing is not None:
                return existing
            behavior = self.behaviors.pop(0) if self.behaviors else "accepted"
            if behavior == "timeout":
                raise TimeoutError("starter timed out before acceptance was known")
            result = WorkflowStartResult(workflow_id=f"workflow-{len(self.workflows) + 1}")
            self.workflows[request.idempotency_key] = result
            if behavior == "accepted_then_timeout":
                raise TimeoutError("starter accepted but the response was lost")
            return result


@dataclass
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class CrashBeforeSendDispatcher(OutboxDispatcher):
    def after_claim(self, event) -> None:
        raise SimulatedCrash


class CrashAfterSendDispatcher(OutboxDispatcher):
    def after_start(self, event) -> None:
        raise SimulatedCrash


def create_pending_job(client: TestClient) -> dict:
    shot = create_shot(client)
    granted = client.post(
        "/v1/wallet/test-grants",
        json={
            "tier": "FAST",
            "amount_ms": 2_000,
            "idempotency_key": f"outbox-grant:{shot['id']}",
            "reason": "outbox test",
        },
    )
    assert granted.status_code == 201
    quoted = client.post(
        "/v1/quotes",
        json={"shot_id": shot["id"], "tier": "FAST", "resolution": "720P"},
    )
    assert quoted.status_code == 201
    generated = client.post(
        "/v1/generations",
        headers={"Idempotency-Key": f"outbox-generate:{shot['id']}"},
        json={"shot_id": shot["id"], "quote_id": quoted.json()["id"]},
    )
    assert generated.status_code == 202
    assert generated.json()["status"] == "QUEUED"
    return generated.json()


def load_outbox(job_id: str) -> OutboxEvent:
    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.job_id == uuid.UUID(job_id)))
        assert event is not None
        db.expunge(event)
        return event


def test_job_reservation_and_outbox_commit_together(client: TestClient) -> None:
    job = create_pending_job(client)
    with SessionLocal() as db:
        stored_job = db.get(GenerationJob, uuid.UUID(job["id"]))
        event = db.scalar(
            select(OutboxEvent).where(OutboxEvent.job_id == uuid.UUID(job["id"]))
        )
        assert stored_job is not None
        assert stored_job.reserved_tx_id is not None
        assert event is not None
        assert event.idempotency_key == generation_workflow_key(stored_job.id)
        assert event.status == OutboxStatus.PENDING

    shot = create_shot(client)
    quoted = client.post(
        "/v1/quotes",
        json={"shot_id": shot["id"], "tier": "FAST", "resolution": "720P"},
    )
    with SessionLocal() as db:
        reserve_count_before = db.scalar(
            select(func.count()).select_from(LedgerTransaction).where(
                LedgerTransaction.tx_type == "RESERVE"
            )
        )
    rejected = client.post(
        "/v1/generations",
        json={"shot_id": shot["id"], "quote_id": quoted.json()["id"]},
    )
    assert rejected.status_code == 409
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count()).select_from(GenerationJob).where(
                GenerationJob.shot_id == uuid.UUID(shot["id"])
            )
        ) == 0
        assert db.scalar(
            select(func.count()).select_from(OutboxEvent).join(GenerationJob).where(
                GenerationJob.shot_id == uuid.UUID(shot["id"])
            )
        ) == 0
        assert db.scalar(
            select(func.count()).select_from(LedgerTransaction).where(
                LedgerTransaction.tx_type == "RESERVE"
            )
        ) == reserve_count_before
        assert db.get(Quote, uuid.UUID(quoted.json()["id"])).status == QuoteStatus.OPEN


def test_timeout_and_duplicate_publish_reuse_one_business_workflow(client: TestClient) -> None:
    job = create_pending_job(client)
    starter = FakeWorkflowStarter("accepted_then_timeout")
    first = OutboxDispatcher(SessionLocal, starter, retry_delay=timedelta(0))
    assert asyncio.run(first.dispatch_once()) == DispatchResult.RETRY_SCHEDULED
    assert load_outbox(job["id"]).status == OutboxStatus.PENDING

    restarted = OutboxDispatcher(SessionLocal, starter)
    assert asyncio.run(restarted.dispatch_once()) == DispatchResult.PUBLISHED
    event = load_outbox(job["id"])
    assert event.status == OutboxStatus.PUBLISHED
    assert event.attempt_count == 2
    assert starter.created_count == 1
    assert starter.calls == [event.idempotency_key, event.idempotency_key]


@pytest.mark.parametrize(
    ("dispatcher_type", "created_before_restart"),
    [(CrashBeforeSendDispatcher, 0), (CrashAfterSendDispatcher, 1)],
)
def test_dispatcher_recovers_crash_windows_after_lease_expiry(
    client: TestClient,
    dispatcher_type: type[OutboxDispatcher],
    created_before_restart: int,
) -> None:
    job = create_pending_job(client)
    clock = MutableClock(datetime.now(UTC))
    starter = FakeWorkflowStarter()
    crashed = dispatcher_type(
        SessionLocal,
        starter,
        lease_duration=timedelta(seconds=10),
        clock=clock,
    )
    with pytest.raises(SimulatedCrash):
        asyncio.run(crashed.dispatch_once())
    assert load_outbox(job["id"]).status == OutboxStatus.PROCESSING
    assert starter.created_count == created_before_restart

    clock.advance(timedelta(seconds=11))
    restarted = OutboxDispatcher(
        SessionLocal,
        starter,
        lease_duration=timedelta(seconds=10),
        clock=clock,
    )
    assert asyncio.run(restarted.dispatch_once()) == DispatchResult.PUBLISHED
    assert load_outbox(job["id"]).status == OutboxStatus.PUBLISHED
    assert starter.created_count == 1


def test_concurrent_dispatchers_claim_an_event_once(client: TestClient) -> None:
    job = create_pending_job(client)
    starter = FakeWorkflowStarter()
    dispatchers = [OutboxDispatcher(SessionLocal, starter) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda dispatcher: asyncio.run(dispatcher.dispatch_once()), dispatchers)
        )

    assert sorted(results) == sorted([DispatchResult.IDLE, DispatchResult.PUBLISHED])
    assert starter.created_count == 1
    assert load_outbox(job["id"]).status == OutboxStatus.PUBLISHED


def test_lifecycle_dispatcher_drains_backlog_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    results = iter([DispatchResult.PUBLISHED, DispatchResult.IDLE])
    calls = 0

    async def dispatch() -> DispatchResult:
        nonlocal calls
        calls += 1
        result = next(results)
        if result == DispatchResult.IDLE:
            stop.set()
        return result

    monkeypatch.setattr(main_module, "dispatch_generation_outbox", dispatch)
    asyncio.run(main_module.outbox_dispatcher_loop(stop))
    assert calls == 2


pytestmark = pytest.mark.database
