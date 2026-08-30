import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.orm import Session, sessionmaker

from app.artifacts import RemoteArtifactReceiver
from app.config import get_settings
from app.db import SessionLocal
from app.media import MediaValidator, create_media_validator
from app.models import AttemptStatus
from app.provider import (
    FailureCode,
    PollResult,
    ProviderFailure,
    ProviderStatus,
    ProviderSubmissionError,
    SubmitDisposition,
    VideoProvider,
    WebhookVerificationRequest,
)
from app.provider_callbacks import ProviderCallbackService, ProviderWebhookResult
from app.provider_completion import (
    AttemptBudget,
    ProviderCompletionService,
    ProviderResultAction,
)
from app.provider_execution_context import AttemptContext, AttemptContextService
from app.provider_polling import (
    PollReservation,
    ProviderPollingPolicy,
    ProviderPollingService,
)
from app.provider_registry import ProviderRegistry
from app.provider_settlement import ProviderSettlementService
from app.provider_submission import ProviderSubmissionService
from app.routing import (
    MAX_PROVIDER_OUTPUT_BYTES,
    CallbackClaimIssuer,
    RouteRegistry,
    get_callback_claim_issuer,
    get_provider_registry,
    get_route_registry,
)
from app.storage import ObjectStorage

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class ProviderExecutionStep:
    is_complete: bool
    poll_count: int
    retry_after: timedelta | None = None


class GenerationExecutionService(
    AttemptContextService,
    ProviderSubmissionService,
    ProviderPollingService,
    ProviderCallbackService,
    ProviderSettlementService,
    ProviderCompletionService,
):
    """Thin coordinator for submit, poll, callback, completion, and cancellation."""

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
                except ProviderSubmissionError as exc:
                    action = self._fail_attempt(context, exc.failure)
                    if action:
                        continue
                    return ProviderExecutionStep(is_complete=True, poll_count=0)
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
                poll_count = max(1, reservation.poll_count)
                return ProviderExecutionStep(
                    is_complete=False,
                    poll_count=poll_count,
                    retry_after=self._polling.delay_after(poll_count),
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
            result = await self._with_terminal_cost(provider, context, result)
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
            terminal = await self._with_terminal_cost(
                provider,
                context,
                PollResult(
                    status=ProviderStatus.CANCELLED,
                    provider_job_id=context.provider_job_id,
                ),
            )
            self._apply_provider_result(context, terminal)

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
            result = await self._with_terminal_cost(provider, context, result)
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


__all__ = [
    "AttemptBudget",
    "AttemptContext",
    "GenerationExecutionService",
    "PollReservation",
    "ProviderExecutionStep",
    "ProviderPollingPolicy",
    "ProviderResultAction",
    "ProviderWebhookResult",
]
