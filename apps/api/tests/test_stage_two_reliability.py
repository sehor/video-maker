import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Attempt,
    AttemptStatus,
    AuditLog,
    Batch,
    Job,
    JobStatus,
    LedgerTransaction,
    OutboxEvent,
    SettlementStatus,
    WorkflowRun,
)
from app.outbox import drain_local_tasks
from app.provider import MockVideoProvider
from app.storage import LocalObjectStorage
from tests.test_mock_jobs import create_shot
from tests.test_stage_two_ledger import grant, quote, submit

ADMIN_HEADERS = {"x-admin-token": "development-admin-token"}


def test_retryable_failure_uses_new_attempt_and_settles_once(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    response = submit(client, shot["id"], item["id"], "flaky")
    assert response.status_code == 202
    job = client.get(f"/v1/generations/{response.json()['id']}").json()
    assert job["status"] == "SUCCEEDED"
    assert [attempt["status"] for attempt in job["attempts"]] == [
        "FAILED_RETRYABLE",
        "SUCCEEDED",
    ]
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(LedgerTransaction.kind == "RESERVE")
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(LedgerTransaction.kind == "SETTLE")
            )
            == 1
        )


def test_retry_limit_releases_once_after_three_timeouts(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    response = submit(client, shot["id"], item["id"], "timeout")
    job = client.get(f"/v1/generations/{response.json()['id']}").json()
    assert job["status"] == "FAILED_FINAL"
    assert job["settlement_status"] == "RELEASED"
    assert [attempt["status"] for attempt in job["attempts"]] == [
        "FAILED_RETRYABLE",
        "FAILED_RETRYABLE",
        "TIMED_OUT",
    ]
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(LedgerTransaction.kind == "RELEASE")
            )
            == 1
        )


def test_unknown_submit_result_recovers_without_duplicate_attempt(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    response = submit(client, shot["id"], item["id"], "submit_unknown")
    job = client.get(f"/v1/generations/{response.json()['id']}").json()
    assert job["status"] == "SUCCEEDED"
    assert len(job["attempts"]) == 1
    assert job["attempts"][0]["status"] == "SUCCEEDED"
    with SessionLocal() as db:
        run = db.scalar(select(WorkflowRun).where(WorkflowRun.job_id == uuid.UUID(job["id"])))
        assert run is not None and run.run_attempt_count == 2


def test_late_old_attempt_completion_cannot_overwrite_success(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    response = submit(client, shot["id"], item["id"], "flaky")
    job_id = uuid.UUID(response.json()["id"])
    with SessionLocal() as db:
        attempts = list(
            db.scalars(select(Attempt).where(Attempt.job_id == job_id).order_by(Attempt.number))
        )
        assert len(attempts) == 2 and attempts[0].provider_job_id is not None
        old_attempt_id = attempts[0].id
        old_provider_id = attempts[0].provider_job_id
    store = LocalObjectStorage(get_settings().storage_root)
    MockVideoProvider(store)._finish_success(
        job_id,
        old_attempt_id,
        old_provider_id,
        MockVideoProvider._create_mp4(),
    )
    job = client.get(f"/v1/generations/{job_id}").json()
    assert job["status"] == "SUCCEEDED"
    assert len(job["outputs"]) == 1


def test_submitted_cancel_waits_for_provider_confirmation(
    client: TestClient, monkeypatch
) -> None:
    async def no_drain(_storage) -> None:
        return None

    async def no_confirmation(self, provider_job_id: str) -> None:
        return None

    monkeypatch.setattr("app.api.drain_local_tasks", no_drain)
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    response = submit(client, shot["id"], item["id"])
    job_id = uuid.UUID(response.json()["id"])
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        attempt = db.scalar(select(Attempt).where(Attempt.job_id == job_id))
        assert job is not None and attempt is not None
        job.status = JobStatus.RUNNING
        attempt.status = AttemptStatus.RUNNING
        attempt.provider_job_id = "mock-cancel-pending"
        db.commit()
    monkeypatch.setattr(MockVideoProvider, "cancel", no_confirmation)
    pending = client.post(f"/v1/generations/{job_id}/cancel")
    assert pending.json()["status"] == "CANCEL_REQUESTED"
    assert pending.json()["settlement_status"] == "RESERVED"
    assert client.get("/v1/wallet").json()["balances"]["FAST_MS"] == {
        "USER_AVAILABLE": 0,
        "USER_RESERVED": 2_000,
    }
    monkeypatch.undo()
    store = LocalObjectStorage(get_settings().storage_root)
    asyncio.run(MockVideoProvider(store).cancel("mock-cancel-pending"))
    cancelled = client.get(f"/v1/generations/{job_id}").json()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["settlement_status"] == "RELEASED"


def test_batch_reserves_total_atomically_and_finishes(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 4_000)
    quotes = [quote(client, shot["id"]) for _ in range(2)]
    response = client.post(
        "/v1/batches",
        json={
            "project_id": shot["project_id"],
            "quote_ids": [item["id"] for item in quotes],
            "idempotency_key": "batch:success:one",
            "mock_mode": "success",
        },
    )
    assert response.status_code == 202
    batch = client.get(f"/v1/batches/{response.json()['id']}").json()
    assert batch["status"] == "SUCCEEDED"
    assert len(batch["jobs"]) == 2
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(LedgerTransaction.kind == "BATCH_RESERVE")
            )
            == 1
        )


