import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, exists, func, or_, select, update

from app.config import get_settings
from app.errors import ApiError
from app.models import (
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    OutboxStatus,
    Project,
    ProjectStatus,
    ProviderArtifact,
    StorageCleanupEvent,
    StorageCleanupObject,
    StorageCleanupObjectKind,
    StorageCleanupObjectStatus,
)
from app.outbox import DispatchResult
from app.state_machine import JOB_TERMINAL_STATUSES
from app.storage import _valid_object_key


def retention():
    return timedelta(seconds=get_settings().provider_artifact_retention_seconds)


def validate_artifact_key(job_id, attempt_id, key, kind):
    _valid_object_key(key)
    prefix = (
        f"provider-outputs/{job_id}/{attempt_id}/" if kind == "SOURCE" else f"outputs/{job_id}/"
    )
    if kind not in {"SOURCE", "FINAL"} or not key.startswith(prefix):
        raise ValueError("artifact key is outside the owning attempt namespace")


def cleanup_due(artifact, job):
    return max(artifact.retain_until, (job.finished_at or job.updated_at) + retention())


def register_artifact(
    session_factory, job_id, attempt_id, key, kind, *, expires_at=None, clock=None
):
    now = clock() if clock else datetime.now(UTC)
    validate_artifact_key(job_id, attempt_id, key, kind)
    with session_factory() as db:
        job = db.get(GenerationJob, job_id)
        attempt = db.get(GenerationAttempt, attempt_id)
        if job is None or attempt is None or attempt.job_id != job_id:
            raise ValueError("artifact attempt is not owned by job")
        project = db.scalar(select(Project).where(Project.id == job.project_id).with_for_update())
        artifact = db.scalar(select(ProviderArtifact).where(ProviderArtifact.object_key == key))
        if artifact is None:
            artifact = ProviderArtifact(
                project_id=job.project_id,
                job_id=job_id,
                attempt_id=attempt_id,
                object_key=key,
                kind=kind,
                retain_until=max(expires_at or now, now + retention()),
                next_attempt_at=now,
            )
            db.add(artifact)
            db.flush()
        elif (
            artifact.job_id != job_id or artifact.attempt_id != attempt_id or artifact.kind != kind
        ):
            raise ValueError("artifact identity conflict")
        elif artifact.status == OutboxStatus.PUBLISHED:
            artifact.status = OutboxStatus.PENDING
            artifact.cleaned_at = None
            artifact.retain_until = max(expires_at or now, now + retention())
        if project.status == ProjectStatus.DELETED:
            event = db.scalar(
                select(StorageCleanupEvent)
                .where(StorageCleanupEvent.project_id == project.id)
                .with_for_update()
            )
            item = db.scalar(
                select(StorageCleanupObject).where(
                    StorageCleanupObject.event_id == event.id,
                    StorageCleanupObject.object_key == key,
                )
            )
            if item is None:
                item = StorageCleanupObject(
                    event_id=event.id,
                    object_key=key,
                    object_kind=StorageCleanupObjectKind.PROVIDER,
                    not_before=cleanup_due(artifact, job),
                )
                db.add(item)
            elif artifact.status != OutboxStatus.PUBLISHED:
                item.status = StorageCleanupObjectStatus.PENDING
                item.not_before = cleanup_due(artifact, job)
            if event.status == OutboxStatus.PUBLISHED:
                event.status = OutboxStatus.PENDING
                event.next_attempt_at = item.not_before
                event.published_at = None
        db.commit()


