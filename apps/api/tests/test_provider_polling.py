import asyncio
import uuid
from datetime import UTC, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobEvent,
    JobStatus,
    LedgerTransaction,
    SettlementStatus,
)
from app.provider_execution import (
    GenerationExecutionService,
    ProviderPollingPolicy,
)
from app.routing import (
    SIMULATED_RUNPOD_ROUTE_VERSION,
    get_route_registry,
)
from app.simulators import (
    DeterministicRunPodSimulator,
    FaultInjector,
    FaultPoint,
    ManualClock,
)
from app.storage import LocalObjectStorage
from tests.test_provider_routing import _quoted_shot


def _create_simulated_job(client: TestClient, *, mode: str = "success") -> uuid.UUID:
    shot, quote, _ = _quoted_shot(client, with_reference=True)
    created = client.post(
        "/v1/generations",
        headers={"x-test-generation-modes": mode},
        json={"shot_id": shot["id"], "quote_id": quote["id"]},
    )
    assert created.status_code == 202
    return uuid.UUID(created.json()["id"])


def _attempt_clock(job_id: uuid.UUID) -> ManualClock:
    with SessionLocal() as db:
        attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
        assert attempt is not None
        created_at = attempt.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return ManualClock(created_at)


def _executor(
    provider: DeterministicRunPodSimulator,
    clock: ManualClock,
    *,
    maximum_polls: int = 10,
    deadline: timedelta = timedelta(minutes=1),
) -> GenerationExecutionService:
    settings = get_settings()
    return GenerationExecutionService(
        LocalObjectStorage(
            settings.storage_root,
            settings.storage_claim_secret.get_secret_value().encode(),
        ),
        provider=provider,
        polling_policy=ProviderPollingPolicy(
            initial_delay=timedelta(seconds=1),
            maximum_delay=timedelta(seconds=2),
            deadline=deadline,
            maximum_polls=maximum_polls,
        ),
        clock=clock,
    )


def test_polling_policy_uses_deterministic_capped_backoff() -> None:
    policy = ProviderPollingPolicy(
        initial_delay=timedelta(seconds=1),
        maximum_delay=timedelta(seconds=4),
    )

    assert [policy.delay_after(count) for count in range(1, 6)] == [
        timedelta(seconds=1),
        timedelta(seconds=2),
        timedelta(seconds=4),
        timedelta(seconds=4),
        timedelta(seconds=4),
    ]


def test_queued_job_recovers_from_database_without_duplicate_submit_or_settlement(
    raw_client: TestClient,
) -> None:
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
    try:
        job_id = _create_simulated_job(raw_client)
        clock = _attempt_clock(job_id)
        provider = DeterministicRunPodSimulator(clock=clock)

        first = asyncio.run(_executor(provider, clock).execute(job_id))
        assert not first.is_complete
        assert (first.poll_count, first.retry_after) == (1, timedelta(seconds=1))

        clock.advance(timedelta(seconds=2))
        restarted = _executor(provider, clock)
        second = asyncio.run(restarted.execute(job_id))
        assert not second.is_complete
        assert (second.poll_count, second.retry_after) == (2, timedelta(seconds=2))

        clock.advance(timedelta(seconds=3))
        completed = asyncio.run(restarted.execute(job_id))
        replayed = asyncio.run(_executor(provider, clock).execute(job_id))
        assert completed.is_complete
        assert replayed.is_complete
        assert len(provider.jobs()) == 1

        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            assert job is not None
            assert job.status == JobStatus.SUCCEEDED
            assert job.settlement_status == SettlementStatus.SETTLED
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(GenerationAttempt)
                    .where(GenerationAttempt.job_id == job_id)
                )
                == 1
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(GenerationOutput)
                    .where(GenerationOutput.job_id == job_id)
                )
                == 1
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(LedgerTransaction)
                    .where(
                        LedgerTransaction.reference_id == str(job_id),
                        LedgerTransaction.tx_type == "SETTLE",
                    )
                )
                == 1
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(JobEvent)
                    .where(
                        JobEvent.attempt_id == job.attempts[0].id,
                        JobEvent.event_type == "provider.poll_started",
                    )
                )
                == 3
            )
    finally:
        routes.activate(previous)


