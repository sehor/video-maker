import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.control_plane import ControlPlaneReconciler, ReadinessService
from app.db import SessionLocal
from app.models import (
    AdminOperationAudit,
    DeadLetterEvent,
    DeadLetterStatus,
    OutboxEvent,
    OutboxStatus,
    ProviderEventInbox,
    ProviderEventInboxStatus,
)
from app.outbox import DispatchResult, OutboxDispatcher
from tests.test_transactional_outbox import FakeWorkflowStarter, create_pending_job


def test_outbox_dead_letter_is_queryable_audited_and_replayable(
    raw_client: TestClient,
) -> None:
    job = create_pending_job(raw_client)
    dispatcher = OutboxDispatcher(
        SessionLocal,
        FakeWorkflowStarter("timeout"),
        max_attempts=1,
    )

    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.DEAD_LETTERED
    with SessionLocal() as db:
        source = db.scalar(select(OutboxEvent))
        dead_letter = db.scalar(select(DeadLetterEvent))
        assert source is not None and source.status == OutboxStatus.DEAD_LETTER
        assert dead_letter is not None
        assert dead_letter.status == DeadLetterStatus.OPEN
        assert dead_letter.source_id == source.id
        dead_letter_id = dead_letter.id

    assert raw_client.get("/v1/admin/dead-letters").status_code == 403
    headers = {"x-test-user": "admin-user"}
    listed = raw_client.get("/v1/admin/dead-letters", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == str(dead_letter_id)
    assert listed.json()[0]["payload_json"]["job_id"] == job["id"]

    first = raw_client.post(
        f"/v1/admin/dead-letters/{dead_letter_id}/replay", headers=headers
    )
    second = raw_client.post(
        f"/v1/admin/dead-letters/{dead_letter_id}/replay", headers=headers
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "REPLAYED"

    with SessionLocal() as db:
        source = db.scalar(select(OutboxEvent))
        assert source is not None
        assert source.status == OutboxStatus.PENDING
        assert source.attempt_count == 0
        assert db.scalar(select(func.count()).select_from(AdminOperationAudit)) == 1

    audits = raw_client.get(
        "/v1/admin/operation-audits",
        headers=headers,
        params={"target_id": str(dead_letter_id)},
    )
    assert audits.status_code == 200
    assert audits.json()[0]["operation_type"] == "dead_letter.replay"

    second_cycle = OutboxDispatcher(
        SessionLocal,
        FakeWorkflowStarter("timeout"),
        max_attempts=1,
    )
    assert asyncio.run(second_cycle.dispatch_once()) == DispatchResult.DEAD_LETTERED
    with SessionLocal() as db:
        dead_letter = db.get(DeadLetterEvent, dead_letter_id)
        assert dead_letter is not None
        assert dead_letter.status == DeadLetterStatus.OPEN
        assert dead_letter.cycle_count == 2

    replayed_again = raw_client.post(
        f"/v1/admin/dead-letters/{dead_letter_id}/replay", headers=headers
    )
    duplicate_again = raw_client.post(
        f"/v1/admin/dead-letters/{dead_letter_id}/replay", headers=headers
    )
    assert replayed_again.status_code == duplicate_again.status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AdminOperationAudit)) == 2


def test_reconciler_releases_stale_provider_lease_once() -> None:
    now = datetime.now(UTC)
    with SessionLocal() as db:
        db.add(
            ProviderEventInbox(
                provider_code="mock",
                external_event_id="evt-stale",
                provider_job_id="provider-job-stale",
                provider_status="RUNNING",
                payload_hash="a" * 64,
                status=ProviderEventInboxStatus.PROCESSING,
                locked_at=now - timedelta(minutes=6),
                lock_token="stale-token",
            )
        )
        db.commit()

    async def execute_job(_job_id):
        raise AssertionError("no stuck job should be executed")

    reconciler = ControlPlaneReconciler(
        SessionLocal,
        (),
        execute_job,
        clock=lambda: now,
    )
    first = asyncio.run(reconciler.reconcile_once())
    second = asyncio.run(reconciler.reconcile_once())
    assert first.provider_leases_released == 1
    assert second.provider_leases_released == 0
    with SessionLocal() as db:
        event = db.scalar(select(ProviderEventInbox))
        assert event is not None
        assert event.status == ProviderEventInboxStatus.RECEIVED
        assert event.lock_token is None


def test_readiness_distinguishes_dependencies_and_migration_head(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'readiness.db'}")
    factory = sessionmaker(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64))"))
        connection.execute(
            text("INSERT INTO alembic_version VALUES ('0011_control_plane_recovery')")
        )

    ready = ReadinessService(factory, lambda: True, lambda: True).check()
    unavailable = ReadinessService(factory, lambda: False, lambda: True).check()
    assert ready.ready
    assert ready.checks == {
        "database": True,
        "migrations": True,
        "storage": True,
        "workflow": True,
    }
    assert not unavailable.ready
    assert unavailable.checks["storage"] is False


def test_health_is_live_when_readiness_fails(raw_client: TestClient) -> None:
    assert raw_client.get("/healthz").status_code == 200
    response = raw_client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
