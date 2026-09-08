from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.artifacts import (
    ArtifactReceiptError,
    MockArtifactReceiver,
    PublishedArtifact,
    RemoteArtifactReceiver,
)
from app.errors import ApiError
from app.media import MediaPolicy, MediaValidator
from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobEvent,
    JobStatus,
    OutputValidationStatus,
)
from app.provider import (
    FailureCode,
    PollResult,
    ProviderEvent,
    ProviderFailure,
    ProviderOutput,
    ProviderStatus,
    VideoProvider,
    is_retryable_failure,
)
from app.provider_execution_context import AttemptContext, AttemptContextService
from app.provider_settlement import ProviderSettlementService
from app.provider_submission import ProviderSubmissionService
from app.routing import MAX_PROVIDER_OUTPUT_BYTES
from app.state_machine import transition_attempt, transition_job
from app.storage import ObjectStorage

MAX_ATTEMPTS_PER_CANDIDATE = 2
MAX_ATTEMPTS_PER_JOB = 3
logger = structlog.get_logger()


class ProviderResultAction(StrEnum):
    WAIT = "WAIT"
    RETRY = "RETRY"
    COMPLETE = "COMPLETE"


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


class ProviderCompletionService:
    """Owns the single idempotent completion, publication, and settlement path."""

    _session_factory: sessionmaker[Session]
    _storage: ObjectStorage
    _artifact_receiver: RemoteArtifactReceiver

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        storage: ObjectStorage,
        artifact_receiver: RemoteArtifactReceiver,
        contexts: AttemptContextService,
        submission: ProviderSubmissionService,
        settlement: ProviderSettlementService,
        record_artifact: Callable[..., None],
        media_validator: MediaValidator,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._artifact_receiver = artifact_receiver
        self._contexts = contexts
        self._submission = submission
        self._settlement = settlement
        self._record_artifact = record_artifact
        self._mock_receiver = MockArtifactReceiver(
            storage, record_artifact, MAX_PROVIDER_OUTPUT_BYTES, media_validator
        )

    async def result_for_event(
        self,
        provider: VideoProvider,
        context: AttemptContext,
        event: ProviderEvent,
    ) -> PollResult:
        if event.status == ProviderStatus.CANCELLED:
            return PollResult(
                status=ProviderStatus.CANCELLED,
                provider_job_id=event.provider_job_id,
            )
        if event.status == ProviderStatus.FAILED and event.failure is not None:
            return PollResult(
                status=ProviderStatus.FAILED,
                provider_job_id=event.provider_job_id,
                failure=event.failure,
            )
        if event.status in {ProviderStatus.SUCCEEDED, ProviderStatus.FAILED}:
            return await provider.poll(context.provider_attempt())
        return PollResult(
            status=event.status,
            provider_job_id=event.provider_job_id,
        )

    def apply_provider_result(
        self, context: AttemptContext, result: PollResult
    ) -> ProviderResultAction:
        if result.provider_job_id and context.provider_job_id is None:
            self._submission.record_submit_accepted(
                context, result.provider_job_id, reconciled=True
            )
            refreshed = self._contexts.load_attempt_by_provider_job(
                context.provider_code, result.provider_job_id
            )
            if refreshed is None:
                return ProviderResultAction.WAIT
            context = refreshed
        if result.status in {ProviderStatus.PENDING, ProviderStatus.RUNNING}:
            return ProviderResultAction.WAIT
        if result.status == ProviderStatus.UNKNOWN:
            self._submission.record_reconcile_pending(context)
            return ProviderResultAction.WAIT
        return self.complete_provider_result(context, result)

    def complete_provider_result(
        self, context: AttemptContext, result: PollResult
    ) -> ProviderResultAction:
        """Single idempotent completion path for polling, webhooks, and cancellation."""

        if result.output is not None and result.output.object_key is not None:
            try:
                self._record_artifact(
                    context.job_id, context.attempt_id, result.output.object_key, "SOURCE"
                )
            except (ValueError, ApiError):
                self.fail_attempt(
                    context,
                    ProviderFailure(
                        FailureCode.OUTPUT_CORRUPTED, "Provider 输出对象不在受控命名空间"
                    ),
                )
                return ProviderResultAction.COMPLETE
        source_failure = self.cost_source_failure(context, result)
        if source_failure is not None:
            self.fail_attempt(context, source_failure)
            return ProviderResultAction.COMPLETE
        if result.status == ProviderStatus.SUCCEEDED:
            if result.output is not None:
                snapshot_failure = self.snapshot_failure(context, result)
                if snapshot_failure is None:
                    self.finish_output(context, result.output, result)
                    return ProviderResultAction.COMPLETE
                self.fail_attempt(context, snapshot_failure)
                return ProviderResultAction.COMPLETE
            result = PollResult(
                status=ProviderStatus.FAILED,
                provider_job_id=result.provider_job_id,
                failure=ProviderFailure(FailureCode.OUTPUT_MISSING, "Provider 未返回输出"),
            )
        if result.status == ProviderStatus.CANCELLED:
            self.finish_cancelled(context, result)
            return ProviderResultAction.COMPLETE
        failure = result.failure or ProviderFailure(
            FailureCode.INTERNAL_ERROR, "Provider 返回未分类错误"
        )
        return (
            ProviderResultAction.RETRY
            if self.fail_attempt(context, failure, result=result)
            else ProviderResultAction.COMPLETE
        )

    async def with_terminal_cost(
        self,
        provider: VideoProvider,
        context: AttemptContext,
        result: PollResult,
    ) -> PollResult:
        if (
            result.status
            not in {
                ProviderStatus.SUCCEEDED,
                ProviderStatus.FAILED,
                ProviderStatus.CANCELLED,
            }
            or result.cost is not None
        ):
            return result
        try:
            cost = await provider.read_cost(context.provider_attempt())
        except Exception as exc:
            logger.warning(
                "provider.cost_snapshot_failed",
                job_id=str(context.job_id),
                attempt_id=str(context.attempt_id),
                provider=context.provider_code,
                error_type=type(exc).__name__,
            )
            return result
        return replace(result, cost=cost)

    @staticmethod
    def snapshot_failure(context: AttemptContext, result: PollResult) -> ProviderFailure | None:
        if result.metrics is None or result.versions is None or result.cost is None:
            return ProviderFailure(
                FailureCode.INTERNAL_ERROR,
                "Provider 成功结果缺少完整版本、计时或成本快照",
            )
        if result.versions.workflow_version != context.workflow_version:
            return ProviderFailure(
                FailureCode.INTERNAL_ERROR,
                "Provider workflow 版本与 Attempt 不一致",
            )
        return None

    @staticmethod
    def cost_source_failure(context: AttemptContext, result: PollResult) -> ProviderFailure | None:
        if result.cost is None:
            return None
        simulated_provider = context.provider_code in {"mock", "runpod-simulator"}
        if simulated_provider == (result.cost.source.value == "SIMULATED"):
            return None
        return ProviderFailure(
            FailureCode.INTERNAL_ERROR,
            "Provider 成本来源与执行环境不一致",
        )

    @staticmethod
    def apply_attempt_snapshot(
        attempt: GenerationAttempt,
        result: PollResult,
    ) -> None:
        values: dict[str, object] = {}
        if result.versions is not None:
            versions = result.versions
            if attempt.workflow_version != versions.workflow_version:
                raise ValueError("provider workflow version does not match attempt")
            values.update(
                worker_version=versions.worker_version,
                image_digest=versions.image_digest,
                worker_commit=versions.worker_commit,
                comfyui_version=versions.comfyui_version,
                comfyui_commit=versions.comfyui_commit,
                workflow_hash=versions.workflow_hash,
                model_hashes_json=dict(versions.model_hashes),
            )
        if result.metrics is not None:
            metrics = result.metrics
            values.update(
                gpu_type=metrics.gpu_type,
                queue_ms=metrics.queue_ms,
                cold_start_ms=metrics.cold_start_ms,
                runtime_ms=metrics.runtime_ms,
                billable_ms=metrics.billable_ms,
                raw_metrics_json={
                    "gpu_type": metrics.gpu_type,
                    "queue_ms": metrics.queue_ms,
                    "cold_start_ms": metrics.cold_start_ms,
                    "runtime_ms": metrics.runtime_ms,
                    "billable_ms": metrics.billable_ms,
                },
            )
        if result.cost is not None:
            values.update(
                cost_minor=result.cost.amount_minor,
                cost_currency=result.cost.currency,
                cost_source=result.cost.source.value,
            )
        for field_name, value in values.items():
            existing = getattr(attempt, field_name)
            if existing is not None and existing != value:
                raise ValueError(f"attempt snapshot field {field_name} is immutable")
            setattr(attempt, field_name, value)

    def fail_attempt(
        self,
        context: AttemptContext,
        failure: ProviderFailure,
        *,
        target: AttemptStatus | None = None,
        result: PollResult | None = None,
    ) -> bool:
        retryable = is_retryable_failure(failure.code)
        with self._session_factory() as db:
            job = db.scalar(
                select(GenerationJob).where(GenerationJob.id == context.job_id).with_for_update()
            )
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if job is None or attempt is None:
                return False
            if result is not None:
                self.apply_attempt_snapshot(attempt, result)
            if job.status == JobStatus.CANCEL_REQUESTED:
                if not transition_job(
                    db,
                    job,
                    JobStatus.CANCELLED,
                    "provider.cancelled",
                    f"job:{job.id}:terminal-cancelled:v1",
                ) or not transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.CANCELLED,
                    "attempt.cancelled",
                    f"attempt:{attempt.id}:terminal-cancelled:v1",
                    {"failure_code": FailureCode.USER_CANCELLED.value},
                ):
                    db.rollback()
                    return False
                attempt.failure_code = FailureCode.USER_CANCELLED.value
                attempt.finished_at = datetime.now(UTC)
                job.failure_code = FailureCode.USER_CANCELLED.value
                job.finished_at = datetime.now(UTC)
                self._settlement.settle_released(db, job)
                db.commit()
                return False
            target = target or (
                AttemptStatus.FAILED_RETRYABLE if retryable else AttemptStatus.FAILED_FINAL
            )
            timed_out = target == AttemptStatus.TIMED_OUT
            if not transition_attempt(
                db,
                attempt,
                target,
                "attempt.timed_out" if timed_out else "attempt.failed",
                f"attempt:{attempt.id}:terminal-{'timed-out' if timed_out else 'failed'}:v1",
                {"failure_code": failure.code.value},
            ):
                return False
            attempt.failure_code = failure.code.value
            attempt.finished_at = datetime.now(UTC)
            if retryable and self.attempt_budget(db, attempt).allows_retry:
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
                self._settlement.settle_released(db, job)
                db.commit()
            else:
                db.rollback()
            return False

    @staticmethod
    def attempt_budget(db: Session, attempt: GenerationAttempt) -> AttemptBudget:
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

    def finish_output(
        self, context: AttemptContext, output: ProviderOutput, result: PollResult
    ) -> None:
        try:
            if output.object_key is not None:
                published = self._artifact_receiver.receive_and_publish(
                    job_id=context.job_id,
                    attempt_id=context.attempt_id,
                    output=output,
                    policy=MediaPolicy(context.duration_ms, context.aspect_ratio),
                )
                valid = True
            elif context.provider_code in {"mock", "runpod-simulator"}:
                # Mock has a fixed 2s landscape fixture; the offline simulator
                # declares the requested dimensions. Neither is a real route.
                policy = (
                    MediaPolicy(2000, "16:9")
                    if context.provider_code == "mock"
                    else MediaPolicy(context.duration_ms, context.aspect_ratio)
                )
                published, valid = self._mock_receiver.receive(
                    context.job_id, context.attempt_id, output, policy
                )
            else:
                raise ArtifactReceiptError(
                    ProviderFailure(FailureCode.OUTPUT_MISSING, "真实 Provider 必须返回受控对象")
                )
        except ArtifactReceiptError as exc:
            self.fail_attempt(context, exc.failure, result=result)
            return
        try:
            self.commit_output(context, published, result, valid=valid)
        except Exception:
            self._storage.delete(published.stored.key)
            raise

    def commit_output(
        self,
        context: AttemptContext,
        published: PublishedArtifact,
        result: PollResult,
        *,
        valid: bool,
    ) -> None:
        stored, facts = published.stored, published.facts
        with self._session_factory() as db:
            job = db.get(GenerationJob, context.job_id)
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if (
                job is None
                or attempt is None
                or job.status not in {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}
                or attempt.status != AttemptStatus.RUNNING
            ):
                self._storage.delete(stored.key)
                return
            self.apply_attempt_snapshot(attempt, result)
            if not transition_job(
                db,
                job,
                JobStatus.POSTPROCESSING,
                "output.postprocessing",
                f"attempt:{attempt.id}:postprocessing:v1",
            ):
                self._storage.delete(stored.key)
                return
            generated = GenerationOutput(
                job_id=job.id,
                attempt_id=attempt.id,
                object_key=stored.key,
                media_type=stored.mime_type,
                duration_ms=facts.duration_ms,
                width=facts.width,
                height=facts.height,
                fps=facts.frame_rate,
                codec=facts.codec,
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
                self._settlement.settle_released(db, job)
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
            self._settlement.settle_succeeded(db, job)
            db.commit()

    def finish_cancelled(self, context: AttemptContext, result: PollResult) -> None:
        with self._session_factory() as db:
            job = db.scalar(
                select(GenerationJob).where(GenerationJob.id == context.job_id).with_for_update()
            )
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if job is None or attempt is None:
                return
            self.apply_attempt_snapshot(attempt, result)
            if job.status not in {
                JobStatus.ROUTING,
                JobStatus.SUBMITTED,
                JobStatus.RUNNING,
                JobStatus.CANCEL_REQUESTED,
            }:
                return
            if not transition_job(
                db,
                job,
                JobStatus.CANCELLED,
                "provider.cancelled",
                f"job:{job.id}:terminal-cancelled:v1",
            ) or not transition_attempt(
                db,
                attempt,
                AttemptStatus.CANCELLED,
                "attempt.cancelled",
                f"attempt:{attempt.id}:terminal-cancelled:v1",
                {"failure_code": FailureCode.USER_CANCELLED.value},
            ):
                db.rollback()
                return
            attempt.failure_code = FailureCode.USER_CANCELLED.value
            attempt.finished_at = datetime.now(UTC)
            job.failure_code = FailureCode.USER_CANCELLED.value
            job.finished_at = datetime.now(UTC)
            self._settlement.settle_released(db, job)
            db.commit()
