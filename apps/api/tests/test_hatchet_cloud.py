"""Opt-in: native API -> Cloud durable workflow -> separate native worker -> test PostgreSQL."""

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import main, public_api
from app.config import Settings, get_settings
from app.db import SessionLocal, engine
from app.hatchet_workflows import create_hatchet_workflows
from app.models import LedgerTransaction, OutboxEvent, OutboxStatus
from app.outbox import generation_workflow_key
from app.routing import MOCK_ROUTE_VERSION, get_route_registry
from app.workflow import HatchetWorkflowStarter, WorkflowStartRequest
from tests.test_provider_routing import _quoted_shot

pytestmark = [pytest.mark.live, pytest.mark.database]


@pytest.fixture
def cloud_api(clean_database, monkeypatch, tmp_path):
    assert engine.dialect.name == "postgresql", "Cloud integration requires TEST_DATABASE_URL"
    assert engine.url.database.endswith("_test")
    configured = Settings(_env_file=None, **{
        **get_settings().model_dump(), "workflow_backend": "hatchet",
        "database_url": engine.url.render_as_string(hide_password=False),
        "hatchet_client_namespace": f"windev04_{uuid.uuid4().hex}",
        "outbox_dispatcher_enabled": True, "outbox_poll_interval_seconds": 0.1,
        "reconciler_enabled": False,
    })
    assert configured.hatchet_client_tls_strategy == "tls"
    workflows = create_hatchet_workflows(configured)
    starter = HatchetWorkflowStarter(workflows.generation_workflow, settings=configured)
    monkeypatch.setattr(main, "settings", configured)
    monkeypatch.setattr(main, "workflow_starter", starter)
    monkeypatch.setattr(public_api, "workflow_starter", starter)
    routes = get_route_registry()
    previous_route = routes.active_key
    routes.activate(MOCK_ROUTE_VERSION)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        health_port = sock.getsockname()[1]
    worker_env = {
        **os.environ,
        "DATABASE_URL": configured.database_url,
        "WORKFLOW_BACKEND": "hatchet",
        "HATCHET_CLIENT_NAMESPACE": configured.hatchet_client_namespace,
        "STORAGE_ROOT": str(configured.storage_root),
        "STORAGE_CLAIM_SECRET": configured.storage_claim_secret.get_secret_value(),
        "GENERATION_ROUTE_VERSION": MOCK_ROUTE_VERSION,
        "HATCHET_CLIENT_WORKER_HEALTHCHECK_ENABLED": "true",
        "HATCHET_CLIENT_WORKER_HEALTHCHECK_BIND_ADDRESS": "127.0.0.1",
        "HATCHET_CLIENT_WORKER_HEALTHCHECK_PORT": str(health_port),
        "PYTHONUNBUFFERED": "1",
    }
    worker = None
    try:
        with (tmp_path / "hatchet-worker.log").open("w", encoding="utf-8") as output:
            worker = subprocess.Popen(
                [sys.executable, "-m", "app.worker"],
                cwd=Path(__file__).resolve().parents[1], env=worker_env,
                stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            deadline = time.monotonic() + 90
            with httpx.Client(timeout=1, trust_env=False) as http:
                while True:
                    assert worker.poll() is None, "Worker exited; inspect its local test log"
                    try:
                        if http.get(f"http://127.0.0.1:{health_port}/health").status_code == 200:
                            break  # SDK health requires a successful Cloud heartbeat.
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline, "Cloud worker readiness timed out"
                    time.sleep(0.2)
            with TestClient(main.app) as client:
                yield client, starter, workflows, worker
    finally:
        routes.activate(previous_route)
        if worker is not None and worker.poll() is None:
            if sys.platform == "win32":
                # Only the process tree created by this fixture, never other development workers.
                subprocess.run(["taskkill", "/PID", str(worker.pid), "/T", "/F"],
                               capture_output=True, timeout=15, check=False)
            else:
                worker.terminate()
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)


def test_cloud_api_dispatch_worker_completion_and_duplicate_reuse(cloud_api):
    client, starter, workflows, worker = cloud_api
    shot, quote, _ = _quoted_shot(client)
    response = client.post("/v1/generations", json={"shot_id": shot["id"], "quote_id": quote["id"]})
    assert response.status_code == 202
    job_id = uuid.UUID(response.json()["id"])
    deadline = time.monotonic() + 120
    while True:
        assert worker.poll() is None, "Native worker exited while processing the job"
        job = client.get(f"/v1/generations/{job_id}").json()
        if job["status"] == "SUCCEEDED":
            break
        assert job["status"] not in {"FAILED_FINAL", "CANCELLED"}, job["status"]
        assert time.monotonic() < deadline, "Cloud generation did not finish in time"
        time.sleep(0.2)
    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.job_id == job_id))
        assert event.status == OutboxStatus.PUBLISHED
        workflow_id = event.workflow_id

    async def complete_and_replay():
        await asyncio.wait_for(
            workflows.hatchet.runs.get_run_ref(workflow_id).aio_result(), timeout=30
        )
        duplicate = await asyncio.wait_for(starter.start(WorkflowStartRequest(
            job_id, generation_workflow_key(job_id), {"job_id": str(job_id)}
        )), timeout=30)
        assert duplicate.workflow_id == workflow_id

    client.portal.call(complete_and_replay)
    assert len(job["outputs"]) == 1
    media = client.get(f"/v1/outputs/{job['final_output_id']}/content")
    assert media.status_code == 200 and media.content[4:8] == b"ftyp"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LedgerTransaction).where(
            LedgerTransaction.reference_id == str(job_id), LedgerTransaction.tx_type == "SETTLE",
        )) == 1
