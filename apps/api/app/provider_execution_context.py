import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.input_snapshot import JobInputSnapshot
from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    JobStatus,
)
from app.provider import ProviderAttempt, provider_cancel_key


@dataclass(frozen=True, slots=True)
class AttemptContext:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    provider_code: str
    provider_job_id: str | None
    workflow_version: str
    route_candidate_id: uuid.UUID | None
    reference_object_key: str | None
    prompt: str
    negative_prompt: str | None
    duration_ms: int
    aspect_ratio: str
    resolution: str
    mode: str
    status: AttemptStatus

    @property
    def idempotency_key(self) -> str:
        return f"attempt:{self.attempt_id}:submit:v1"

    def provider_attempt(self, *, idempotency_key: str | None = None) -> ProviderAttempt:
        return ProviderAttempt(
            attempt_id=self.attempt_id,
            idempotency_key=idempotency_key or self.idempotency_key,
            provider_job_id=self.provider_job_id,
            mode=self.mode,
        )

    def provider_cancel_attempt(self, idempotency_key: str | None = None) -> ProviderAttempt:
        expected = provider_cancel_key(self.attempt_id)
        if idempotency_key is not None and idempotency_key != expected:
            raise ValueError("provider cancel idempotency key is not bound to attempt_id")
        return self.provider_attempt(idempotency_key=expected)


class AttemptContextService:
    """Loads immutable execution context from the database truth."""

    _session_factory: sessionmaker[Session]

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def context_for(
        self,
        db: Session,
        job: GenerationJob,
        attempt: GenerationAttempt,
    ) -> AttemptContext | None:
        if job.input_snapshot_json is None:
            # Legacy jobs require draining/manual review, never guessed input.
            return None
        snapshot = JobInputSnapshot.model_validate(job.input_snapshot_json)
        reference_object_key = snapshot.references[0].object_key if snapshot.references else None
        return AttemptContext(
            job_id=job.id,
            attempt_id=attempt.id,
            provider_code=attempt.provider_code,
            provider_job_id=attempt.provider_job_id,
            workflow_version=attempt.workflow_version,
            route_candidate_id=job.selected_route_candidate_id,
            reference_object_key=reference_object_key,
            prompt=snapshot.prompt,
            negative_prompt=snapshot.negative_prompt,
            duration_ms=snapshot.duration_ms,
            aspect_ratio=snapshot.aspect_ratio,
            resolution=snapshot.resolution,
            mode=snapshot.mode,
            status=attempt.status,
        )

    def load_active_attempt(self, job_id: uuid.UUID) -> AttemptContext | None:
        with self._session_factory() as db:
            job = db.get(GenerationJob, job_id)
            if job is None or job.status in {
                JobStatus.SUCCEEDED,
                JobStatus.FAILED_FINAL,
                JobStatus.CANCELLED,
                JobStatus.EXPIRED,
                JobStatus.REJECTED_POLICY,
            }:
                return None
            attempt = db.scalar(
                select(GenerationAttempt)
                .where(GenerationAttempt.job_id == job_id)
                .order_by(GenerationAttempt.attempt_no.desc())
                .limit(1)
            )
            if attempt is None:
                return None
            return self.context_for(db, job, attempt)

    def load_cancellable_attempt(
        self, job_id: uuid.UUID, attempt_id: uuid.UUID
    ) -> AttemptContext | None:
        with self._session_factory() as db:
            job = db.get(GenerationJob, job_id)
            if job is None or job.status != JobStatus.CANCEL_REQUESTED:
                return None
            attempt = db.get(GenerationAttempt, attempt_id)
            if attempt is None or attempt.job_id != job_id:
                return None
            return self.context_for(db, job, attempt)

    def load_attempt_by_provider_job(
        self, provider_code: str, provider_job_id: str
    ) -> AttemptContext | None:
        with self._session_factory() as db:
            attempt = db.scalar(
                select(GenerationAttempt).where(
                    GenerationAttempt.provider_code == provider_code,
                    GenerationAttempt.provider_job_id == provider_job_id,
                )
            )
            if attempt is None:
                return None
            job = db.get(GenerationJob, attempt.job_id)
            if job is None:
                return None
            return self.context_for(db, job, attempt)
