import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    JobStatus,
    ProjectAsset,
    Shot,
    ShotReference,
)
from app.provider import ProviderAttempt


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

    def provider_attempt(self) -> ProviderAttempt:
        return ProviderAttempt(
            attempt_id=self.attempt_id,
            idempotency_key=self.idempotency_key,
            provider_job_id=self.provider_job_id,
            mode=self.mode,
        )


class AttemptContextService:
    """Loads immutable execution context from the database truth."""

    _session_factory: sessionmaker[Session]

    def _context_for(
        self,
        db: Session,
        job: GenerationJob,
        attempt: GenerationAttempt,
    ) -> AttemptContext | None:
        shot = db.get(Shot, job.shot_id)
        if shot is None:
            return None
        reference_object_key = db.scalar(
            select(ProjectAsset.object_key)
            .join(
                ShotReference,
                (ShotReference.asset_id == ProjectAsset.id)
                & (ShotReference.project_id == ProjectAsset.project_id),
            )
            .where(ShotReference.shot_id == shot.id)
            .order_by(ShotReference.created_at, ShotReference.id)
            .limit(1)
        )
        return AttemptContext(
            job_id=job.id,
            attempt_id=attempt.id,
            provider_code=attempt.provider_code,
            provider_job_id=attempt.provider_job_id,
            workflow_version=attempt.workflow_version,
            route_candidate_id=job.selected_route_candidate_id,
            reference_object_key=reference_object_key,
            prompt=shot.prompt,
            negative_prompt=None,
            duration_ms=job.duration_ms,
            aspect_ratio=job.aspect_ratio,
            resolution=job.resolution,
            mode=job.mock_mode,
            status=attempt.status,
        )

    def _load_active_attempt(self, job_id: uuid.UUID) -> AttemptContext | None:
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
            return self._context_for(db, job, attempt)

    def _load_attempt_by_provider_job(
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
            return self._context_for(db, job, attempt)