def test_batch_insufficient_balance_rolls_back_everything(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 3_000)
    quotes = [quote(client, shot["id"]) for _ in range(2)]
    response = client.post(
        "/v1/batches",
        json={
            "project_id": shot["project_id"],
            "quote_ids": [item["id"] for item in quotes],
            "idempotency_key": "batch:insufficient",
        },
    )
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Batch)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_batch_tracks_partial_success_while_jobs_settle_independently(
    client: TestClient, monkeypatch
) -> None:
    async def no_drain(_storage) -> None:
        return None

    monkeypatch.setattr("app.api.drain_local_tasks", no_drain)
    shot = create_shot(client)
    grant(client, 4_000)
    quotes = [quote(client, shot["id"]) for _ in range(2)]
    response = client.post(
        "/v1/batches",
        json={
            "project_id": shot["project_id"],
            "quote_ids": [item["id"] for item in quotes],
            "idempotency_key": "batch:partial:one",
        },
    )
    batch_id = uuid.UUID(response.json()["id"])
    with SessionLocal() as db:
        jobs = list(db.scalars(select(Job).where(Job.batch_id == batch_id).order_by(Job.id)))
        jobs[0].mock_mode = "success"
        jobs[1].mock_mode = "failure"
        db.commit()
    monkeypatch.undo()
    asyncio.run(drain_local_tasks(LocalObjectStorage(get_settings().storage_root)))
    batch = client.get(f"/v1/batches/{batch_id}").json()
    assert batch["status"] == "PARTIAL_SUCCESS"
    assert {job["status"] for job in batch["jobs"]} == {"SUCCEEDED", "FAILED_FINAL"}
    wallet = client.get("/v1/wallet").json()["balances"]["FAST_MS"]
    assert wallet == {"USER_AVAILABLE": 2_000, "USER_RESERVED": 0}


def test_admin_force_release_is_audited_and_reconciles(
    client: TestClient, monkeypatch
) -> None:
    async def no_drain(_storage) -> None:
        return None

    monkeypatch.setattr("app.api.drain_local_tasks", no_drain)
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    job = submit(client, shot["id"], item["id"]).json()
    released = client.post(
        f"/v1/admin/jobs/{job['id']}/force-release",
        json={"reason": "dispatcher recovery investigation"},
        headers=ADMIN_HEADERS,
    )
    assert released.status_code == 200
    assert released.json()["settlement_status"] == SettlementStatus.RELEASED.value
    report = client.get("/v1/admin/reconciliation", headers=ADMIN_HEADERS)
    assert report.status_code == 200 and report.json()["ok"] is True
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "job.force_release")
            )
            == 1
        )


def test_admin_retry_reserves_again_and_records_audit(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    failed = submit(client, shot["id"], item["id"], "failure").json()
    retried = client.post(
        f"/v1/admin/jobs/{failed['id']}/retry",
        json={"reason": "operator verified transient provider incident"},
        headers=ADMIN_HEADERS,
    )
    assert retried.status_code == 202
    final = client.get(f"/v1/generations/{failed['id']}").json()
    assert final["status"] == "FAILED_FINAL"
    assert len(final["attempts"]) == 2
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "job.retry")
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(LedgerTransaction.kind == "ADMIN_RETRY_RESERVE")
            )
            == 1
        )
