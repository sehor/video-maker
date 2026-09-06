import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.models import (
    AttemptStatus,
    DeadLetterEvent,
    DeadLetterStatus,
    GenerationAttempt,
    GenerationJob,
    JobStatus,
    OutboxEvent,
    OutboxStatus,
    ProviderEventInbox,
    ProviderEventInboxStatus,
    StorageCleanupEvent,
)
from app.outbox import DispatchResult

logger = structlog.get_logger()
Dispatch = Callable[[], Awaitable[DispatchResult]]
ExecuteJob = Callable[[uuid.UUID], Awaitable[object]]

ACTIVE_JOB_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.ROUTING,
    JobStatus.SUBMITTED,
    JobStatus.RUNNING,
}
ACTIVE_ATTEMPT_STATUSES = {
    AttemptStatus.CREATED,
    AttemptStatus.SUBMITTING,
    AttemptStatus.SUBMITTED,
    AttemptStatus.RUNNING,
}


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    dispatch_results: tuple[DispatchResult, ...]
    provider_leases_released: int
    jobs_reconciled: int


class ControlPlaneReconciler:
    """Re-enters only idempotent dispatch/execution paths; it never repairs terminal state."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        dispatchers: tuple[Dispatch, ...],
        execute_job: ExecuteJob,
        *,
        stuck_after: timedelta = timedelta(minutes=5),
        provider_lease: timedelta = timedelta(minutes=5),
        batch_size: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.dispatchers = dispatchers
        self.execute_job = execute_job
        self.stuck_after = stuck_after
        self.provider_lease = provider_lease
        self.batch_size = batch_size
        self.clock = clock

    async def reconcile_once(self) -> ReconcileResult:
        dispatch_results = tuple([await dispatch() for dispatch in self.dispatchers])
        released = self._release_expired_provider_leases()
        job_ids = self._stuck_job_ids()
        reconciled = 0
        for job_id in job_ids:
            try:
                await self.execute_job(job_id)
            except Exception as exc:
                logger.warning(
                    "control_plane.job_reconcile_failed",
                    job_id=str(job_id),
                    error_type=type(exc).__name__,
                )
            else:
                reconciled += 1
        result = ReconcileResult(dispatch_results, released, reconciled)
        logger.info(
            "control_plane.reconcile_completed",
            dispatch_results=[item.value for item in dispatch_results],
            provider_leases_released=released,
            stuck_jobs_found=len(job_ids),
            jobs_reconciled=reconciled,
        )
        return result

    def _release_expired_provider_leases(self) -> int:
        stale_before = self.clock() - self.provider_lease
        with self.session_factory() as db:
            result = db.execute(
                update(ProviderEventInbox)
                .where(
                    ProviderEventInbox.status == ProviderEventInboxStatus.PROCESSING,
                    ProviderEventInbox.locked_at <= stale_before,
                )
                .values(
                    status=ProviderEventInboxStatus.RECEIVED,
                    locked_at=None,
                    lock_token=None,
                )
            )
            db.commit()
            return result.rowcount

    def _stuck_job_ids(self) -> list[uuid.UUID]:
        stale_before = self.clock() - self.stuck_after
        with self.session_factory() as db:
            attempt_job_ids = select(GenerationAttempt.job_id).where(
                GenerationAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                GenerationAttempt.updated_at <= stale_before,
            )
            return list(
                db.scalars(
                    select(GenerationJob.id)
                    .where(
                        GenerationJob.status.in_(ACTIVE_JOB_STATUSES),
                        GenerationJob.updated_at <= stale_before,
                        GenerationJob.id.in_(attempt_job_ids),
                    )
                    .order_by(GenerationJob.updated_at, GenerationJob.id)
                    .limit(self.batch_size)
                )
            )


def operational_metrics(
    db: Session,
    *,
    stuck_after: timedelta = timedelta(minutes=5),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, int | float | None]:
    now = clock()
    pending_outbox = (
        db.scalar(
            select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.status.in_([OutboxStatus.PENDING, OutboxStatus.PROCESSING])
            )
        )
        or 0
    )
    pending_cleanup = (
        db.scalar(
            select(func.count()).select_from(StorageCleanupEvent).where(
                StorageCleanupEvent.status.in_([OutboxStatus.PENDING, OutboxStatus.PROCESSING])
            )
        )
        or 0
    )
    oldest = db.scalar(
        select(func.min(OutboxEvent.created_at)).where(
            OutboxEvent.status.in_([OutboxStatus.PENDING, OutboxStatus.PROCESSING])
        )
    )
    cleanup_oldest = db.scalar(
        select(func.min(StorageCleanupEvent.created_at)).where(
            StorageCleanupEvent.status.in_([OutboxStatus.PENDING, OutboxStatus.PROCESSING])
        )
    )
    candidates = [value for value in (oldest, cleanup_oldest) if value is not None]
    oldest_at = min(candidates) if candidates else None
    if oldest_at is not None and oldest_at.tzinfo is None:
        oldest_at = oldest_at.replace(tzinfo=UTC)
    retries = (db.scalar(select(func.sum(OutboxEvent.attempt_count))) or 0) + (
        db.scalar(select(func.sum(StorageCleanupEvent.attempt_count))) or 0
    )
    dead_letters = (
        db.scalar(
            select(func.count()).select_from(DeadLetterEvent).where(
                DeadLetterEvent.status == DeadLetterStatus.OPEN
            )
        )
        or 0
    )
    stuck_jobs = (
        db.scalar(
            select(func.count()).select_from(GenerationJob).where(
                GenerationJob.status.in_(ACTIVE_JOB_STATUSES),
                GenerationJob.updated_at <= now - stuck_after,
            )
        )
        or 0
    )
    return {
        "pending_count": pending_outbox + pending_cleanup,
        "oldest_pending_age_seconds": (
            max(0.0, (now - oldest_at).total_seconds()) if oldest_at else None
        ),
        "retry_count": retries,
        "dead_letter_count": dead_letters,
        "stuck_job_count": stuck_jobs,
    }


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    checks: dict[str, bool]


class ReadinessService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        storage_check: Callable[[], bool],
        workflow_check: Callable[[], bool],
        *,
        expected_revision: str = "0012_optional_media_hashes",
    ) -> None:
        self.session_factory = session_factory
        self.storage_check = storage_check
        self.workflow_check = workflow_check
        self.expected_revision = expected_revision

    def check(self) -> ReadinessResult:
        checks = {
            "database": self._database_ready(),
            "migrations": self._migrations_ready(),
            "storage": self._safe_check(self.storage_check),
            "workflow": self._safe_check(self.workflow_check),
        }
        return ReadinessResult(ready=all(checks.values()), checks=checks)

    def _database_ready(self) -> bool:
        try:
            with self.session_factory() as db:
                db.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def _migrations_ready(self) -> bool:
        try:
            with self.session_factory() as db:
                revision = db.scalar(text("SELECT version_num FROM alembic_version"))
                return revision == self.expected_revision
        except Exception:
            return False

    @staticmethod
    def _safe_check(check: Callable[[], bool]) -> bool:
        try:
            return bool(check())
        except Exception:
            return False
