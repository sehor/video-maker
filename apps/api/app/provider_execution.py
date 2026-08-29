import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.artifacts import ArtifactReceiptError, RemoteArtifactReceiver
from app.config import get_settings
from app.db import SessionLocal
from app.ledger import finish_reservation
from app.media import MediaPolicy, MediaValidator, create_media_validator
from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobEvent,
    JobStatus,
    OutputValidationStatus,
    ProjectAsset,
    ProviderEventInbox,
    ProviderEventInboxStatus,
    Shot,
    ShotReference,
)
from app.provider import (
    FailureCode,
    PollResult,
    ProviderAttempt,
    ProviderEvent,
    ProviderFailure,
    ProviderOutput,
    ProviderStatus,
    SubmitDisposition,
    SubmitRequest,
    VideoProvider,
    WebhookVerificationRequest,
    is_retryable_failure,
)
from app.provider_registry import ProviderRegistry
from app.routing import (
    MAX_PROVIDER_OUTPUT_BYTES,
    CallbackClaimIssuer,
    RouteRegistry,
    get_callback_claim_issuer,
    get_provider_registry,
    get_route_registry,
)
from app.state_machine import transition_attempt, transition_job
from app.storage import ObjectStorage

MAX_ATTEMPTS_PER_CANDIDATE = 2
MAX_ATTEMPTS_PER_JOB = 3
PROVIDER_EVENT_LEASE = timedelta(minutes=5)
logger = structlog.get_logger()


class ProviderResultAction(StrEnum):
    WAIT = "WAIT"
    RETRY = "RETRY"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class ProviderPollingPolicy:
    initial_delay: timedelta = timedelta(seconds=1)
    maximum_delay: timedelta = timedelta(seconds=30)
    deadline: timedelta = timedelta(minutes=30)
    maximum_polls: int = 120

    def __post_init__(self) -> None:
        if self.initial_delay <= timedelta(0):
            raise ValueError("initial polling delay must be positive")
        if self.maximum_delay < self.initial_delay:
            raise ValueError("maximum polling delay must not be shorter than initial delay")
        if self.deadline <= timedelta(0):
            raise ValueError("polling deadline must be positive")
        if self.maximum_polls <= 0:
            raise ValueError("maximum polls must be positive")

    def delay_after(self, poll_count: int) -> timedelta:
        if poll_count <= 0:
            raise ValueError("poll count must be positive")
        multiplier = 1 << min(poll_count - 1, 30)
        return min(self.initial_delay * multiplier, self.maximum_delay)


@dataclass(frozen=True, slots=True)
class ProviderExecutionStep:
    is_complete: bool
    poll_count: int
    retry_after: timedelta | None = None


@dataclass(frozen=True, slots=True)
class PollReservation:
    poll_count: int
    acquired: bool
    exhausted: bool


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


@dataclass(frozen=True, slots=True)
class ProviderWebhookResult:
    event_id: str
    status: ProviderEventInboxStatus


