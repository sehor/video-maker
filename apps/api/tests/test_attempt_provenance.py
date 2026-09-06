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
    JobStatus,
    LedgerTransaction,
    SettlementStatus,
)
from app.provider import CostResult, CostSource, ProviderMetrics
from app.routing import SIMULATED_RUNPOD_ROUTE_VERSION, get_route_registry
from app.simulators import DeterministicRunPodSimulator, SimulatedRunPodMetrics
from tests.test_provider_polling import (
    _attempt_clock,
    _create_simulated_job,
    _executor,
)


def _complete_simulated_job(
    client: TestClient,
    *,
    metrics: SimulatedRunPodMetrics | None = None,
) -> tuple[uuid.UUID, DeterministicRunPodSimulator]:
    job_id = _create_simulated_job(client)
    clock = _attempt_clock(job_id)
    provider = DeterministicRunPodSimulator(clock=clock, metrics=metrics)
    executor = _executor(provider, clock)

    assert not asyncio.run(executor.execute(job_id)).is_complete
    clock.advance(timedelta(seconds=2))
    assert not asyncio.run(executor.execute(job_id)).is_complete
    clock.advance(timedelta(seconds=3))
    assert asyncio.run(executor.execute(job_id)).is_complete
    return job_id, provider


def test_output_keeps_complete_attempt_snapshot_and_replay_does_not_resettle(
    raw_client: TestClient,
) -> None:
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
    try:
        job_id, provider = _complete_simulated_job(raw_client)
        with SessionLocal() as db:
            attempt = db.scalar(
                select(GenerationAttempt).where(GenerationAttempt.job_id == job_id)
            )
            output = db.scalar(
                select(GenerationOutput).where(GenerationOutput.job_id == job_id)
            )
            assert attempt is not None
            assert output is not None
            assert output.attempt_id == attempt.id
            assert attempt.image_digest == provider.jobs()[0].provenance.image_digest
            assert attempt.worker_version == "worker-comfyui-simulator/1.0.0"
            assert attempt.worker_commit == "a1981e99b1f5a7201f387653420ad1f275b97d0a"
            assert attempt.comfyui_commit == "a8c44f9b2a0678ac4082e3529a3f43db7472acfe"
            assert attempt.workflow_hash == (
                "454f03238c881b751529524c1db47c16619cb0dce13cb7c694f55653acd35fad"
            )
            assert attempt.model_hashes_json == dict(provider.jobs()[0].provenance.model_sha256)
            assert (
                attempt.queue_ms,
                attempt.cold_start_ms,
                attempt.runtime_ms,
                attempt.billable_ms,
            ) == (2_000, 500, 3_000, 3_500)
            assert (attempt.cost_minor, attempt.cost_currency, attempt.cost_source) == (
                7,
                "USD",
                "SIMULATED",
            )
            snapshot = {
                "image_digest": attempt.image_digest,
                "worker_commit": attempt.worker_commit,
                "model_hashes_json": attempt.model_hashes_json,
                "raw_metrics_json": attempt.raw_metrics_json,
                "cost_minor": attempt.cost_minor,
                "cost_source": attempt.cost_source,
            }

        clock = _attempt_clock(job_id)
        replayed = asyncio.run(_executor(provider, clock).execute(job_id))
        assert replayed.is_complete
        with SessionLocal() as db:
            attempt = db.scalar(
                select(GenerationAttempt).where(GenerationAttempt.job_id == job_id)
            )
            assert attempt is not None
            assert {
                "image_digest": attempt.image_digest,
                "worker_commit": attempt.worker_commit,
                "model_hashes_json": attempt.model_hashes_json,
                "raw_metrics_json": attempt.raw_metrics_json,
                "cost_minor": attempt.cost_minor,
                "cost_source": attempt.cost_source,
            } == snapshot
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


def test_simulated_provider_cannot_persist_actual_cost(raw_client: TestClient) -> None:
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
    try:
        job_id, _ = _complete_simulated_job(
            raw_client,
            metrics=SimulatedRunPodMetrics(cost_source=CostSource.ACTUAL),
        )
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            attempt = db.scalar(
                select(GenerationAttempt).where(GenerationAttempt.job_id == job_id)
            )
            assert job is not None
            assert attempt is not None
            assert job.status == JobStatus.FAILED_FINAL
            assert job.settlement_status == SettlementStatus.RELEASED
            assert attempt.cost_source is None
            assert db.scalar(
                select(func.count())
                .select_from(GenerationOutput)
                .where(GenerationOutput.job_id == job_id)
            ) == 0
    finally:
        routes.activate(previous)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CostResult(amount_minor=0.1, currency="USD", source=CostSource.ESTIMATE),
        lambda: ProviderMetrics(
            gpu_type="GPU",
            queue_ms=0.1,
            cold_start_ms=0,
            runtime_ms=1,
            billable_ms=1,
        ),
    ],
)
def test_cost_and_timing_contracts_reject_floats(factory) -> None:
    with pytest.raises(ValueError):
        factory()


pytestmark = pytest.mark.database
