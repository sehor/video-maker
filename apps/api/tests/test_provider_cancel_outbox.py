import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.bootstrap as public_api_module
from app.db import SessionLocal
from app.models import (
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobStatus,
    OutboxEvent,
    OutboxStatus,
    SettlementStatus,
)
from app.outbox import DispatchResult
from app.provider import (
    CancelResult,
    FailureCode,
    MockVideoProvider,
    ProviderAttempt,
    ProviderStatus,
    provider_cancel_key,
)
from app.provider_cancel_outbox import (
    PROVIDER_CANCEL_REQUESTED,
    ClaimedProviderCancelEvent,
    ProviderCancelDispatcher,
    ProviderCancelRequest,
)
from app.provider_execution import GenerationExecutionService
from tests.test_provider_webhooks import (
    LateSuccessProvider,
    PendingProvider,
    ledger_count,
    object_storage,
    request_for,
    running_job,
    stored_provider_job_id,
    webhook_body,
)


class SimulatedCrash(BaseException):
    pass


@dataclass
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class IdempotentCancelWorker:
    def __init__(self, *behaviors: str) -> None:
        self.behaviors = list(behaviors)
        self.calls: list[str] = []
        self.effects: set[str] = set()
        self._lock = threading.Lock()

    async def __call__(self, request: ProviderCancelRequest) -> None:
        with self._lock:
            self.calls.append(request.idempotency_key)
            behavior = self.behaviors.pop(0) if self.behaviors else "accepted"
            if behavior == "transient_failure":
                raise TimeoutError("provider unavailable before acceptance")
            self.effects.add(request.idempotency_key)
            if behavior == "accepted_then_timeout":
                raise TimeoutError("provider accepted cancel but response was lost")


class CrashBeforeCancelDispatcher(ProviderCancelDispatcher):
    def after_claim(self, event: ClaimedProviderCancelEvent) -> None:
        raise SimulatedCrash


class CrashAfterCancelDispatcher(ProviderCancelDispatcher):
    def after_cancel(self, event: ClaimedProviderCancelEvent) -> None:
        raise SimulatedCrash


def request_cancel(raw_client: TestClient, job_id: str, key: str = "cancel-request") -> dict:
    response = raw_client.post(
        f"/v1/generations/{job_id}/cancel",
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCEL_REQUESTED"
    return response.json()


def load_cancel_outbox(job_id: str) -> OutboxEvent:
    with SessionLocal() as db:
        event = db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.job_id == uuid.UUID(job_id),
                OutboxEvent.event_type == PROVIDER_CANCEL_REQUESTED,
            )
        )
        assert event is not None
        db.expunge(event)
        return event


def test_cancel_status_and_unique_outbox_commit_together(raw_client: TestClient) -> None:
    running, _ = running_job(raw_client, PendingProvider())

    first = request_cancel(raw_client, running["id"], "cancel-once")
    replay = request_cancel(raw_client, running["id"], "cancel-again")

    assert first["settlement_status"] == replay["settlement_status"] == "RESERVED"
    with SessionLocal() as db:
        job = db.get(GenerationJob, uuid.UUID(running["id"]))
        attempt = db.scalar(
            select(GenerationAttempt)
            .where(GenerationAttempt.job_id == uuid.UUID(running["id"]))
            .order_by(GenerationAttempt.attempt_no.desc())
            .limit(1)
        )
        events = list(
            db.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.job_id == uuid.UUID(running["id"]),
                    OutboxEvent.event_type == PROVIDER_CANCEL_REQUESTED,
                )
            )
        )
        assert job is not None and attempt is not None
        assert job.status == JobStatus.CANCEL_REQUESTED
        assert job.settlement_status == SettlementStatus.RESERVED
        assert len(events) == 1
        assert events[0].attempt_id == attempt.id
        assert events[0].idempotency_key == provider_cancel_key(attempt.id)
        assert events[0].status == OutboxStatus.PENDING
        assert events[0].attempt_count == 0
        assert events[0].last_error is None
    assert ledger_count(running["id"], "SETTLE") == 0
    assert ledger_count(running["id"], "RELEASE") == 0


