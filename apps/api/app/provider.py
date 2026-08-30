import asyncio
import hashlib
import hmac
import json
import re
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
    PROVIDER_AUTHENTICATION = "PROVIDER_AUTHENTICATION"
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
    ESTIMATE = "ESTIMATE"
    SIMULATED = "SIMULATED"


@dataclass(frozen=True, slots=True)
class SubmitRequest:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    idempotency_key: str
    prompt: str
    negative_prompt: str | None
    duration_ms: int
    aspect_ratio: str
    resolution: str
    workflow_id: str
    input_claim: str | None
    output_claim: str
    callback_claim: str
    mode: str = "success"


SIGNED_CLAIM_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


def validate_fixed_worker_request(
    request: SubmitRequest,
    *,
    workflow_id: str,
) -> None:
    """Reject anything outside the fixed claim-only worker contract."""

    if request.idempotency_key != f"attempt:{request.attempt_id}:submit:v1":
        raise ValueError("idempotency key is not bound to attempt_id")
    if request.workflow_id != workflow_id:
        raise ValueError("workflow_id is not the configured immutable workflow")
    if request.duration_ms != 5_000:
        raise ValueError("only the configured five-second duration is allowed")
    if request.aspect_ratio not in {"16:9", "9:16"}:
        raise ValueError("aspect_ratio is not allowed")
    if request.resolution != "720p":
        raise ValueError("resolution is not allowed")
    claims = (request.input_claim, request.output_claim, request.callback_claim)
    if any(
        claim is None
        or "://" in claim
        or SIGNED_CLAIM_PATTERN.fullmatch(claim) is None
        for claim in claims
    ):
        raise ValueError("worker request requires opaque system-signed claims")


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
    content: bytes | None = None
    media_type: str = "video/mp4"
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    object_key: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    metrics: "ProviderMetrics | None" = None
    versions: "ProviderVersions | None" = None


@dataclass(frozen=True, slots=True)
class ProviderMetrics:
    gpu_type: str
    queue_ms: int
    cold_start_ms: int
    runtime_ms: int
    billable_ms: int
    cost_minor: int | None = None
    currency: str | None = None
    cost_source: CostSource | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.gpu_type, str)
            or not self.gpu_type
            or len(self.gpu_type) > 100
        ):
            raise ValueError("gpu_type must be between 1 and 100 characters")
        timings = (self.queue_ms, self.cold_start_ms, self.runtime_ms, self.billable_ms)
        if any(type(value) is not int for value in timings) or min(timings) < 0:
            raise ValueError("provider timings must be non-negative integer milliseconds")
        cost_fields = (self.cost_minor, self.currency, self.cost_source)
        if any(value is not None for value in cost_fields):
            if (
                type(self.cost_minor) is not int
                or self.cost_minor < 0
                or not isinstance(self.currency, str)
                or re.fullmatch(r"[A-Z]{3}", self.currency) is None
                or not isinstance(self.cost_source, CostSource)
            ):
                raise ValueError("provider cost metadata must be complete and valid")


@dataclass(frozen=True, slots=True)
class ProviderVersions:
    image_digest: str
    worker_version: str
    worker_commit: str
    comfyui_version: str
    comfyui_commit: str
    workflow_version: str
    workflow_hash: str
    model_hashes: Mapping[str, str]

    @property
    def workflow_sha256(self) -> str:
        return self.workflow_hash

    @property
    def model_sha256(self) -> Mapping[str, str]:
        return self.model_hashes

    def __post_init__(self) -> None:
        if not isinstance(self.image_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.image_digest
        ):
            raise ValueError("image_digest must be a lowercase sha256 digest")
        if (
            not isinstance(self.worker_version, str)
            or not self.worker_version
            or len(self.worker_version) > 100
        ):
            raise ValueError("worker_version must be between 1 and 100 characters")
        if (
            not isinstance(self.comfyui_version, str)
            or not self.comfyui_version
            or len(self.comfyui_version) > 100
        ):
            raise ValueError("comfyui_version must be between 1 and 100 characters")
        if (
            not isinstance(self.workflow_version, str)
            or not self.workflow_version
            or len(self.workflow_version) > 100
        ):
            raise ValueError("workflow_version must be between 1 and 100 characters")
        for field_name, value in (
            ("worker_commit", self.worker_commit),
            ("comfyui_commit", self.comfyui_commit),
            ("workflow_hash", self.workflow_hash),
        ):
            if not isinstance(value, str) or not re.fullmatch(
                r"[0-9a-f]{40}|[0-9a-f]{64}", value
            ):
                raise ValueError(f"{field_name} must be a lowercase commit or sha256 hash")
        if not isinstance(self.model_hashes, Mapping) or not self.model_hashes or any(
            not isinstance(name, str)
            or not isinstance(digest, str)
            or not name
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for name, digest in self.model_hashes.items()
        ):
            raise ValueError("model_hashes must contain named lowercase sha256 hashes")


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
    metrics: ProviderMetrics | None = None
    versions: ProviderVersions | None = None
    cost: "CostResult | None" = None


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

    def __post_init__(self) -> None:
        if type(self.amount_minor) is not int or self.amount_minor < 0:
            raise ValueError("cost amount must be non-negative integer minor units")
        if not isinstance(self.currency, str) or not re.fullmatch(
            r"[A-Z]{3}", self.currency
        ):
            raise ValueError("cost currency must be an ISO 4217 alpha-3 code")
        if not isinstance(self.source, CostSource):
            raise ValueError("cost source must use the closed CostSource enum")


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
    def _metrics() -> ProviderMetrics:
        return ProviderMetrics(
            gpu_type="MOCK GPU (SIMULATED)",
            queue_ms=0,
            cold_start_ms=0,
            runtime_ms=0,
            billable_ms=0,
        )

    @staticmethod
    def _versions() -> ProviderVersions:
        return ProviderVersions(
            image_digest="sha256:" + hashlib.sha256(b"mock-worker-image").hexdigest(),
            worker_version="mock-worker/1.0.0",
            worker_commit=hashlib.sha1(b"mock-worker").hexdigest(),
            comfyui_version="mock-comfyui/1.0.0",
            comfyui_commit=hashlib.sha1(b"mock-comfyui").hexdigest(),
            workflow_version="mock:v1",
            workflow_hash=hashlib.sha256(b"mock-workflow-v1").hexdigest(),
            model_hashes={"mock-model": hashlib.sha256(b"mock-model").hexdigest()},
        )

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
                metrics=self._metrics(),
                versions=self._versions(),
            )
        if attempt.mode == "failure":
            return PollResult(
                status=ProviderStatus.FAILED,
                provider_job_id=provider_job_id,
                failure=ProviderFailure(
                    FailureCode.WORKFLOW_FAILED, "Mock Provider 工作流失败"
                ),
                metrics=self._metrics(),
                versions=self._versions(),
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
            metrics=self._metrics(),
            versions=self._versions(),
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
        return CostResult(amount_minor=0, currency="USD", source=CostSource.SIMULATED)


class WebhookVerificationError(ValueError):
    pass


class ProviderSubmissionError(RuntimeError):
    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure
