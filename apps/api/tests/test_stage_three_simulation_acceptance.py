import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import (
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    LedgerTransaction,
)
from app.routing import SIMULATED_RUNPOD_ROUTE_VERSION, get_route_registry
from app.simulators import DeterministicRunPodSimulator
from tests.test_provider_polling import (
    _attempt_clock,
    _create_simulated_job,
    _executor,
)


class CountingSimulator(DeterministicRunPodSimulator):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.submit_calls = 0

    async def submit(self, request):
        self.submit_calls += 1
        return await super().submit(request)


def run_with_restarts(
    provider: CountingSimulator,
    clock,
    job_id: uuid.UUID,
) -> None:
    """Treat every deterministic polling step as a fresh process."""

    for _ in range(8):
        step = asyncio.run(_executor(provider, clock).execute(job_id))
        if step.is_complete:
            return
        clock.advance(timedelta(seconds=5))
    raise AssertionError("simulated execution did not reach a terminal state")


def counts(job_id: uuid.UUID) -> tuple[int, int, int, int]:
    with SessionLocal() as db:
        attempts = db.scalar(
            select(func.count())
            .select_from(GenerationAttempt)
            .where(GenerationAttempt.job_id == job_id)
        )
        outputs = db.scalar(
            select(func.count())
            .select_from(GenerationOutput)
            .where(GenerationOutput.job_id == job_id)
        )
        settlements = db.scalar(
            select(func.count())
            .select_from(LedgerTransaction)
            .where(
                LedgerTransaction.reference_id == str(job_id),
                LedgerTransaction.tx_type == "SETTLE",
            )
        )
        releases = db.scalar(
            select(func.count())
            .select_from(LedgerTransaction)
            .where(
                LedgerTransaction.reference_id == str(job_id),
                LedgerTransaction.tx_type == "RELEASE",
            )
        )
    return attempts or 0, outputs or 0, settlements or 0, releases or 0


@pytest.mark.parametrize(
    (
        "mode",
        "expected_status",
        "expected_settlement",
        "expected_counts",
        "expected_submits",
    ),
    [
        ("success", "SUCCEEDED", "SETTLED", (1, 1, 1, 0), 1),
        ("failure", "FAILED_FINAL", "RELEASED", (1, 0, 0, 1), 1),
        ("timeout", "FAILED_FINAL", "RELEASED", (2, 0, 0, 1), 2),
    ],
)
def test_terminal_matrix_survives_restart_and_replay_without_duplicate_effects(
    raw_client: TestClient,
    mode: str,
    expected_status: str,
    expected_settlement: str,
    expected_counts: tuple[int, int, int, int],
    expected_submits: int,
) -> None:
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
    try:
        job_id = _create_simulated_job(raw_client, mode=mode)
        clock = _attempt_clock(job_id)
        provider = CountingSimulator(clock=clock)

        run_with_restarts(provider, clock, job_id)
        replayed = asyncio.run(_executor(provider, clock).execute(job_id))
        assert replayed.is_complete

        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            assert job is not None
            assert job.status.value == expected_status
            assert job.settlement_status.value == expected_settlement
        assert counts(job_id) == expected_counts
        assert provider.submit_calls == expected_submits
        assert len(provider.jobs()) == expected_submits
    finally:
        routes.activate(previous)


def test_cancel_and_replay_release_once_without_publishing(
    raw_client: TestClient,
) -> None:
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
    try:
        job_id = _create_simulated_job(raw_client)
        clock = _attempt_clock(job_id)
        provider = CountingSimulator(clock=clock)
        executor = _executor(provider, clock)

        started = asyncio.run(executor.execute(job_id))
        assert not started.is_complete
        asyncio.run(_executor(provider, clock).request_cancel(job_id))
        replayed = asyncio.run(_executor(provider, clock).execute(job_id))
        assert replayed.is_complete

        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            assert job is not None
            assert job.status.value == "CANCELLED"
            assert job.settlement_status.value == "RELEASED"
        assert counts(job_id) == (1, 0, 0, 1)
        assert provider.submit_calls == 1
        assert len(provider.jobs()) == 1
    finally:
        routes.activate(previous)
