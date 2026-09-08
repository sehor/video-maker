import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import structlog
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.blocking_io import run_blocking
from app.dead_letters import add_dead_letter
from app.models import DeadLetterSource, GenerationJob, OutboxEvent, OutboxStatus
from app.workflow import WorkflowStarter, WorkflowStartRequest

GENERATION_WORKFLOW_START = "generation.workflow.start"
logger = structlog.get_logger()


def generation_workflow_key(job_id: uuid.UUID) -> str:
    return f"generation-job:{job_id}:v1"


def enqueue_generation_workflow(db: Session, job: GenerationJob) -> OutboxEvent:
    event = OutboxEvent(
        job_id=job.id,
        event_type=GENERATION_WORKFLOW_START,
        idempotency_key=generation_workflow_key(job.id),
        payload_json={"job_id": str(job.id)},
        status=OutboxStatus.PENDING,
    )
    db.add(event)
    return event


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    id: uuid.UUID
    job_id: uuid.UUID
    event_type: str
    idempotency_key: str
    payload: dict[str, object]
    lock_token: str


class DispatchResult(StrEnum):
    IDLE = "IDLE"
    PUBLISHED = "PUBLISHED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    DEAD_LETTERED = "DEAD_LETTERED"


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        starter: WorkflowStarter,
        *,
        lease_duration: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=1),
        max_attempts: int = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.starter = starter
        self.lease_duration = lease_duration
        self.retry_delay = retry_delay
        self.max_attempts = max_attempts
        self.clock = clock
        if max_attempts <= 0:
            raise ValueError("outbox max attempts must be positive")

    async def dispatch_once(self) -> DispatchResult:
        event = await run_blocking(self._claim_one)
        if event is None:
            return DispatchResult.IDLE
        (await run_blocking(self.after_claim, event))
        try:
            result = await self.starter.start(
                WorkflowStartRequest(
                    job_id=event.job_id,
                    idempotency_key=event.idempotency_key,
                    payload=event.payload,
                )
            )
        except Exception as exc:
            return await run_blocking(self._schedule_retry, event, exc)
        (await run_blocking(self.after_start, event))
        (await run_blocking(self._mark_published, event, result.workflow_id))
        return DispatchResult.PUBLISHED

    def after_claim(self, event: ClaimedOutboxEvent) -> None:
        """Failure-injection seam for the crash-before-send window."""

    def after_start(self, event: ClaimedOutboxEvent) -> None:
        """Failure-injection seam for the accepted-before-marked window."""

    def _claim_one(self) -> ClaimedOutboxEvent | None:
        now = self.clock()
        stale_before = now - self.lease_duration
        dispatchable = and_(
            OutboxEvent.event_type == GENERATION_WORKFLOW_START,
            or_(
                and_(
                    OutboxEvent.status == OutboxStatus.PENDING,
                    OutboxEvent.next_attempt_at <= now,
                ),
                and_(
                    OutboxEvent.status == OutboxStatus.PROCESSING,
                    OutboxEvent.locked_at <= stale_before,
                ),
            ),
        )
        with self.session_factory() as db:
            try:
                candidates = list(
                    db.scalars(
                        select(OutboxEvent)
                        .where(dispatchable)
                        .order_by(OutboxEvent.created_at, OutboxEvent.id)
                        .with_for_update(skip_locked=True)
                        .limit(8)
                    )
                )
                for candidate in candidates:
                    lock_token = str(uuid.uuid4())
                    claimed = db.execute(
                        update(OutboxEvent)
                        .where(OutboxEvent.id == candidate.id, dispatchable)
                        .values(
                            status=OutboxStatus.PROCESSING,
                            attempt_count=OutboxEvent.attempt_count + 1,
                            locked_at=now,
                            lock_token=lock_token,
                            last_error=None,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if claimed.rowcount != 1:
                        db.rollback()
                        continue
                    db.commit()
                    return ClaimedOutboxEvent(
                        id=candidate.id,
                        job_id=candidate.job_id,
                        event_type=candidate.event_type,
                        idempotency_key=candidate.idempotency_key,
                        payload=dict(candidate.payload_json),
                        lock_token=lock_token,
                    )
            except OperationalError:
                if db.get_bind().dialect.name != "sqlite":
                    raise
                # SQLite cannot express SKIP LOCKED; the compare-and-set still protects
                # correctness, and a later dispatcher pass will retry the pending row.
                db.rollback()
                logger.debug("outbox.claim_contended", database="sqlite")
                return None
        return None

    def _schedule_retry(self, event: ClaimedOutboxEvent, exc: Exception) -> DispatchResult:
        now = self.clock()
        error = f"{type(exc).__name__}: {exc}"[:2000]
        with self.session_factory() as db:
            source = db.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.id == event.id,
                    OutboxEvent.status == OutboxStatus.PROCESSING,
                    OutboxEvent.lock_token == event.lock_token,
                )
                .with_for_update()
            )
            if source is None:
                db.rollback()
                raise RuntimeError("outbox lease was lost while scheduling a retry")
            if source.attempt_count >= self.max_attempts:
                source.status = OutboxStatus.DEAD_LETTER
                add_dead_letter(
                    db,
                    source_type=DeadLetterSource.OUTBOX,
                    source_id=source.id,
                    event_type=source.event_type,
                    payload=dict(source.payload_json),
                    attempt_count=source.attempt_count,
                    error=error,
                )
                result = DispatchResult.DEAD_LETTERED
            else:
                source.status = OutboxStatus.PENDING
                source.next_attempt_at = now + self.retry_delay
                result = DispatchResult.RETRY_SCHEDULED
            source.locked_at = None
            source.lock_token = None
            source.last_error = error
            db.commit()
        if result == DispatchResult.DEAD_LETTERED:
            logger.error(
                "outbox.dead_lettered",
                outbox_event_id=str(event.id),
                event_type=event.event_type,
                attempt_count=self.max_attempts,
                error_type=type(exc).__name__,
            )
            return result
        logger.warning(
            "outbox.retry_scheduled",
            outbox_event_id=str(event.id),
            job_id=str(event.job_id),
            error_type=type(exc).__name__,
        )
        return result

    def _mark_published(self, event: ClaimedOutboxEvent, workflow_id: str) -> None:
        now = self.clock()
        with self.session_factory() as db:
            published = db.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event.id,
                    OutboxEvent.status == OutboxStatus.PROCESSING,
                    OutboxEvent.lock_token == event.lock_token,
                )
                .values(
                    status=OutboxStatus.PUBLISHED,
                    published_at=now,
                    workflow_id=workflow_id,
                    locked_at=None,
                    lock_token=None,
                    last_error=None,
                )
            )
            if published.rowcount != 1:
                db.rollback()
                raise RuntimeError("outbox lease was lost while marking the event published")
            db.commit()
        logger.info(
            "outbox.published",
            outbox_event_id=str(event.id),
            job_id=str(event.job_id),
            workflow_id=workflow_id,
        )