def test_submit_outcome_unknown_is_reconciled_after_restart_without_resubmit(
    raw_client: TestClient,
) -> None:
    class OutcomeUnknownSimulator(DeterministicRunPodSimulator):
        def __init__(self, *, clock: ManualClock, faults: FaultInjector) -> None:
            super().__init__(clock=clock, faults=faults)
            self.submit_calls = 0

        async def submit(self, request):
            self.submit_calls += 1
            result = await super().submit(request)
            if self.submit_calls == 1:
                raise TimeoutError("submit response lost after provider acceptance")
            return result

    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
    try:
        job_id = _create_simulated_job(raw_client)
        clock = _attempt_clock(job_id)
        faults = FaultInjector()
        faults.fail_next(FaultPoint.RUNPOD_POLL, TimeoutError("process interrupted"))
        provider = OutcomeUnknownSimulator(clock=clock, faults=faults)

        interrupted = asyncio.run(_executor(provider, clock).execute(job_id))
        assert not interrupted.is_complete
        with SessionLocal() as db:
            attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
            assert attempt is not None
            assert attempt.status.value == "SUBMITTING"
            assert attempt.provider_job_id is None

        recovered = asyncio.run(_executor(provider, clock).execute(job_id))
        assert not recovered.is_complete
        assert provider.submit_calls == 1
        assert len(provider.jobs()) == 1
        with SessionLocal() as db:
            attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
            assert attempt is not None
            assert attempt.status.value == "RUNNING"
            assert attempt.provider_job_id == provider.jobs()[0].provider_job_id

        clock.advance(timedelta(seconds=5))
        completed = asyncio.run(_executor(provider, clock).execute(job_id))
        replayed = asyncio.run(_executor(provider, clock).execute(job_id))
        assert completed.is_complete
        assert replayed.is_complete
        assert provider.submit_calls == 1
        assert len(provider.jobs()) == 1
        with SessionLocal() as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(GenerationOutput)
                    .where(GenerationOutput.job_id == job_id)
                )
                == 1
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(LedgerTransaction)
                    .where(
                        LedgerTransaction.reference_id == str(job_id),
                        LedgerTransaction.tx_type == "SETTLE",
                    )
                )
                == 1
            )
    finally:
        routes.activate(previous)


def test_poll_budget_timeout_finishes_after_bounded_attempt_retries(
    raw_client: TestClient,
) -> None:
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
    try:
        job_id = _create_simulated_job(raw_client)
        clock = _attempt_clock(job_id)
        provider = DeterministicRunPodSimulator(clock=clock)
        executor = _executor(provider, clock, maximum_polls=1)

        for _ in range(4):
            result = asyncio.run(executor.execute(job_id))
            if result.is_complete:
                break
        else:
            raise AssertionError("polling did not terminate within its bounded retry budget")

        replayed = asyncio.run(executor.execute(job_id))
        assert replayed.is_complete
        assert len(provider.jobs()) == 2
        assert len({job.idempotency_key for job in provider.jobs()}) == 2
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            assert job is not None
            assert job.status == JobStatus.FAILED_FINAL
            assert job.settlement_status == SettlementStatus.RELEASED
            attempts = db.scalars(
                select(GenerationAttempt)
                .where(GenerationAttempt.job_id == job_id)
                .order_by(GenerationAttempt.attempt_no)
            ).all()
            assert len(attempts) == 2
            assert all(attempt.status.value == "TIMED_OUT" for attempt in attempts)
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(LedgerTransaction)
                    .where(
                        LedgerTransaction.reference_id == str(job_id),
                        LedgerTransaction.tx_type == "RELEASE",
                    )
                )
                == 1
            )
    finally:
        routes.activate(previous)