class GenerationExecutionService:
    """Owns persistence and storage around the side-effect-free Provider contract."""

    def __init__(
        self,
        storage: ObjectStorage,
        provider: VideoProvider | None = None,
        provider_registry: ProviderRegistry | None = None,
        route_registry: RouteRegistry | None = None,
        callback_claim_issuer: CallbackClaimIssuer | None = None,
        session_factory: sessionmaker[Session] = SessionLocal,
        polling_policy: ProviderPollingPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        media_validator: MediaValidator | None = None,
    ) -> None:
        self._storage = storage
        self._provider_override = provider
        self._providers = provider_registry or get_provider_registry()
        self._routes = route_registry or get_route_registry()
        self._callback_claims = callback_claim_issuer or get_callback_claim_issuer()
        self._claim_ttl = timedelta(seconds=get_settings().provider_claim_ttl_seconds)
        self._session_factory = session_factory
        self._polling = polling_policy or ProviderPollingPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._artifact_receiver = RemoteArtifactReceiver(
            storage,
            media_validator or create_media_validator(get_settings()),
            max_bytes=MAX_PROVIDER_OUTPUT_BYTES,
        )

    def _provider_for(self, provider_code: str) -> VideoProvider:
        if self._provider_override is not None:
            return self._provider_override
        return self._providers.get(provider_code)

    def _submit_request(self, context: AttemptContext) -> SubmitRequest:
        if context.route_candidate_id is None:
            raise RuntimeError("generation attempt has no immutable route candidate")
        route = self._routes.by_candidate_id(context.route_candidate_id)
        if (
            route.provider_code != context.provider_code
            or route.workflow_id != context.workflow_version
        ):
            raise RuntimeError("generation attempt does not match its route version")
        input_claim = (
            self._storage.read_claim(
                context.reference_object_key,
                expires_in=self._claim_ttl,
            ).token
            if context.reference_object_key is not None
            else None
        )
        if route.requires_input_claim and input_claim is None:
            raise RuntimeError("route requires a reference asset claim")
        output_claim = self._storage.write_claim(
            f"provider-outputs/{context.job_id}/{context.attempt_id}",
            mime_type="video/mp4",
            max_bytes=MAX_PROVIDER_OUTPUT_BYTES,
            expires_in=self._claim_ttl,
        ).token
        callback_claim = self._callback_claims.issue(
            job_id=context.job_id,
            attempt_id=context.attempt_id,
            route=route,
            expires_in=self._claim_ttl,
        )
        return SubmitRequest(
            job_id=context.job_id,
            attempt_id=context.attempt_id,
            idempotency_key=context.idempotency_key,
            prompt=context.prompt,
            negative_prompt=context.negative_prompt,
            duration_ms=context.duration_ms,
            aspect_ratio=context.aspect_ratio,
            resolution=context.resolution.lower(),
            workflow_id=context.workflow_version,
            input_claim=input_claim,
            output_claim=output_claim,
            callback_claim=callback_claim,
            mode=context.mode,
        )

    async def execute(self, job_id: uuid.UUID) -> ProviderExecutionStep:
        while context := self._load_active_attempt(job_id):
            provider = self._provider_for(context.provider_code)
            if context.status == AttemptStatus.CREATED:
                if not self._start_submit(context):
                    return ProviderExecutionStep(is_complete=False, poll_count=0)
                context = self._load_active_attempt(job_id)
                if context is None:
                    return ProviderExecutionStep(is_complete=True, poll_count=0)
                try:
                    submitted = await provider.submit(self._submit_request(context))
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
                return ProviderExecutionStep(is_complete=True, poll_count=0)
            if context.status not in {
                AttemptStatus.SUBMITTING,
                AttemptStatus.SUBMITTED,
                AttemptStatus.RUNNING,
            }:
                return ProviderExecutionStep(is_complete=True, poll_count=0)

            reservation = self._reserve_poll(context)
            if reservation.exhausted:
                action = self._fail_attempt(
                    context,
                    ProviderFailure(
                        FailureCode.QUEUE_TIMEOUT,
                        "Provider polling budget exhausted",
                    ),
                    target=AttemptStatus.TIMED_OUT,
                )
                if action:
                    continue
                return ProviderExecutionStep(
                    is_complete=True,
                    poll_count=reservation.poll_count,
                )
            if not reservation.acquired:
                return ProviderExecutionStep(
                    is_complete=False,
                    poll_count=max(1, reservation.poll_count),
                    retry_after=self._polling.delay_after(max(1, reservation.poll_count)),
                )

            try:
                result = await provider.poll(context.provider_attempt())
            except Exception:
                self._record_reconcile_pending(context)
                return ProviderExecutionStep(
                    is_complete=False,
                    poll_count=reservation.poll_count,
                    retry_after=self._polling.delay_after(reservation.poll_count),
                )
            action = self._apply_provider_result(context, result)
            if action == ProviderResultAction.RETRY:
                continue
            if action == ProviderResultAction.WAIT:
                return ProviderExecutionStep(
                    is_complete=False,
                    poll_count=reservation.poll_count,
                    retry_after=self._polling.delay_after(reservation.poll_count),
                )
            return ProviderExecutionStep(
                is_complete=True,
                poll_count=reservation.poll_count,
            )
        return ProviderExecutionStep(is_complete=True, poll_count=0)

    async def request_cancel(self, job_id: uuid.UUID) -> None:
        context = self._load_active_attempt(job_id)
        if context is None:
            return
        provider = self._provider_for(context.provider_code)
        try:
            result = await provider.cancel(context.provider_attempt())
        except Exception as exc:
            logger.warning(
                "provider.cancel_failed",
                job_id=str(job_id),
                attempt_id=str(context.attempt_id),
                provider=context.provider_code,
                error_type=type(exc).__name__,
            )
            return
        logger.info(
            "provider.cancel_result",
            job_id=str(job_id),
            attempt_id=str(context.attempt_id),
            provider=context.provider_code,
            accepted=result.accepted,
            provider_status=result.status.value,
        )
        if result.status == ProviderStatus.CANCELLED:
            self._apply_provider_result(
                context,
                PollResult(
                    status=ProviderStatus.CANCELLED,
                    provider_job_id=context.provider_job_id,
                ),
            )

    async def handle_webhook(
        self,
        provider_code: str,
        request: WebhookVerificationRequest,
    ) -> ProviderWebhookResult:
        provider = self._provider_for(provider_code)
        event = await provider.verify_webhook(request)
        event_id, lock_token = self._receive_provider_event(provider_code, request.body, event)
        if lock_token is None:
            return self._webhook_result(event_id)

        context = self._load_attempt_by_provider_job(provider_code, event.provider_job_id)
        if context is None:
            self._reset_provider_event(event_id, lock_token)
            return self._webhook_result(event_id)

        try:
            result = await self._result_for_event(provider, context, event)
        except Exception as exc:
            self._reset_provider_event(event_id, lock_token)
            logger.warning(
                "provider.webhook_reconcile_failed",
                event_id=event.event_id,
                provider=provider_code,
                attempt_id=str(context.attempt_id),
                error_type=type(exc).__name__,
            )
            return self._webhook_result(event_id)

        self._apply_provider_result(context, result)
        if event.status in {
            ProviderStatus.SUCCEEDED,
            ProviderStatus.FAILED,
        } and result.status in {
            ProviderStatus.PENDING,
            ProviderStatus.RUNNING,
            ProviderStatus.UNKNOWN,
        }:
            self._reset_provider_event(event_id, lock_token)
            return self._webhook_result(event_id)
        self._finish_provider_event(event_id, lock_token, context)
        logger.info(
            "provider.webhook_processed",
            event_id=event.event_id,
            provider=provider_code,
            attempt_id=str(context.attempt_id),
            provider_status=event.status.value,
        )
        return self._webhook_result(event_id)

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

    def _reserve_poll(self, context: AttemptContext) -> PollReservation:
        """Persist one poll slot before the external call so crashes consume its budget."""

        with self._session_factory() as db:
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if attempt is None:
                return PollReservation(0, acquired=False, exhausted=False)
            poll_count = (
                db.scalar(
                    select(func.count())
                    .select_from(JobEvent)
                    .where(
                        JobEvent.attempt_id == attempt.id,
                        JobEvent.event_type == "provider.poll_started",
                    )
                )
                or 0
            )
            created_at = attempt.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            deadline_at = created_at.astimezone(UTC) + self._polling.deadline
            if attempt.status not in {
                AttemptStatus.SUBMITTING,
                AttemptStatus.SUBMITTED,
                AttemptStatus.RUNNING,
            }:
                return PollReservation(
                    poll_count,
                    acquired=False,
                    exhausted=False,
                )
            if self._now() >= deadline_at or poll_count >= self._polling.maximum_polls:
                return PollReservation(
                    poll_count,
                    acquired=False,
                    exhausted=True,
                )

            next_poll_count = poll_count + 1
            db.add(
                JobEvent(
                    job_id=attempt.job_id,
                    attempt_id=attempt.id,
                    event_type="provider.poll_started",
                    from_status=attempt.status.value,
                    to_status=attempt.status.value,
                    dedup_key=f"attempt:{attempt.id}:poll:{next_poll_count}:v1",
                    payload_json={
                        "poll_count": next_poll_count,
                        "deadline_at": deadline_at.isoformat(),
                    },
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return PollReservation(
                    next_poll_count,
                    acquired=False,
                    exhausted=False,
                )
            return PollReservation(
                next_poll_count,
                acquired=True,
                exhausted=False,
            )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("provider execution clock must be timezone-aware")
        return now.astimezone(UTC)

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
            shot = db.get(Shot, job.shot_id) if job is not None else None
            if job is None or shot is None:
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

    async def _result_for_event(
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

    def _apply_provider_result(
        self, context: AttemptContext, result: PollResult
    ) -> ProviderResultAction:
        if result.provider_job_id and context.provider_job_id is None:
            self._record_submit_accepted(context, result.provider_job_id, reconciled=True)
            refreshed = self._load_attempt_by_provider_job(
                context.provider_code, result.provider_job_id
            )
            if refreshed is None:
                return ProviderResultAction.WAIT
            context = refreshed
        if result.status in {ProviderStatus.PENDING, ProviderStatus.RUNNING}:
            return ProviderResultAction.WAIT
        if result.status == ProviderStatus.UNKNOWN:
            self._record_reconcile_pending(context)
            return ProviderResultAction.WAIT
        return self._complete_provider_result(context, result)

    def _complete_provider_result(
        self, context: AttemptContext, result: PollResult
    ) -> ProviderResultAction:
        """Single idempotent completion path for polling, webhooks, and cancellation."""

        if result.status == ProviderStatus.SUCCEEDED:
            if result.output is not None:
                self._finish_output(context, result.output)
                return ProviderResultAction.COMPLETE
            result = PollResult(
                status=ProviderStatus.FAILED,
                provider_job_id=result.provider_job_id,
                failure=ProviderFailure(FailureCode.OUTPUT_MISSING, "Provider 未返回输出"),
            )
        if result.status == ProviderStatus.CANCELLED:
            self._finish_cancelled(context)
            return ProviderResultAction.COMPLETE
        failure = result.failure or ProviderFailure(
            FailureCode.INTERNAL_ERROR, "Provider 返回未分类错误"
        )
        return (
            ProviderResultAction.RETRY
            if self._fail_attempt(context, failure)
            else ProviderResultAction.COMPLETE
        )

    def _receive_provider_event(
        self,
        provider_code: str,
        body: bytes,
        event: ProviderEvent,
    ) -> tuple[uuid.UUID, str | None]:
        payload_hash = hashlib.sha256(body).hexdigest()
        now = datetime.now(UTC)
        lock_token = str(uuid.uuid4())
        with self._session_factory() as db:
            inbox = db.scalar(
                select(ProviderEventInbox).where(
                    ProviderEventInbox.provider_code == provider_code,
                    ProviderEventInbox.external_event_id == event.event_id,
                )
            )
            if inbox is None:
                inbox = ProviderEventInbox(
                    provider_code=provider_code,
                    external_event_id=event.event_id,
                    provider_job_id=event.provider_job_id,
                    provider_status=event.status.value,
                    payload_hash=payload_hash,
                    failure_code=event.failure.code.value if event.failure else None,
                    status=ProviderEventInboxStatus.RECEIVED,
                )
                db.add(inbox)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    inbox = db.scalar(
                        select(ProviderEventInbox).where(
                            ProviderEventInbox.provider_code == provider_code,
                            ProviderEventInbox.external_event_id == event.event_id,
                        )
                    )
                    if inbox is None:
                        raise
            if inbox.payload_hash != payload_hash:
                logger.warning(
                    "provider.webhook_event_conflict",
                    provider=provider_code,
                    event_id=event.event_id,
                )
                return inbox.id, None
            changed = db.execute(
                update(ProviderEventInbox)
                .where(
                    ProviderEventInbox.id == inbox.id,
                    (
                        (ProviderEventInbox.status == ProviderEventInboxStatus.RECEIVED)
                        | (
                            (ProviderEventInbox.status == ProviderEventInboxStatus.PROCESSING)
                            & (ProviderEventInbox.locked_at < now - PROVIDER_EVENT_LEASE)
                        )
                    ),
                )
                .values(
                    status=ProviderEventInboxStatus.PROCESSING,
                    locked_at=now,
                    lock_token=lock_token,
                )
                .execution_options(synchronize_session=False)
            )
            db.commit()
            return inbox.id, lock_token if changed.rowcount == 1 else None

    def _reset_provider_event(self, event_id: uuid.UUID, lock_token: str) -> None:
        with self._session_factory() as db:
            db.execute(
                update(ProviderEventInbox)
                .where(
                    ProviderEventInbox.id == event_id,
                    ProviderEventInbox.status == ProviderEventInboxStatus.PROCESSING,
                    ProviderEventInbox.lock_token == lock_token,
                )
                .values(
                    status=ProviderEventInboxStatus.RECEIVED,
                    locked_at=None,
                    lock_token=None,
                )
            )
            db.commit()

    def _finish_provider_event(
        self, event_id: uuid.UUID, lock_token: str, context: AttemptContext
    ) -> None:
        with self._session_factory() as db:
            db.execute(
                update(ProviderEventInbox)
                .where(
                    ProviderEventInbox.id == event_id,
                    ProviderEventInbox.status == ProviderEventInboxStatus.PROCESSING,
                    ProviderEventInbox.lock_token == lock_token,
                )
                .values(
                    status=ProviderEventInboxStatus.PROCESSED,
                    attempt_id=context.attempt_id,
                    job_id=context.job_id,
                    locked_at=None,
                    lock_token=None,
                    processed_at=datetime.now(UTC),
                )
            )
            db.commit()

    def _webhook_result(self, event_id: uuid.UUID) -> ProviderWebhookResult:
        with self._session_factory() as db:
            inbox = db.get(ProviderEventInbox, event_id)
            if inbox is None:
                raise RuntimeError("provider webhook inbox row disappeared")
            return ProviderWebhookResult(
                event_id=inbox.external_event_id,
                status=inbox.status,
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

    def _fail_attempt(
        self,
        context: AttemptContext,
        failure: ProviderFailure,
        *,
        target: AttemptStatus | None = None,
    ) -> bool:
        retryable = is_retryable_failure(failure.code)
        with self._session_factory() as db:
            job = db.get(GenerationJob, context.job_id)
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if job is None or attempt is None:
                return False
            if job.status == JobStatus.CANCEL_REQUESTED:
                if not transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.CANCELLED,
                    "attempt.cancelled",
                    f"attempt:{attempt.id}:terminal-cancelled:v1",
                    {"failure_code": FailureCode.USER_CANCELLED.value},
                ) or not transition_job(
                    db,
                    job,
                    JobStatus.CANCELLED,
                    "provider.cancelled",
                    f"job:{job.id}:terminal-cancelled:v1",
                ):
                    db.rollback()
                    return False
                attempt.failure_code = FailureCode.USER_CANCELLED.value
                attempt.finished_at = datetime.now(UTC)
                job.failure_code = FailureCode.USER_CANCELLED.value
                job.finished_at = datetime.now(UTC)
                finish_reservation(db, job, settle=False)
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
        if output.object_key is not None:
            self._finish_remote_output(context, output)
            return
        self._finish_embedded_output(context, output)

    def _finish_remote_output(
        self, context: AttemptContext, output: ProviderOutput
    ) -> None:
        try:
            published = self._artifact_receiver.receive_and_publish(
                job_id=context.job_id,
                attempt_id=context.attempt_id,
                output=output,
                policy=MediaPolicy(
                    expected_duration_ms=context.duration_ms,
                    expected_aspect_ratio=context.aspect_ratio,
                ),
            )
        except ArtifactReceiptError as exc:
            self._fail_attempt(context, exc.failure)
            return

        stored = published.stored
        facts = published.facts
        try:
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
                if not transition_job(
                    db,
                    job,
                    JobStatus.POSTPROCESSING,
                    "output.postprocessing",
                    f"attempt:{attempt.id}:postprocessing:v1",
                ) or not transition_job(
                    db,
                    job,
                    JobStatus.VALIDATING,
                    "output.validating",
                    f"attempt:{attempt.id}:validating:v1",
                ):
                    db.rollback()
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
                    validation_status=OutputValidationStatus.VALID,
                )
                db.add(generated)
                db.flush()
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
        except Exception:
            self._storage.delete(stored.key)
            raise

    def _finish_embedded_output(
        self, context: AttemptContext, output: ProviderOutput
    ) -> None:
        if output.content is None:
            self._fail_attempt(
                context,
                ProviderFailure(FailureCode.OUTPUT_MISSING, "Provider 未返回输出"),
            )
            return
        claim = self._storage.write_claim(
            "outputs",
            mime_type=output.media_type,
            max_bytes=max(1, len(output.content)),
        )
        stored = self._storage.put(claim, output.content, output.media_type)
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

    def _finish_cancelled(self, context: AttemptContext) -> None:
        with self._session_factory() as db:
            job = db.get(GenerationJob, context.job_id)
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if job is None or attempt is None:
                return
            if job.status not in {
                JobStatus.ROUTING,
                JobStatus.SUBMITTED,
                JobStatus.RUNNING,
                JobStatus.CANCEL_REQUESTED,
            }:
                return
            if not transition_attempt(
                db,
                attempt,
                AttemptStatus.CANCELLED,
                "attempt.cancelled",
                f"attempt:{attempt.id}:terminal-cancelled:v1",
                {"failure_code": FailureCode.USER_CANCELLED.value},
            ) or not transition_job(
                db,
                job,
                JobStatus.CANCELLED,
                "provider.cancelled",
                f"job:{job.id}:terminal-cancelled:v1",
            ):
                db.rollback()
                return
            attempt.failure_code = FailureCode.USER_CANCELLED.value
            attempt.finished_at = datetime.now(UTC)
            job.failure_code = FailureCode.USER_CANCELLED.value
            job.finished_at = datetime.now(UTC)
            finish_reservation(db, job, settle=False)
            db.commit()