def test_concurrent_cancel_requests_create_one_outbox(raw_client: TestClient) -> None:
    running, _ = running_job(raw_client, PendingProvider())

    def cancel(key: str) -> tuple[int, str]:
        response = raw_client.post(
            f"/v1/generations/{running['id']}/cancel",
            headers={"Idempotency-Key": key},
        )
        return response.status_code, response.json()["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(cancel, ("cancel-concurrent-a", "cancel-concurrent-b")))

    assert results == [(200, "CANCEL_REQUESTED"), (200, "CANCEL_REQUESTED")]
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.job_id == uuid.UUID(running["id"]),
                    OutboxEvent.event_type == PROVIDER_CANCEL_REQUESTED,
                )
            )
            == 1
        )


def test_claim_crash_is_recovered_after_lease_expiry(raw_client: TestClient) -> None:
    running, _ = running_job(raw_client, PendingProvider())
    request_cancel(raw_client, running["id"])
    clock = MutableClock(datetime.now(UTC))
    worker = IdempotentCancelWorker()

    with pytest.raises(SimulatedCrash):
        asyncio.run(
            CrashBeforeCancelDispatcher(
                SessionLocal,
                worker,
                lease_duration=timedelta(seconds=10),
                clock=clock,
            ).dispatch_once()
        )
    claimed = load_cancel_outbox(running["id"])
    assert claimed.status == OutboxStatus.PROCESSING
    assert claimed.attempt_count == 1
    assert worker.calls == []

    clock.advance(timedelta(seconds=11))
    result = asyncio.run(
        ProviderCancelDispatcher(
            SessionLocal,
            worker,
            lease_duration=timedelta(seconds=10),
            clock=clock,
        ).dispatch_once()
    )
    recovered = load_cancel_outbox(running["id"])
    assert result == DispatchResult.PUBLISHED
    assert recovered.status == OutboxStatus.PUBLISHED
    assert recovered.attempt_count == 2
    assert worker.calls == [recovered.idempotency_key]


def test_provider_acceptance_with_lost_response_reuses_stable_key(
    raw_client: TestClient,
) -> None:
    running, _ = running_job(raw_client, PendingProvider())
    request_cancel(raw_client, running["id"])
    clock = MutableClock(datetime.now(UTC))
    worker = IdempotentCancelWorker("accepted_then_timeout", "accepted")
    dispatcher = ProviderCancelDispatcher(
        SessionLocal,
        worker,
        retry_delay=timedelta(seconds=5),
        clock=clock,
    )

    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.RETRY_SCHEDULED
    retry = load_cancel_outbox(running["id"])
    assert retry.status == OutboxStatus.PENDING
    assert retry.attempt_count == 1
    retry_at = retry.next_attempt_at
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    assert retry_at == clock.now + timedelta(seconds=5)
    assert retry.last_error == "TimeoutError: provider accepted cancel but response was lost"

    clock.advance(timedelta(seconds=5))
    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.PUBLISHED
    published = load_cancel_outbox(running["id"])
    assert published.status == OutboxStatus.PUBLISHED
    assert published.attempt_count == 2
    assert worker.calls == [published.idempotency_key, published.idempotency_key]
    assert worker.effects == {published.idempotency_key}


