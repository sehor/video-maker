import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from typing import Protocol


def mock_video_fixture() -> bytes:
    return files("app").joinpath("fixtures", "mock-success.mp4").read_bytes()


class FailureCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    POLICY_REJECTED = "POLICY_REJECTED"
    USER_CANCELLED = "USER_CANCELLED"
    UNSUPPORTED_PARAMETER = "UNSUPPORTED_PARAMETER"
    LICENSE_BLOCKED = "LICENSE_BLOCKED"
    PROVIDER_TERMS_BLOCKED = "PROVIDER_TERMS_BLOCKED"
    NO_ROUTE = "NO_ROUTE"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    PROVIDER_5XX = "PROVIDER_5XX"
    PROVIDER_CAPACITY = "PROVIDER_CAPACITY"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
    WORKER_INTERRUPTED = "WORKER_INTERRUPTED"
    MODEL_LOAD_FAILED = "MODEL_LOAD_FAILED"
    OUT_OF_MEMORY = "OUT_OF_MEMORY"
    WORKFLOW_FAILED = "WORKFLOW_FAILED"
    ASSET_DOWNLOAD_FAILED = "ASSET_DOWNLOAD_FAILED"
    OUTPUT_UPLOAD_FAILED = "OUTPUT_UPLOAD_FAILED"
    OUTPUT_MISSING = "OUTPUT_MISSING"
    OUTPUT_CORRUPTED = "OUTPUT_CORRUPTED"
    OUTPUT_INVALID_MEDIA = "OUTPUT_INVALID_MEDIA"
    LEDGER_ERROR = "LEDGER_ERROR"
    STATE_CONFLICT = "STATE_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


RETRYABLE_FAILURE_CODES = frozenset(
    {
        FailureCode.NETWORK_TIMEOUT,
        FailureCode.PROVIDER_5XX,
        FailureCode.PROVIDER_CAPACITY,
        FailureCode.QUEUE_TIMEOUT,
        FailureCode.WORKER_INTERRUPTED,
    }
)


def is_retryable_failure(code: FailureCode) -> bool:
    return code in RETRYABLE_FAILURE_CODES


class SubmitDisposition(StrEnum):
    ACCEPTED = "ACCEPTED"
    UNKNOWN = "UNKNOWN"


class ProviderStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class CostSource(StrEnum):
    ACTUAL = "ACTUAL"
    BILLING_IMPORT = "BILLING_IMPORT"
    ESTIMATED = "ESTIMATED"


@dataclass(frozen=True, slots=True)
class SubmitRequest:
    attempt_id: uuid.UUID
    idempotency_key: str
    prompt: str
    negative_prompt: str | None
    duration_ms: int
    aspect_ratio: str
    resolution: str
    workflow_version: str
    mode: str = "success"


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    attempt_id: uuid.UUID
    idempotency_key: str
    provider_job_id: str | None
    mode: str = "success"


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    code: FailureCode
    message: str


@dataclass(frozen=True, slots=True)
class ProviderOutput:
    content: bytes
    media_type: str
    duration_ms: int
    width: int
    height: int
    fps: float
    codec: str


@dataclass(frozen=True, slots=True)
class SubmitResult:
    disposition: SubmitDisposition
    provider_job_id: str | None = None


@dataclass(frozen=True, slots=True)
class PollResult:
    status: ProviderStatus
    provider_job_id: str | None = None
    output: ProviderOutput | None = None
    failure: ProviderFailure | None = None


@dataclass(frozen=True, slots=True)
class CancelResult:
    accepted: bool
    status: ProviderStatus


@dataclass(frozen=True, slots=True)
class WebhookVerificationRequest:
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    event_id: str
    provider_job_id: str
    status: ProviderStatus
    failure: ProviderFailure | None = None


@dataclass(frozen=True, slots=True)
class CostResult:
    amount_minor: int
    currency: str
    source: CostSource


