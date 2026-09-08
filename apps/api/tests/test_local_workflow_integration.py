import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import bootstrap as public_api
from app import main
from app.config import get_settings
from app.control_plane import ControlPlaneReconciler
from app.db import SessionLocal
from app.local_workflow import LocalWorkflowStarter
from app.models import GenerationJob, LedgerTransaction, OutboxEvent, OutboxStatus
from app.outbox import DispatchResult, OutboxDispatcher
from app.provider_execution import GenerationExecutionService
from app.provider_polling import ProviderPollingPolicy
from app.routing import MOCK_ROUTE_VERSION, SIMULATED_RUNPOD_ROUTE_VERSION, get_route_registry
from app.storage import LocalObjectStorage
from tests.test_provider_routing import _quoted_shot


@pytest.fixture
def background_client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "outbox_dispatcher_enabled", True)
    monkeypatch.setattr(settings, "reconciler_enabled", False)
    monkeypatch.setattr(settings, "outbox_poll_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "storage_root", settings.storage_root / "path with spaces")
    runner = LocalWorkflowStarter(settings)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "workflow_starter", runner)
    monkeypatch.setattr(public_api, "workflow_starter", runner)
    with TestClient(main.app) as client:
        yield client, runner
    assert not runner.ready()


@pytest.mark.parametrize("route", [MOCK_ROUTE_VERSION, SIMULATED_RUNPOD_ROUTE_VERSION])
def test_background_dispatch_completes_job_output_and_single_settlement(background_client, route):
    client, runner = background_client
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(route)
    try:
        assert client.get("/readyz").json()["checks"]["workflow"] is True
        shot, quote, _ = _quoted_shot(
            client, with_reference=route == SIMULATED_RUNPOD_ROUTE_VERSION
        )
        response = client.post(
            "/v1/generations", json={"shot_id": shot["id"], "quote_id": quote["id"]}
        )
        assert response.status_code == 202
        job_id = uuid.UUID(response.json()["id"])
        assert client.portal is not None

        async def wait_for_published_run():
            # The API's real lifespan dispatcher must accept the outbox; this test
            # never calls dispatch_once or GenerationExecutionService.execute itself.
            async with asyncio.timeout(20):
                while True:
                    with SessionLocal() as db:
                        event = db.scalar(select(OutboxEvent).where(OutboxEvent.job_id == job_id))
                        if event.status == OutboxStatus.PUBLISHED:
                            assert event.workflow_id.startswith("local:")
                            break
                    await asyncio.sleep(0.01)
                await runner.wait_idle()
                await public_api.reconcile_generation_job(job_id)
                await runner.wait_idle()

        client.portal.call(wait_for_published_run)
        job = client.get(f"/v1/generations/{job_id}").json()
        assert job["status"] == "SUCCEEDED"
        assert len(job["outputs"]) == 1
        assert job["final_output_id"] == job["outputs"][0]["id"]
        media = client.get(f"/v1/outputs/{job['final_output_id']}/content")
        assert media.status_code == 200 and media.content[4:8] == b"ftyp"
        assert client.get("/v1/wallet").json()["balances"]["FAST_MS"] == {
            "USER_AVAILABLE": 5_000, "USER_RESERVED": 0,
        }
        with SessionLocal() as db:
            assert db.scalar(
                select(func.count()).select_from(LedgerTransaction).where(
                    LedgerTransaction.reference_id == str(job_id),
                    LedgerTransaction.tx_type == "SETTLE",
                )
            ) == 1
    finally:
        routes.activate(previous)


def test_reconciler_resumes_published_work_after_runner_shutdown(raw_client, monkeypatch):
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
    try:
        shot, quote, _ = _quoted_shot(raw_client, with_reference=True)
        response = raw_client.post(
            "/v1/generations", json={"shot_id": shot["id"], "quote_id": quote["id"]}
        )
        assert response.status_code == 202
        job_id = uuid.UUID(response.json()["id"])
        settings = get_settings()
        store = LocalObjectStorage(
            settings.storage_root, settings.storage_claim_secret.get_secret_value().encode()
        )

        async def interrupt_and_resume():
            first_step = asyncio.Event()
            executor = GenerationExecutionService(
                store,
                polling_policy=ProviderPollingPolicy(
                    initial_delay=timedelta(seconds=30), maximum_delay=timedelta(seconds=30)
                ),
            )

            class ObservedExecutor:
                async def execute(self, job_id):
                    result = await executor.execute(job_id)
                    assert not result.is_complete
                    first_step.set()
                    return result

            interrupted = LocalWorkflowStarter(settings, executor_factory=ObservedExecutor)
            await interrupted.startup()
            try:
                assert await OutboxDispatcher(SessionLocal, interrupted).dispatch_once() == (
                    DispatchResult.PUBLISHED
                )
                await asyncio.wait_for(first_step.wait(), timeout=10)
            finally:
                await interrupted.shutdown()
            with SessionLocal() as db:
                assert db.get(GenerationJob, job_id).status.value in {"SUBMITTED", "RUNNING"}
                event = db.scalar(select(OutboxEvent).where(OutboxEvent.job_id == job_id))
                assert event.status == OutboxStatus.PUBLISHED

            resumed = LocalWorkflowStarter(settings)
            monkeypatch.setattr(public_api, "workflow_starter", resumed)
            await resumed.startup()
            try:
                reconciler = ControlPlaneReconciler(
                    SessionLocal, (), public_api.reconcile_generation_job, stuck_after=timedelta(0)
                )
                result = await reconciler.reconcile_once()
                assert result.jobs_reconciled == 1
                await asyncio.wait_for(resumed.wait_idle(), timeout=20)
            finally:
                await resumed.shutdown()

        assert raw_client.portal is not None
        raw_client.portal.call(interrupt_and_resume)
        job = raw_client.get(f"/v1/generations/{job_id}").json()
        assert job["status"] == "SUCCEEDED"
        assert len(job["outputs"]) == 1
        assert job["settlement_status"] == "SETTLED"
    finally:
        routes.activate(previous)


pytestmark = [pytest.mark.database, pytest.mark.media]