def test_dispatcher_renews_lease_during_provider_call(raw_client: TestClient) -> None:
    running, _ = running_job(raw_client, PendingProvider())
    request_cancel(raw_client, running["id"])
    clock = MutableClock(datetime.now(UTC))
    worker_started = asyncio.Event()
    allow_worker = asyncio.Event()

    async def worker(request: ProviderCancelRequest) -> None:
        worker_started.set()
        await allow_worker.wait()

    async def exercise() -> tuple[DispatchResult, datetime | None]:
        dispatcher = ProviderCancelDispatcher(
            SessionLocal,
            worker,
            clock=clock,
            lease_duration=timedelta(seconds=30),
            lease_renew_interval_seconds=0.01,
        )
        task = asyncio.create_task(dispatcher.dispatch_once())
        await asyncio.wait_for(worker_started.wait(), timeout=5)
        clock.advance(timedelta(seconds=20))
        await asyncio.sleep(0.03)
        with SessionLocal() as db:
            locked_at = db.scalar(
                select(OutboxEvent.locked_at).where(
                    OutboxEvent.job_id == uuid.UUID(running["id"]),
                    OutboxEvent.event_type == PROVIDER_CANCEL_REQUESTED,
                )
            )
        allow_worker.set()
        return await task, locked_at

    result, renewed_at = asyncio.run(exercise())
    assert result == DispatchResult.PUBLISHED
    assert renewed_at is not None
    # SQLite returns naive timestamps; PostgreSQL preserves the UTC offset.
    if renewed_at.tzinfo is None:
        renewed_at = renewed_at.replace(tzinfo=UTC)
    assert renewed_at == clock.now


def test_temporary_cancel_failures_retry_until_success(raw_client: TestClient) -> None:
    running, _ = running_job(raw_client, PendingProvider())
    request_cancel(raw_client, running["id"])
    clock = MutableClock(datetime.now(UTC))
    worker = IdempotentCancelWorker("transient_failure", "transient_failure", "accepted")
    dispatcher = ProviderCancelDispatcher(
        SessionLocal,
        worker,
        retry_delay=timedelta(seconds=2),
        clock=clock,
    )

    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.RETRY_SCHEDULED
    clock.advance(timedelta(seconds=2))
    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.RETRY_SCHEDULED
    clock.advance(timedelta(seconds=2))
    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.PUBLISHED

    published = load_cancel_outbox(running["id"])
    assert published.attempt_count == 3
    assert published.last_error is None
    assert len(worker.calls) == 3
    assert worker.effects == {published.idempotency_key}


def test_crash_after_provider_call_replays_without_duplicate_effect(
    raw_client: TestClient,
) -> None:
    running, _ = running_job(raw_client, PendingProvider())
    request_cancel(raw_client, running["id"])
    clock = MutableClock(datetime.now(UTC))
    worker = IdempotentCancelWorker()

    with pytest.raises(SimulatedCrash):
        asyncio.run(
            CrashAfterCancelDispatcher(
                SessionLocal,
                worker,
                lease_duration=timedelta(seconds=10),
                clock=clock,
            ).dispatch_once()
        )
    clock.advance(timedelta(seconds=11))
    assert (
        asyncio.run(
            ProviderCancelDispatcher(
                SessionLocal,
                worker,
                lease_duration=timedelta(seconds=10),
                clock=clock,
            ).dispatch_once()
        )
        == DispatchResult.PUBLISHED
    )
    event = load_cancel_outbox(running["id"])
    assert event.attempt_count == 2
    assert worker.calls == [event.idempotency_key, event.idempotency_key]
    assert worker.effects == {event.idempotency_key}


class RacingCancelProvider(PendingProvider):
    def __init__(self) -> None:
        super().__init__(webhook_secret="test-webhook-secret")
        self.cancel_started = asyncio.Event()
        self.allow_cancel = asyncio.Event()
        self.succeeded = False
        self.cancel_keys: list[str] = []

    async def poll(self, attempt: ProviderAttempt):
        if self.succeeded:
            return await MockVideoProvider.poll(self, attempt)
        return await super().poll(attempt)

    async def cancel(self, attempt: ProviderAttempt) -> CancelResult:
        self.cancel_keys.append(attempt.idempotency_key)
        self.cancel_started.set()
        await self.allow_cancel.wait()
        return CancelResult(accepted=True, status=ProviderStatus.CANCELLED)