class VideoProvider(Protocol):
    async def submit(self, request: SubmitRequest) -> SubmitResult: ...

    async def poll(self, attempt: ProviderAttempt) -> PollResult: ...

    async def cancel(self, attempt: ProviderAttempt) -> CancelResult: ...

    async def verify_webhook(
        self, request: WebhookVerificationRequest
    ) -> ProviderEvent: ...

    async def read_cost(self, attempt: ProviderAttempt) -> CostResult | None: ...


class MockVideoProvider:
    """A deterministic Provider adapter with no database or object-storage knowledge."""

    def __init__(self, webhook_secret: str | None = None) -> None:
        self.submit_calls: list[str] = []
        self._webhook_secret = webhook_secret

    @staticmethod
    def _provider_job_id(idempotency_key: str) -> str:
        value = uuid.uuid5(uuid.NAMESPACE_URL, f"video-maker:{idempotency_key}")
        return f"mock-{value.hex}"

    async def submit(self, request: SubmitRequest) -> SubmitResult:
        self.submit_calls.append(request.idempotency_key)
        if request.mode == "submit_unknown":
            return SubmitResult(disposition=SubmitDisposition.UNKNOWN)
        return SubmitResult(
            disposition=SubmitDisposition.ACCEPTED,
            provider_job_id=self._provider_job_id(request.idempotency_key),
        )

    async def poll(self, attempt: ProviderAttempt) -> PollResult:
        provider_job_id = attempt.provider_job_id or self._provider_job_id(
            attempt.idempotency_key
        )
        if attempt.mode == "delayed":
            await asyncio.sleep(1)
        if attempt.mode == "timeout":
            return PollResult(
                status=ProviderStatus.FAILED,
                provider_job_id=provider_job_id,
                failure=ProviderFailure(
                    FailureCode.NETWORK_TIMEOUT, "Mock Provider 网络超时"
                ),
            )
        if attempt.mode == "failure":
            return PollResult(
                status=ProviderStatus.FAILED,
                provider_job_id=provider_job_id,
                failure=ProviderFailure(
                    FailureCode.WORKFLOW_FAILED, "Mock Provider 工作流失败"
                ),
            )
        content = (
            b"not-an-mp4"
            if attempt.mode == "corrupt"
            else mock_video_fixture()
        )
        return PollResult(
            status=ProviderStatus.SUCCEEDED,
            provider_job_id=provider_job_id,
            output=ProviderOutput(
                content=content,
                media_type="video/mp4",
                duration_ms=2_000,
                width=1280,
                height=720,
                fps=25,
                codec="mpeg4",
            ),
        )

    async def cancel(self, attempt: ProviderAttempt) -> CancelResult:
        return CancelResult(accepted=True, status=ProviderStatus.CANCELLED)

    async def verify_webhook(
        self, request: WebhookVerificationRequest
    ) -> ProviderEvent:
        if self._webhook_secret is None:
            raise WebhookVerificationError("webhook secret is not configured")
        supplied = request.headers.get("x-provider-signature", "")
        expected = hmac.new(
            self._webhook_secret.encode(), request.body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise WebhookVerificationError("invalid webhook signature")
        try:
            payload = json.loads(request.body)
            event_id = payload["event_id"]
            provider_job_id = payload["provider_job_id"]
            status = ProviderStatus(payload["status"])
            if not isinstance(event_id, str) or not event_id or len(event_id) > 255:
                raise ValueError("invalid event id")
            if (
                not isinstance(provider_job_id, str)
                or not provider_job_id
                or len(provider_job_id) > 255
            ):
                raise ValueError("invalid provider job id")
            failure = None
            if status == ProviderStatus.FAILED:
                failure_code = FailureCode(payload["failure_code"])
                failure = ProviderFailure(failure_code, "Provider reported failure")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WebhookVerificationError("invalid webhook payload") from exc
        return ProviderEvent(
            event_id=event_id,
            provider_job_id=provider_job_id,
            status=status,
            failure=failure,
        )

    async def read_cost(self, attempt: ProviderAttempt) -> CostResult | None:
        return CostResult(amount_minor=0, currency="USD", source=CostSource.ESTIMATED)


class WebhookVerificationError(ValueError):
    pass