class ArtifactCleanupDispatcher:
    """Durable outbox per claimed object, including writes that never returned a result."""

    def __init__(
        self,
        session_factory,
        storage,
        *,
        clock=lambda: datetime.now(UTC),
        lease=timedelta(seconds=30),
        max_attempts=5,
    ):
        self.sessions, self.storage, self.clock = session_factory, storage, clock
        self.lease, self.max_attempts = lease, max_attempts

    async def dispatch_once(self):
        now = self.clock()
        with self.sessions() as db:
            referenced = exists().where(GenerationOutput.object_key == ProviderArtifact.object_key)
            artifact = db.scalar(
                select(ProviderArtifact)
                .join(GenerationJob, GenerationJob.id == ProviderArtifact.job_id)
                .join(Project, Project.id == ProviderArtifact.project_id)
                .where(
                    GenerationJob.status.in_(JOB_TERMINAL_STATUSES),
                    func.coalesce(GenerationJob.finished_at, GenerationJob.updated_at)
                    <= now - retention(),
                    ProviderArtifact.retain_until <= now,
                    or_(
                        ProviderArtifact.kind == "SOURCE",
                        ~referenced,
                        Project.status == ProjectStatus.DELETED,
                    ),
                    or_(
                        and_(
                            ProviderArtifact.status == OutboxStatus.PENDING,
                            ProviderArtifact.next_attempt_at <= now,
                        ),
                        and_(
                            ProviderArtifact.status == OutboxStatus.PROCESSING,
                            ProviderArtifact.locked_at <= now - self.lease,
                        ),
                    ),
                )
                .order_by(ProviderArtifact.created_at, ProviderArtifact.id)
                .with_for_update(of=ProviderArtifact, skip_locked=True)
                .limit(1)
            )
            if artifact is None:
                return DispatchResult.IDLE
            token = str(uuid.uuid4())
            artifact.status, artifact.lock_token, artifact.locked_at = (
                OutboxStatus.PROCESSING,
                token,
                now,
            )
            artifact.attempt_count += 1
            db.commit()
            identity = artifact.id
            key = artifact.object_key
            validate_artifact_key(artifact.job_id, artifact.attempt_id, key, artifact.kind)
        self.after_claim(identity)
        try:
            try:
                self.storage.delete(key)
            except ApiError as exc:
                if exc.code != "STORAGE_OBJECT_NOT_FOUND":
                    raise
        except Exception as exc:
            with self.sessions() as db:
                artifact = db.scalar(
                    select(ProviderArtifact)
                    .where(
                        ProviderArtifact.id == identity,
                        ProviderArtifact.lock_token == token,
                        ProviderArtifact.status == OutboxStatus.PROCESSING,
                    )
                    .with_for_update()
                )
                if artifact is not None:
                    artifact.status = (
                        OutboxStatus.DEAD_LETTER
                        if artifact.attempt_count >= self.max_attempts
                        else OutboxStatus.PENDING
                    )
                    artifact.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                    artifact.next_attempt_at = now + timedelta(
                        seconds=min(60, 2 ** min(artifact.attempt_count, 6))
                    )
                    artifact.lock_token = None
                    artifact.locked_at = None
                    db.commit()
                    if artifact.status == OutboxStatus.DEAD_LETTER:
                        return DispatchResult.DEAD_LETTERED
            return DispatchResult.RETRY_SCHEDULED
        self.after_delete(identity)
        with self.sessions() as db:
            db.execute(
                update(ProviderArtifact)
                .where(
                    ProviderArtifact.id == identity,
                    ProviderArtifact.lock_token == token,
                    ProviderArtifact.status == OutboxStatus.PROCESSING,
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
        return DispatchResult.PUBLISHED

    def after_claim(self, identity):
        """Crash injection seam."""

    def after_delete(self, identity):
        """Crash injection seam before durable cleanup acknowledgement."""


def inventory_local_sources(root, session_factory, *, apply=False):
    """Identify pre-registry local sources by exact Job/Attempt namespace; never delete."""
    root = root.resolve()
    report = []
    for path in (root / "provider-outputs").rglob("*"):
        if not path.is_file() or root not in path.resolve().parents:
            continue
        key = path.relative_to(root).as_posix()
        parts = key.split("/")
        try:
            if len(parts) != 4:
                raise ValueError("unexpected source layout")
            job_id, attempt_id = uuid.UUID(parts[1]), uuid.UUID(parts[2])
            validate_artifact_key(job_id, attempt_id, key, "SOURCE")
            with session_factory() as db:
                attempt = db.get(GenerationAttempt, attempt_id)
                known = attempt is not None and attempt.job_id == job_id
        except (ValueError, ApiError):
            known = False
        if known and apply:
            register_artifact(session_factory, job_id, attempt_id, key, "SOURCE")
        report.append(
            {
                "object_key": key,
                "status": "REGISTERED"
                if known and apply
                else "ELIGIBLE"
                if known
                else "REVIEW_REQUIRED",
            }
        )
    return report
