import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol

from hatchet_sdk.exceptions import IdempotencyCollisionError
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.ledger import utcnow
from app.models import OutboxEvent, WorkflowRun, WorkflowRunStatus
from app.provider import MockVideoProvider
from app.storage import LocalObjectStorage

GENERATION_TOPIC = "generation.requested"
GENERATION_WORKFLOW = "generation-job-v1"
logger = logging.getLogger(__name__)


class DispatcherCrash(BaseException):
    """Test-only process-crash signal; intentionally bypasses normal retry handling."""


class WorkflowLauncher(Protocol):
    def start(self, workflow_key: str, payload: dict[str, str]) -> bool: ...


def enqueue_generation(
    db: Session, job_id: uuid.UUID, attempt_id: uuid.UUID
) -> OutboxEvent:
    workflow_key = f"attempt:{attempt_id}:submit:v1"
    event = OutboxEvent(
        topic=GENERATION_TOPIC,
        aggregate_type="job",
        aggregate_id=str(job_id),
        idempotency_key=workflow_key,
        payload={
            "job_id": str(job_id),
            "attempt_id": str(attempt_id),
            "workflow_name": GENERATION_WORKFLOW,
        },
    )
    db.add(event)
    return event


class LocalWorkflowLauncher:
    """Durable development launcher behind the same idempotent boundary as Hatchet."""

    def start(self, workflow_key: str, payload: dict[str, str]) -> bool:
        with SessionLocal() as db:
            if db.scalar(select(WorkflowRun.id).where(WorkflowRun.workflow_key == workflow_key)):
                return False
            run = WorkflowRun(
                workflow_key=workflow_key,
                workflow_name=payload["workflow_name"],
                job_id=uuid.UUID(payload["job_id"]),
                attempt_id=uuid.UUID(payload["attempt_id"]),
                payload=payload,
                status=WorkflowRunStatus.ACCEPTED,
            )
            db.add(run)
            try:
                db.commit()
                return True
            except IntegrityError:
                db.rollback()
                return False


class HatchetWorkflowLauncher:
    """Start the real Hatchet task; database state remains the business truth."""

    def start(self, workflow_key: str, payload: dict[str, str]) -> bool:
        from app.hatchet_workflow import GenerationWorkflowInput, get_hatchet_generation_task

        _, task = get_hatchet_generation_task()
        try:
            task.run_no_wait(
                input=GenerationWorkflowInput(
                    job_id=payload["job_id"],
                    attempt_id=payload["attempt_id"],
                    workflow_key=workflow_key,
                )
            )
            return True
        except IdempotencyCollisionError:
            return False


def configured_launcher() -> WorkflowLauncher:
    if get_settings().workflow_backend == "hatchet":
        return HatchetWorkflowLauncher()
    return LocalWorkflowLauncher()


class OutboxDispatcher:
    def __init__(
        self,
        launcher: WorkflowLauncher | None = None,
        *,
        lease_seconds: int = 30,
        after_publish: Callable[[OutboxEvent], None] | None = None,
    ) -> None:
        self.launcher = launcher or configured_launcher()
        self.lease_seconds = lease_seconds
        self.after_publish = after_publish

    def claim_one(self) -> tuple[uuid.UUID, str, dict[str, str], str] | None:
        now = utcnow()
        with SessionLocal() as db:
            candidates = list(
                db.scalars(
                    select(OutboxEvent.id)
                    .where(
                        OutboxEvent.sent_at.is_(None),
                        OutboxEvent.next_attempt_at <= now,
                        or_(OutboxEvent.locked_until.is_(None), OutboxEvent.locked_until <= now),
                    )
                    .order_by(OutboxEvent.created_at, OutboxEvent.id)
                    .limit(10)
                )
            )
            for event_id in candidates:
                token = uuid.uuid4().hex
                result = db.execute(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.id == event_id,
                        OutboxEvent.sent_at.is_(None),
                        OutboxEvent.next_attempt_at <= now,
                        or_(OutboxEvent.locked_until.is_(None), OutboxEvent.locked_until <= now),
                    )
                    .values(
                        locked_by=token,
                        locked_until=now + timedelta(seconds=self.lease_seconds),
                        attempt_count=OutboxEvent.attempt_count + 1,
                    )
                )
                if result.rowcount != 1:
                    db.rollback()
                    continue
                event = db.get(OutboxEvent, event_id)
                assert event is not None
                payload = dict(event.payload)
                key = event.idempotency_key
                db.commit()
                return event.id, key, payload, token
        return None

    def dispatch_one(self) -> bool:
        claimed = self.claim_one()
        if claimed is None:
            return False
        event_id, workflow_key, payload, token = claimed
        try:
            self.launcher.start(workflow_key, payload)
            if self.after_publish:
                with SessionLocal() as db:
                    event = db.get(OutboxEvent, event_id)
                    assert event is not None
                    self.after_publish(event)
        except Exception as exc:
            self.mark_failed(event_id, token, exc)
            return True
        with SessionLocal() as db:
            db.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.locked_by == token,
                    OutboxEvent.sent_at.is_(None),
                )
                .values(
                    sent_at=utcnow(),
                    locked_by=None,
                    locked_until=None,
                    last_error=None,
                )
            )
            db.commit()
        return True

    def mark_failed(self, event_id: uuid.UUID, token: str, exc: Exception) -> None:
        with SessionLocal() as db:
            event = db.get(OutboxEvent, event_id)
            if event is None or event.locked_by != token:
                return
            delay_seconds = min(60, 2 ** min(event.attempt_count, 6))
            event.next_attempt_at = utcnow() + timedelta(seconds=delay_seconds)
            event.locked_by = None
            event.locked_until = None
            event.last_error = f"{type(exc).__name__}: {exc}"[:500]
            db.commit()

    def dispatch_all(self, limit: int = 100) -> int:
        count = 0
        while count < limit and self.dispatch_one():
            count += 1
        return count


