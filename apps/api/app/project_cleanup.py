import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.artifact_lifecycle import cleanup_due
from app.dead_letters import add_dead_letter
from app.errors import ApiError
from app.models import (
    DeadLetterSource,
    GenerationJob,
    GenerationOutput,
    OutboxStatus,
    Project,
    ProjectAsset,
    ProjectAssetStatus,
    ProjectStatus,
    ProviderArtifact,
    StorageCleanupEvent,
    StorageCleanupObject,
    StorageCleanupObjectKind,
    StorageCleanupObjectStatus,
)
from app.outbox import DispatchResult
from app.state_machine import JOB_TERMINAL_STATUSES
from app.storage import ObjectStorage

logger = structlog.get_logger()


def storage_cleanup_key(project_id: uuid.UUID) -> str:
    return f"project:{project_id}:storage-cleanup:v1"


def request_project_deletion(
    db: Session,
    project: Project,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StorageCleanupEvent:
    existing = db.scalar(
        select(StorageCleanupEvent).where(StorageCleanupEvent.project_id == project.id)
    )
    if project.status == ProjectStatus.DELETED:
        if existing is None:
            raise ApiError(
                500,
                "PROJECT_CLEANUP_EVENT_MISSING",
                "已删除项目缺少 Storage Cleanup 事件",
            )
        return existing

    active_job_id = db.scalar(
        select(GenerationJob.id)
        .where(
            GenerationJob.project_id == project.id,
            GenerationJob.status.not_in(JOB_TERMINAL_STATUSES),
        )
        .limit(1)
    )
    if active_job_id is not None:
        raise ApiError(409, "PROJECT_HAS_ACTIVE_JOBS", "项目仍有进行中的生成任务")
    if existing is not None:
        raise ApiError(
            500,
            "PROJECT_CLEANUP_STATE_INVALID",
            "活动项目已经存在 Storage Cleanup 事件",
        )

    cleanup_objects: dict[str, StorageCleanupObjectKind] = {}
    for object_key in db.scalars(
        select(ProjectAsset.object_key).where(ProjectAsset.project_id == project.id)
    ):
        cleanup_objects[object_key] = StorageCleanupObjectKind.ASSET
    for object_key in db.scalars(
        select(GenerationOutput.object_key)
        .join(GenerationJob, GenerationJob.id == GenerationOutput.job_id)
        .where(GenerationJob.project_id == project.id)
    ):
        cleanup_objects.setdefault(object_key, StorageCleanupObjectKind.OUTPUT)

    now = clock()
    retained = {}
    for artifact, job in db.execute(
        select(ProviderArtifact, GenerationJob)
        .join(GenerationJob, GenerationJob.id == ProviderArtifact.job_id)
        .where(ProviderArtifact.project_id == project.id)
    ):
        cleanup_objects.setdefault(artifact.object_key, StorageCleanupObjectKind.PROVIDER)
        retained[artifact.object_key] = cleanup_due(artifact, job)
    project.status = ProjectStatus.DELETED
    project.deleted_at = now
    event = StorageCleanupEvent(
        project_id=project.id,
        idempotency_key=storage_cleanup_key(project.id),
        status=OutboxStatus.PENDING,
        next_attempt_at=now,
    )
    db.add(event)
    db.flush()
    db.add_all(
        StorageCleanupObject(
            event_id=event.id,
            object_key=object_key,
            object_kind=object_kind,
            status=StorageCleanupObjectStatus.PENDING,
            not_before=max(now, retained.get(object_key, now)),
        )
        for object_key, object_kind in sorted(cleanup_objects.items())
    )
    return event


@dataclass(frozen=True, slots=True)
class ClaimedStorageCleanupEvent:
    event_id: uuid.UUID
    project_id: uuid.UUID
    lock_token: str


@dataclass(frozen=True, slots=True)
class StorageCleanupRequest:
    event_id: uuid.UUID
    project_id: uuid.UUID
    object_id: uuid.UUID
    object_key: str
    object_kind: StorageCleanupObjectKind


class StorageCleanupDispatcher:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        storage: ObjectStorage,
        *,
        lease_duration: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=1),
        max_attempts: int = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.lease_duration = lease_duration
        self.retry_delay = retry_delay
        self.max_attempts = max_attempts
        self.clock = clock
        if max_attempts <= 0:
            raise ValueError("storage cleanup max attempts must be positive")

    async def dispatch_once(self) -> DispatchResult:
        event = self._claim_one()
        if event is None:
            return DispatchResult.IDLE
        self.after_claim(event)

        while request := self._next_object(event):
            self._renew_lease(event)
            self._start_object_attempt(event, request)
            try:
                self.storage.delete(request.object_key)
            except ApiError as exc:
                if exc.code != "STORAGE_OBJECT_NOT_FOUND":
                    self._mark_object_failure(event, request, exc)
                    return self._schedule_retry(event, exc)
            except Exception as exc:
                self._mark_object_failure(event, request, exc)
                return self._schedule_retry(event, exc)
            self.after_delete(event, request)
            self._mark_object_deleted(event, request)

        return self._mark_published(event)

    def after_claim(self, event: ClaimedStorageCleanupEvent) -> None:
        """Failure-injection seam for a crash after claiming the cleanup event."""

    def after_delete(
        self,
        event: ClaimedStorageCleanupEvent,
        request: StorageCleanupRequest,
    ) -> None:
        """Failure-injection seam for a crash after deleting an object."""

    def _dispatchable(self, now: datetime):
        stale_before = now - self.lease_duration
        return or_(
            and_(
                StorageCleanupEvent.status == OutboxStatus.PENDING,
                StorageCleanupEvent.next_attempt_at <= now,
            ),
            and_(
                StorageCleanupEvent.status == OutboxStatus.PROCESSING,
                StorageCleanupEvent.locked_at <= stale_before,
            ),
        )

    def _claim_one(self) -> ClaimedStorageCleanupEvent | None:
        now = self.clock()
        dispatchable = self._dispatchable(now)
        with self.session_factory() as db:
            try:
                candidates = list(
                    db.scalars(
                        select(StorageCleanupEvent)
                        .where(dispatchable)
                        .order_by(StorageCleanupEvent.created_at, StorageCleanupEvent.id)
                        .with_for_update(skip_locked=True)
                        .limit(8)
                    )
                )
                for candidate in candidates:
                    lock_token = str(uuid.uuid4())
                    claimed = db.execute(
                        update(StorageCleanupEvent)
                        .where(StorageCleanupEvent.id == candidate.id, dispatchable)
                        .values(
                            status=OutboxStatus.PROCESSING,
                            attempt_count=StorageCleanupEvent.attempt_count + 1,
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
                    return ClaimedStorageCleanupEvent(
                        event_id=candidate.id,
                        project_id=candidate.project_id,
                        lock_token=lock_token,
                    )
            except OperationalError:
                if db.get_bind().dialect.name != "sqlite":
                    raise
                db.rollback()
                logger.debug("storage_cleanup_outbox.claim_contended", database="sqlite")
                return None
        return None

    def _lease_owned(self, event: ClaimedStorageCleanupEvent):
        return exists().where(
            StorageCleanupEvent.id == event.event_id,
            StorageCleanupEvent.status == OutboxStatus.PROCESSING,
            StorageCleanupEvent.lock_token == event.lock_token,
        )

    def _next_object(self, event: ClaimedStorageCleanupEvent) -> StorageCleanupRequest | None:
        with self.session_factory() as db:
            cleanup_object = db.scalar(
                select(StorageCleanupObject)
                .where(
                    StorageCleanupObject.event_id == event.event_id,
                    StorageCleanupObject.status == StorageCleanupObjectStatus.PENDING,
                    StorageCleanupObject.not_before <= self.clock(),
                )
                .order_by(StorageCleanupObject.object_key, StorageCleanupObject.id)
                .limit(1)
            )
            if cleanup_object is None:
                return None
            return StorageCleanupRequest(
                event_id=event.event_id,
                project_id=event.project_id,
                object_id=cleanup_object.id,
                object_key=cleanup_object.object_key,
                object_kind=cleanup_object.object_kind,
            )

    def _renew_lease(self, event: ClaimedStorageCleanupEvent) -> None:
        now = self.clock()
        with self.session_factory() as db:
            renewed = db.execute(
                update(StorageCleanupEvent)
                .where(
                    StorageCleanupEvent.id == event.event_id,
                    StorageCleanupEvent.status == OutboxStatus.PROCESSING,
                    StorageCleanupEvent.lock_token == event.lock_token,
                )
                .values(locked_at=now)
            )
            if renewed.rowcount != 1:
                db.rollback()
                raise RuntimeError("storage cleanup outbox lease was lost while renewing")
            db.commit()

    def _start_object_attempt(
        self,
        event: ClaimedStorageCleanupEvent,
        request: StorageCleanupRequest,
    ) -> None:
        with self.session_factory() as db:
            started = db.execute(
                update(StorageCleanupObject)
                .where(
                    StorageCleanupObject.id == request.object_id,
                    StorageCleanupObject.status == StorageCleanupObjectStatus.PENDING,
                    self._lease_owned(event),
                )
                .values(
                    attempt_count=StorageCleanupObject.attempt_count + 1,
                    last_error=None,
                )
            )
            if started.rowcount != 1:
                db.rollback()
                raise RuntimeError("storage cleanup outbox lease was lost before object deletion")
            db.commit()

    def _mark_object_failure(
        self,
        event: ClaimedStorageCleanupEvent,
        request: StorageCleanupRequest,
        exc: Exception,
    ) -> None:
        with self.session_factory() as db:
            failed = db.execute(
                update(StorageCleanupObject)
                .where(
                    StorageCleanupObject.id == request.object_id,
                    StorageCleanupObject.status == StorageCleanupObjectStatus.PENDING,
                    self._lease_owned(event),
                )
                .values(last_error=f"{type(exc).__name__}: {exc}"[:2000])
            )
            if failed.rowcount != 1:
                db.rollback()
                raise RuntimeError("storage cleanup outbox lease was lost after object failure")
            db.commit()

    def _mark_object_deleted(
        self,
        event: ClaimedStorageCleanupEvent,
        request: StorageCleanupRequest,
    ) -> None:
        now = self.clock()
        with self.session_factory() as db:
            deleted = db.execute(
                update(StorageCleanupObject)
                .where(
                    StorageCleanupObject.id == request.object_id,
                    StorageCleanupObject.status == StorageCleanupObjectStatus.PENDING,
                    self._lease_owned(event),
                )
                .values(
                    status=StorageCleanupObjectStatus.DELETED,
                    cleaned_at=now,
                    last_error=None,
                )
            )
            if deleted.rowcount != 1:
                db.rollback()
                raise RuntimeError("storage cleanup outbox lease was lost after object deletion")
            if request.object_kind == StorageCleanupObjectKind.ASSET:
                db.execute(
                    update(ProjectAsset)
                    .where(
                        ProjectAsset.project_id == event.project_id,
                        ProjectAsset.object_key == request.object_key,
                    )
                    .values(status=ProjectAssetStatus.DELETED)
                )
            db.execute(
                update(ProviderArtifact)
                .where(
                    ProviderArtifact.project_id == event.project_id,
                    ProviderArtifact.object_key == request.object_key,
                )
                .values(
                    status=OutboxStatus.PUBLISHED,
                    cleaned_at=now,
                    locked_at=None,
                    lock_token=None,
                    last_error=None,
                )
            )
            db.commit()

    def _schedule_retry(self, event: ClaimedStorageCleanupEvent, exc: Exception) -> DispatchResult:
        now = self.clock()
        error = f"{type(exc).__name__}: {exc}"[:2000]
        with self.session_factory() as db:
            source = db.scalar(
                select(StorageCleanupEvent)
                .where(
                    StorageCleanupEvent.id == event.event_id,
                    StorageCleanupEvent.status == OutboxStatus.PROCESSING,
                    StorageCleanupEvent.lock_token == event.lock_token,
                )
                .with_for_update()
            )
            if source is None:
                db.rollback()
                raise RuntimeError("storage cleanup outbox lease was lost while retrying")
            if source.attempt_count >= self.max_attempts:
                source.status = OutboxStatus.DEAD_LETTER
                add_dead_letter(
                    db,
                    source_type=DeadLetterSource.STORAGE_CLEANUP,
                    source_id=source.id,
                    event_type="project.storage.cleanup",
                    payload={"project_id": str(source.project_id)},
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
                "storage_cleanup_outbox.dead_lettered",
                event_id=str(event.event_id),
                project_id=str(event.project_id),
                attempt_count=self.max_attempts,
                error_type=type(exc).__name__,
            )
            return result
        logger.warning(
            "storage_cleanup_outbox.retry_scheduled",
            event_id=str(event.event_id),
            project_id=str(event.project_id),
            error_type=type(exc).__name__,
        )
        return result

    def _mark_published(self, event: ClaimedStorageCleanupEvent) -> DispatchResult:
        now = self.clock()
        pending_objects = exists().where(
            StorageCleanupObject.event_id == event.event_id,
            StorageCleanupObject.status == StorageCleanupObjectStatus.PENDING,
        )
        with self.session_factory() as db:
            next_due = db.scalar(
                select(func.min(StorageCleanupObject.not_before)).where(
                    StorageCleanupObject.event_id == event.event_id,
                    StorageCleanupObject.status == StorageCleanupObjectStatus.PENDING,
                )
            )
            if next_due is not None:
                deferred = db.execute(
                    update(StorageCleanupEvent)
                    .where(
                        StorageCleanupEvent.id == event.event_id,
                        StorageCleanupEvent.lock_token == event.lock_token,
                        StorageCleanupEvent.status == OutboxStatus.PROCESSING,
                    )
                    .values(
                        status=OutboxStatus.PENDING,
                        next_attempt_at=next_due,
                        locked_at=None,
                        lock_token=None,
                        attempt_count=StorageCleanupEvent.attempt_count - 1,
                    )
                )
                if deferred.rowcount != 1:
                    raise RuntimeError(
                        "storage cleanup lease lost while deferring retained objects"
                    )
                db.commit()
                return DispatchResult.RETRY_SCHEDULED
            published = db.execute(
                update(StorageCleanupEvent)
                .where(
                    StorageCleanupEvent.id == event.event_id,
                    StorageCleanupEvent.status == OutboxStatus.PROCESSING,
                    StorageCleanupEvent.lock_token == event.lock_token,
                    ~pending_objects,
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
                raise RuntimeError("storage cleanup outbox lease was lost while publishing")
            db.commit()
        logger.info(
            "storage_cleanup_outbox.published",
            event_id=str(event.event_id),
            project_id=str(event.project_id),
        )
        return DispatchResult.PUBLISHED
