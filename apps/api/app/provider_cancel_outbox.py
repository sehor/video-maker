import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.blocking_io import run_blocking
from app.dead_letters import add_dead_letter
from app.models import (
    DeadLetterSource,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    OutboxStatus,
)
from app.outbox import DispatchResult
from app.provider import provider_cancel_key

PROVIDER_CANCEL_REQUESTED = "provider.cancel.requested"
logger = structlog.get_logger()


def enqueue_provider_cancel(
    db: Session,
    job: GenerationJob,
    attempt: GenerationAttempt,
) -> OutboxEvent:
    existing = db.scalar(
        select(OutboxEvent).where(
            OutboxEvent.job_id == job.id,
            OutboxEvent.event_type == PROVIDER_CANCEL_REQUESTED,
        )
    )
    if existing is not None:
        if existing.attempt_id != attempt.id:
            raise RuntimeError("provider cancel outbox is bound to another attempt")
        return existing
    event = OutboxEvent(
        job_id=job.id,
        attempt_id=attempt.id,
        event_type=PROVIDER_CANCEL_REQUESTED,
        idempotency_key=provider_cancel_key(attempt.id),
        payload_json={"job_id": str(job.id), "attempt_id": str(attempt.id)},
        status=OutboxStatus.PENDING,
    )
    db.add(event)
    return event


@dataclass(frozen=True, slots=True)
class ProviderCancelRequest:
    outbox_event_id: uuid.UUID
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    provider_code: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ClaimedProviderCancelEvent:
    request: ProviderCancelRequest
    lock_token: str


ProviderCancelWorker = Callable[[ProviderCancelRequest], Awaitable[None]]


class ProviderCancelDispatcher:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        worker: ProviderCancelWorker,
        *,
        lease_duration: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=1),
        max_attempts: int = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lease_renew_interval_seconds: float | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.worker = worker
        self.lease_duration = lease_duration
        self.retry_delay = retry_delay
        self.max_attempts = max_attempts
        self.clock = clock
        self.lease_renew_interval_seconds = (
            lease_renew_interval_seconds
            if lease_renew_interval_seconds is not None
            else max(0.1, lease_duration.total_seconds() / 3)
        )
        if self.lease_renew_interval_seconds <= 0:
            raise ValueError("provider cancel lease renewal interval must be positive")
        if max_attempts <= 0:
            raise ValueError("provider cancel max attempts must be positive")

    async def dispatch_once(self) -> DispatchResult:
        event = await run_blocking(self._claim_one)
        if event is None:
            return DispatchResult.IDLE
        (await run_blocking(self.after_claim, event))
        (await run_blocking(self._renew_lease, event))
        stop_renewal = asyncio.Event()
        renewal = asyncio.create_task(self._renew_while_processing(event, stop_renewal))
        error: Exception | None = None
        try:
            await self.worker(event.request)
        except Exception as exc:
            error = exc
        finally:
            stop_renewal.set()
            await renewal
        if error is not None:
            return await run_blocking(self._schedule_retry, event, error)
        (await run_blocking(self.after_cancel, event))
        (await run_blocking(self._mark_published, event))
        return DispatchResult.PUBLISHED

    def after_claim(self, event: ClaimedProviderCancelEvent) -> None:
        """Failure-injection seam for a crash before the Provider call."""

    def after_cancel(self, event: ClaimedProviderCancelEvent) -> None:
        """Failure-injection seam for a crash after Provider acceptance."""

    def _dispatchable(self, now: datetime):
        stale_before = now - self.lease_duration
        return and_(
            OutboxEvent.event_type == PROVIDER_CANCEL_REQUESTED,
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

    def _claim_one(self) -> ClaimedProviderCancelEvent | None:
        now = self.clock()
        dispatchable = self._dispatchable(now)
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
                    if candidate.attempt_id is None:
                        raise RuntimeError("provider cancel outbox is missing attempt_id")
                    attempt = db.get(GenerationAttempt, candidate.attempt_id)
                    if attempt is None or attempt.job_id != candidate.job_id:
                        raise RuntimeError("provider cancel outbox attempt does not match job")
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
                    request = ProviderCancelRequest(
                        outbox_event_id=candidate.id,
                        job_id=candidate.job_id,
                        attempt_id=candidate.attempt_id,
                        provider_code=attempt.provider_code,
                        idempotency_key=candidate.idempotency_key,
                    )
                    db.commit()
                    return ClaimedProviderCancelEvent(request=request, lock_token=lock_token)
            except OperationalError:
                if db.get_bind().dialect.name != "sqlite":
                    raise
                db.rollback()
                logger.debug("provider_cancel_outbox.claim_contended", database="sqlite")
                return None
        return None

    def _renew_lease(self, event: ClaimedProviderCancelEvent) -> None:
        now = self.clock()
        with self.session_factory() as db:
            renewed = db.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event.request.outbox_event_id,
                    OutboxEvent.status == OutboxStatus.PROCESSING,
                    OutboxEvent.lock_token == event.lock_token,
                )
                .values(locked_at=now)
            )
            if renewed.rowcount != 1:
                db.rollback()
                raise RuntimeError("provider cancel outbox lease was lost while renewing")
            db.commit()

    async def _renew_while_processing(
        self,
        event: ClaimedProviderCancelEvent,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.lease_renew_interval_seconds)
            except TimeoutError:
                (await run_blocking(self._renew_lease, event))

    def _schedule_retry(self, event: ClaimedProviderCancelEvent, exc: Exception) -> DispatchResult:
        now = self.clock()
        error = f"{type(exc).__name__}: {exc}"[:2000]
        with self.session_factory() as db:
            source = db.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.id == event.request.outbox_event_id,
                    OutboxEvent.status == OutboxStatus.PROCESSING,
                    OutboxEvent.lock_token == event.lock_token,
                )
                .with_for_update()
            )
            if source is None:
                db.rollback()
                raise RuntimeError("provider cancel outbox lease was lost while retrying")
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
                "provider_cancel_outbox.dead_lettered",
                outbox_event_id=str(event.request.outbox_event_id),
                job_id=str(event.request.job_id),
                attempt_id=str(event.request.attempt_id),
                attempt_count=self.max_attempts,
                error_type=type(exc).__name__,
            )
            return result
        logger.warning(
            "provider_cancel_outbox.retry_scheduled",
            outbox_event_id=str(event.request.outbox_event_id),
            job_id=str(event.request.job_id),
            attempt_id=str(event.request.attempt_id),
            error_type=type(exc).__name__,
        )
        return result

    def _mark_published(self, event: ClaimedProviderCancelEvent) -> None:
        now = self.clock()
        with self.session_factory() as db:
            published = db.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event.request.outbox_event_id,
                    OutboxEvent.status == OutboxStatus.PROCESSING,
                    OutboxEvent.lock_token == event.lock_token,
                )
                .values(
                    status=OutboxStatus.PUBLISHED,
                    published_at=now,
                    locked_at=None,
                    lock_token=None,
                    last_error=None,
                )
            )
            if published.rowcount != 1:
                db.rollback()
                raise RuntimeError("provider cancel outbox lease was lost while publishing")
            db.commit()
        logger.info(
            "provider_cancel_outbox.published",
            outbox_event_id=str(event.request.outbox_event_id),
            job_id=str(event.request.job_id),
            attempt_id=str(event.request.attempt_id),
        )