def test_cancel_and_success_race_settles_only_once(raw_client: TestClient) -> None:
    provider = RacingCancelProvider()
    running, executor = running_job(raw_client, provider)
    request_cancel(raw_client, running["id"])

    async def worker(request: ProviderCancelRequest) -> None:
        await executor.request_cancel(request.job_id, request.attempt_id)

    dispatcher = ProviderCancelDispatcher(SessionLocal, worker)

    async def race() -> None:
        dispatch = asyncio.create_task(dispatcher.dispatch_once())
        await asyncio.wait_for(provider.cancel_started.wait(), timeout=5)
        provider.succeeded = True
        body = webhook_body(
            "evt-success-wins-cancel-race",
            stored_provider_job_id(running),
            ProviderStatus.SUCCEEDED,
        )
        await executor.handle_webhook("mock", request_for(body))
        provider.allow_cancel.set()
        assert await dispatch == DispatchResult.PUBLISHED

    asyncio.run(race())

    with SessionLocal() as db:
        job = db.get(GenerationJob, uuid.UUID(running["id"]))
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED
        assert job.settlement_status == SettlementStatus.SETTLED
        assert (
            db.scalar(
                select(func.count())
                .select_from(GenerationOutput)
                .where(GenerationOutput.job_id == job.id)
            )
            == 1
        )
    event = load_cancel_outbox(running["id"])
    assert event.status == OutboxStatus.PUBLISHED
    assert provider.cancel_keys == [event.idempotency_key]
    assert ledger_count(running["id"], "SETTLE") == 1
    assert ledger_count(running["id"], "RELEASE") == 0


def test_cancel_wins_and_late_success_cannot_settle(
    raw_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = MockVideoProvider(webhook_secret="test-webhook-secret")
    late_success_provider = LateSuccessProvider(webhook_secret="test-webhook-secret")
    running, executor = running_job(raw_client, late_success_provider)
    request_cancel(raw_client, running["id"])
    cancel_executor = GenerationExecutionService(object_storage(), provider=provider)
    monkeypatch.setattr(
        public_api_module,
        "provider_executor",
        lambda _: cancel_executor,
    )
    assert asyncio.run(public_api_module.dispatch_provider_cancel_outbox()) == (
        DispatchResult.PUBLISHED
    )
    late_success_provider.succeeded = True
    body = webhook_body(
        "evt-late-success-after-cancel-outbox",
        stored_provider_job_id(running),
        ProviderStatus.SUCCEEDED,
    )
    asyncio.run(executor.handle_webhook("mock", request_for(body)))

    with SessionLocal() as db:
        job = db.get(GenerationJob, uuid.UUID(running["id"]))
        assert job is not None
        assert job.status == JobStatus.CANCELLED
        assert job.settlement_status == SettlementStatus.RELEASED
        assert (
            db.scalar(
                select(func.count())
                .select_from(GenerationOutput)
                .where(GenerationOutput.job_id == job.id)
            )
            == 0
        )
    assert ledger_count(running["id"], "SETTLE") == 0
    assert ledger_count(running["id"], "RELEASE") == 1


def test_final_failure_and_late_cancel_after_request_release_once(
    raw_client: TestClient,
) -> None:
    provider = PendingProvider(webhook_secret="test-webhook-secret")
    running, executor = running_job(raw_client, provider)
    request_cancel(raw_client, running["id"])
    assert (
        asyncio.run(
            ProviderCancelDispatcher(SessionLocal, IdempotentCancelWorker()).dispatch_once()
        )
        == DispatchResult.PUBLISHED
    )
    provider_job_id = stored_provider_job_id(running)
    failed = webhook_body(
        "evt-final-failure-after-cancel-request",
        provider_job_id,
        ProviderStatus.FAILED,
        failure_code=FailureCode.WORKFLOW_FAILED.value,
    )
    cancelled = webhook_body(
        "evt-late-cancel-after-final-failure",
        provider_job_id,
        ProviderStatus.CANCELLED,
    )
    asyncio.run(executor.handle_webhook("mock", request_for(failed)))
    asyncio.run(executor.handle_webhook("mock", request_for(cancelled)))

    with SessionLocal() as db:
        job = db.get(GenerationJob, uuid.UUID(running["id"]))
        assert job is not None
        assert job.status == JobStatus.CANCELLED
        assert job.settlement_status == SettlementStatus.RELEASED
    assert ledger_count(running["id"], "SETTLE") == 0
    assert ledger_count(running["id"], "RELEASE") == 1


pytestmark = pytest.mark.database