class LocalWorkflowRunner:
    def __init__(self, storage: LocalObjectStorage, *, lease_seconds: int = 60) -> None:
        self.storage = storage
        self.lease_seconds = lease_seconds

    def claim_one(self) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str] | None:
        now = utcnow()
        with SessionLocal() as db:
            candidates = list(
                db.scalars(
                    select(WorkflowRun.id)
                    .where(
                        WorkflowRun.status.in_(
                            [WorkflowRunStatus.ACCEPTED, WorkflowRunStatus.RUNNING]
                        ),
                        or_(WorkflowRun.locked_until.is_(None), WorkflowRun.locked_until <= now),
                    )
                    .order_by(WorkflowRun.accepted_at, WorkflowRun.id)
                    .limit(10)
                )
            )
            for run_id in candidates:
                token = uuid.uuid4().hex
                result = db.execute(
                    update(WorkflowRun)
                    .where(
                        WorkflowRun.id == run_id,
                        WorkflowRun.status.in_(
                            [WorkflowRunStatus.ACCEPTED, WorkflowRunStatus.RUNNING]
                        ),
                        or_(WorkflowRun.locked_until.is_(None), WorkflowRun.locked_until <= now),
                    )
                    .values(
                        status=WorkflowRunStatus.RUNNING,
                        locked_by=token,
                        locked_until=now + timedelta(seconds=self.lease_seconds),
                        started_at=now,
                        run_attempt_count=WorkflowRun.run_attempt_count + 1,
                    )
                )
                if result.rowcount != 1:
                    db.rollback()
                    continue
                run = db.get(WorkflowRun, run_id)
                assert run is not None
                job_id = run.job_id
                attempt_id = run.attempt_id
                db.commit()
                return run.id, job_id, attempt_id, token
        return None

    async def run_one(self) -> bool:
        claimed = self.claim_one()
        if claimed is None:
            return False
        run_id, job_id, attempt_id, token = claimed
        try:
            await MockVideoProvider(self.storage).submit(job_id, attempt_id)
        except Exception as exc:
            with SessionLocal() as db:
                db.execute(
                    update(WorkflowRun)
                    .where(WorkflowRun.id == run_id, WorkflowRun.locked_by == token)
                    .values(
                        status=WorkflowRunStatus.ACCEPTED,
                        locked_by=None,
                        locked_until=None,
                        last_error=f"{type(exc).__name__}: {exc}"[:500],
                    )
                )
                db.commit()
            return False
        with SessionLocal() as db:
            db.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id, WorkflowRun.locked_by == token)
                .values(
                    status=WorkflowRunStatus.SUCCEEDED,
                    locked_by=None,
                    locked_until=None,
                    finished_at=utcnow(),
                    last_error=None,
                )
            )
            db.commit()
        return True

    async def run_all(self, limit: int = 100) -> int:
        count = 0
        while count < limit and await self.run_one():
            count += 1
        return count


async def drain_local_tasks(storage: LocalObjectStorage) -> None:
    dispatcher = OutboxDispatcher()
    if get_settings().workflow_backend == "hatchet":
        dispatcher.dispatch_all()
        return
    runner = LocalWorkflowRunner(storage)
    for _ in range(100):
        dispatched = dispatcher.dispatch_all()
        ran = await runner.run_all()
        if dispatched == 0 and ran == 0:
            return
    raise RuntimeError("local workflow drain exceeded safety limit")


async def dispatcher_loop(storage: LocalObjectStorage, poll_seconds: float) -> None:
    while True:
        try:
            await drain_local_tasks(storage)
        except Exception:
            logger.exception("outbox dispatcher iteration failed")
        await asyncio.sleep(poll_seconds)
