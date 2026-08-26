import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.ledger import finish_reservation
from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobEvent,
    JobStatus,
    OutputValidationStatus,
    Shot,
)
from app.provider import (
    FailureCode,
    MockVideoProvider,
    PollResult,
    ProviderAttempt,
    ProviderFailure,
    ProviderOutput,
    ProviderStatus,
    SubmitDisposition,
    SubmitRequest,
    VideoProvider,
    is_retryable_failure,
)
from app.state_machine import transition_attempt, transition_job
from app.storage import LocalObjectStorage

MAX_ATTEMPTS_PER_CANDIDATE = 2
MAX_ATTEMPTS_PER_JOB = 3


@dataclass(frozen=True, slots=True)
class AttemptBudget:
    total_attempts: int
    candidate_attempts: int

    @property
    def allows_retry(self) -> bool:
        return (
            self.total_attempts < MAX_ATTEMPTS_PER_JOB
            and self.candidate_attempts < MAX_ATTEMPTS_PER_CANDIDATE
        )


@dataclass(frozen=True, slots=True)
class AttemptContext:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    provider_code: str
    provider_job_id: str | None
    workflow_version: str
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


class GenerationExecutionService:
    """Owns persistence and storage around the side-effect-free Provider contract."""

    def __init__(
        self,
        storage: LocalObjectStorage,
        provider: VideoProvider | None = None,
        session_factory: sessionmaker[Session] = SessionLocal,
    ) -> None:
        self._storage = storage
        self._provider = provider or MockVideoProvider()
        self._session_factory = session_factory

    async def execute(self, job_id: uuid.UUID) -> None:
        while context := self._load_active_attempt(job_id):
            if context.status == AttemptStatus.CREATED:
                if not self._start_submit(context):
                    return
                context = self._load_active_attempt(job_id)
                if context is None:
                    return
                try:
                    submitted = await self._provider.submit(
                        SubmitRequest(
                            attempt_id=context.attempt_id,
                            idempotency_key=context.idempotency_key,
                            prompt=context.prompt,
                            negative_prompt=context.negative_prompt,
                            duration_ms=context.duration_ms,
                            aspect_ratio=context.aspect_ratio,
                            resolution=context.resolution,
                            workflow_version=context.workflow_version,
                            mode=context.mode,
                        )
                    )
                except Exception:
                    submitted = None
                if submitted is not None and submitted.disposition == SubmitDisposition.ACCEPTED:
                    if submitted.provider_job_id is None:
                        self._record_submit_unknown(context)
                    else:
                        self._record_submit_accepted(context, submitted.provider_job_id)
                else:
                    self._record_submit_unknown(context)

            context = self._load_active_attempt(job_id)
            if context is None:
                return
            if context.status not in {
                AttemptStatus.SUBMITTING,
                AttemptStatus.SUBMITTED,
                AttemptStatus.RUNNING,
            }:
                return

            try:
                result = await self._provider.poll(context.provider_attempt())
            except Exception:
                self._record_reconcile_pending(context)
                return
            if result.provider_job_id and context.provider_job_id is None:
                self._record_submit_accepted(context, result.provider_job_id, reconciled=True)
                context = self._load_active_attempt(job_id)
                if context is None:
                    return
            if result.status in {ProviderStatus.PENDING, ProviderStatus.RUNNING}:
                return
            if result.status == ProviderStatus.UNKNOWN:
                self._record_reconcile_pending(context)
                return
            if result.status == ProviderStatus.SUCCEEDED:
                if result.output is None:
                    result = PollResult(
                        status=ProviderStatus.FAILED,
                        provider_job_id=result.provider_job_id,
                        failure=ProviderFailure(
                            FailureCode.OUTPUT_MISSING, "Provider 未返回输出"
                        ),
                    )
                else:
                    self._finish_output(context, result.output)
                    return
            if result.status == ProviderStatus.CANCELLED:
                result = PollResult(
                    status=ProviderStatus.FAILED,
                    provider_job_id=result.provider_job_id,
                    failure=ProviderFailure(FailureCode.USER_CANCELLED, "Provider 已取消任务"),
                )
            if result.status == ProviderStatus.FAILED:
                failure = result.failure or ProviderFailure(
                    FailureCode.INTERNAL_ERROR, "Provider 返回未分类错误"
                )
                if not self._fail_attempt(context, failure):
                    return

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
            shot = db.get(Shot, job.shot_id)
            if attempt is None or shot is None:
                return None
            return AttemptContext(
                job_id=job.id,
                attempt_id=attempt.id,
                provider_code=attempt.provider_code,
                provider_job_id=attempt.provider_job_id,
                workflow_version=attempt.workflow_version,
                prompt=shot.prompt,
                negative_prompt=None,
                duration_ms=job.duration_ms,
                aspect_ratio=job.aspect_ratio,
                resolution=job.resolution,
                mode=job.mock_mode,
                status=attempt.status,
            )

    def _start_submit(self, context: AttemptContext) -> bool:
        with self._session_factory() as db:
            job = db.get(GenerationJob, context.job_id)
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if job is None or attempt is None or attempt.status != AttemptStatus.CREATED:
                return False
            if job.status == JobStatus.QUEUED and not transition_job(
                db, job, JobStatus.ROUTING, "job.routing", f"job:{job.id}:routing:v1"
            ):
                return False
            if not transition_attempt(
                db,
                attempt,
                AttemptStatus.SUBMITTING,
                "attempt.submitting",
                f"attempt:{attempt.id}:submitting:v1",
            ):
                return False
            db.commit()
            return True

    def _record_submit_accepted(
        self, context: AttemptContext, provider_job_id: str, *, reconciled: bool = False
    ) -> None:
        with self._session_factory() as db:
            job = db.get(GenerationJob, context.job_id)
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if job is None or attempt is None:
                return
            if attempt.provider_job_id not in {None, provider_job_id}:
                return
            attempt.provider_job_id = provider_job_id
            if job.status == JobStatus.ROUTING:
                transition_job(
                    db,
                    job,
                    JobStatus.SUBMITTED,
                    "provider.reconciled" if reconciled else "provider.submitted",
                    f"attempt:{attempt.id}:job-submitted:v1",
                    {"provider_job_id": provider_job_id},
                )
            if attempt.status == AttemptStatus.SUBMITTING:
                transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.SUBMITTED,
                    "attempt.reconciled" if reconciled else "attempt.submitted",
                    f"attempt:{attempt.id}:submitted:v1",
                    {"provider_job_id": provider_job_id},
                )
            if job.status == JobStatus.SUBMITTED:
                transition_job(
                    db,
                    job,
                    JobStatus.RUNNING,
                    "provider.started",
                    f"attempt:{attempt.id}:job-running:v1",
                )
                job.started_at = job.started_at or datetime.now(UTC)
            if attempt.status == AttemptStatus.SUBMITTED:
                transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.RUNNING,
                    "attempt.running",
                    f"attempt:{attempt.id}:running:v1",
                )
                attempt.started_at = attempt.started_at or datetime.now(UTC)
            db.commit()

    def _record_submit_unknown(self, context: AttemptContext) -> None:
        with self._session_factory() as db:
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if attempt is None or attempt.status != AttemptStatus.SUBMITTING:
                return
            self._add_same_state_event(
                db,
                attempt,
                "provider.submit_unknown",
                f"attempt:{attempt.id}:submit-unknown:v1",
            )
            db.commit()

    def _record_reconcile_pending(self, context: AttemptContext) -> None:
        with self._session_factory() as db:
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if attempt is None:
                return
            self._add_same_state_event(
                db,
                attempt,
                "provider.reconcile_pending",
                f"attempt:{attempt.id}:reconcile-pending:v1",
            )
            db.commit()

    @staticmethod
    def _add_same_state_event(
        db: Session,
        attempt: GenerationAttempt,
        event_type: str,
        dedup_key: str,
    ) -> None:
        exists = db.scalar(select(JobEvent.id).where(JobEvent.dedup_key == dedup_key))
        if exists is None:
            db.add(
                JobEvent(
                    job_id=attempt.job_id,
                    attempt_id=attempt.id,
                    event_type=event_type,
                    from_status=attempt.status.value,
                    to_status=attempt.status.value,
                    dedup_key=dedup_key,
                    payload_json={},
                )
            )

    def _fail_attempt(self, context: AttemptContext, failure: ProviderFailure) -> bool:
        retryable = is_retryable_failure(failure.code)
        with self._session_factory() as db:
            job = db.get(GenerationJob, context.job_id)
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if job is None or attempt is None:
                return False
            target = (
                AttemptStatus.FAILED_RETRYABLE if retryable else AttemptStatus.FAILED_FINAL
            )
            if not transition_attempt(
                db,
                attempt,
                target,
                "attempt.failed",
                f"attempt:{attempt.id}:terminal-failed:v1",
                {"failure_code": failure.code.value},
            ):
                return False
            attempt.failure_code = failure.code.value
            attempt.finished_at = datetime.now(UTC)
            if retryable and self._attempt_budget(db, attempt).allows_retry:
                retry = GenerationAttempt(
                    job_id=job.id,
                    attempt_no=attempt.attempt_no + 1,
                    provider_endpoint_id=attempt.provider_endpoint_id,
                    provider_code=attempt.provider_code,
                    workflow_version=attempt.workflow_version,
                    status=AttemptStatus.CREATED,
                )
                db.add(retry)
                db.flush()
                db.add(
                    JobEvent(
                        job_id=job.id,
                        attempt_id=retry.id,
                        event_type="attempt.retry_created",
                        from_status=None,
                        to_status=AttemptStatus.CREATED.value,
                        dedup_key=f"attempt:{attempt.id}:retry:v1",
                        payload_json={"failure_code": failure.code.value},
                    )
                )
                db.commit()
                return True
            if transition_job(
                db,
                job,
                JobStatus.FAILED_FINAL,
                "provider.failed",
                f"job:{job.id}:terminal-failed:v1",
                {"failure_code": failure.code.value},
            ):
                job.failure_code = failure.code.value
                job.error_message = failure.message
                job.finished_at = datetime.now(UTC)
                finish_reservation(db, job, settle=False)
                db.commit()
            else:
                db.rollback()
            return False

    @staticmethod
    def _attempt_budget(db: Session, attempt: GenerationAttempt) -> AttemptBudget:
        candidate_filter = (
            GenerationAttempt.provider_endpoint_id == attempt.provider_endpoint_id
            if attempt.provider_endpoint_id is not None
            else GenerationAttempt.provider_code == attempt.provider_code
        )
        total = db.scalar(
            select(func.count())
            .select_from(GenerationAttempt)
            .where(GenerationAttempt.job_id == attempt.job_id)
        )
        candidate = db.scalar(
            select(func.count())
            .select_from(GenerationAttempt)
            .where(GenerationAttempt.job_id == attempt.job_id, candidate_filter)
        )
        return AttemptBudget(total_attempts=total or 0, candidate_attempts=candidate or 0)

    def _finish_output(self, context: AttemptContext, output: ProviderOutput) -> None:
        stored = self._storage.write_bytes("outputs", output.content, output.media_type)
        with self._session_factory() as db:
            job = db.get(GenerationJob, context.job_id)
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if (
                job is None
                or attempt is None
                or job.status != JobStatus.RUNNING
                or attempt.status != AttemptStatus.RUNNING
            ):
                self._storage.delete(stored.key)
                return
            if not transition_job(
                db,
                job,
                JobStatus.POSTPROCESSING,
                "output.postprocessing",
                f"attempt:{attempt.id}:postprocessing:v1",
            ):
                self._storage.delete(stored.key)
                return
            valid = len(output.content) >= 8 and output.content[4:8] == b"ftyp"
            generated = GenerationOutput(
                job_id=job.id,
                attempt_id=attempt.id,
                object_key=stored.key,
                media_type=stored.mime_type,
                duration_ms=output.duration_ms,
                width=output.width,
                height=output.height,
                fps=output.fps,
                codec=output.codec,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                validation_status=(
                    OutputValidationStatus.VALID if valid else OutputValidationStatus.INVALID
                ),
            )
            db.add(generated)
            db.flush()
            if not transition_job(
                db,
                job,
                JobStatus.VALIDATING,
                "output.validating",
                f"attempt:{attempt.id}:validating:v1",
            ):
                db.rollback()
                self._storage.delete(stored.key)
                return
            if not valid:
                failure = ProviderFailure(
                    FailureCode.OUTPUT_INVALID_MEDIA, "Provider 输出不是有效 MP4"
                )
                if not transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.FAILED_FINAL,
                    "attempt.failed",
                    f"attempt:{attempt.id}:terminal-invalid-output:v1",
                    {"failure_code": failure.code.value},
                ) or not transition_job(
                    db,
                    job,
                    JobStatus.FAILED_FINAL,
                    "output.invalid",
                    f"job:{job.id}:terminal-invalid-output:v1",
                    {"failure_code": failure.code.value},
                ):
                    db.rollback()
                    self._storage.delete(stored.key)
                    return
                attempt.failure_code = failure.code.value
                attempt.finished_at = datetime.now(UTC)
                job.failure_code = failure.code.value
                job.error_message = failure.message
                job.finished_at = datetime.now(UTC)
                finish_reservation(db, job, settle=False)
                db.commit()
                return
            job.final_output_id = generated.id
            if not transition_job(
                db,
                job,
                JobStatus.SUCCEEDED,
                "provider.completed",
                f"job:{job.id}:terminal-succeeded:v1",
                {"output_id": str(generated.id)},
            ) or not transition_attempt(
                db,
                attempt,
                AttemptStatus.SUCCEEDED,
                "attempt.succeeded",
                f"attempt:{attempt.id}:terminal-succeeded:v1",
                {"output_id": str(generated.id)},
            ):
                db.rollback()
                self._storage.delete(stored.key)
                return
            job.finished_at = datetime.now(UTC)
            attempt.finished_at = datetime.now(UTC)
            finish_reservation(db, job, settle=True)
            db.commit()
