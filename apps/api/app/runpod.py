from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.provider import (
    CancelResult,
    CostResult,
    CostSource,
    FailureCode,
    PollResult,
    ProviderAttempt,
    ProviderEvent,
    ProviderFailure,
    ProviderMetrics,
    ProviderOutput,
    ProviderStatus,
    ProviderSubmissionError,
    ProviderVersions,
    SubmitDisposition,
    SubmitRequest,
    SubmitResult,
    WebhookVerificationError,
    WebhookVerificationRequest,
)

RUNPOD_API_ORIGIN = "https://api.runpod.ai"
RUNPOD_ENDPOINT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,100}$")
RUNPOD_JOB_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,255}$")
CLAIM_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
MAX_RESPONSE_BYTES = 1_048_576


class RunPodAdapterError(ValueError):
    """A local configuration or fixed-contract violation."""


@dataclass(frozen=True, slots=True)
class RunPodAdapterConfig:
    endpoint_id: str
    api_key: str = field(repr=False)
    image_digest: str
    workflow_id: str
    workflow_sha256: str
    model_sha256: dict[str, str]
    worker_version: str = "5.8.7"
    worker_commit: str = "a1981e99b1f5a7201f387653420ad1f275b97d0a"
    comfyui_version: str = "0.29.0"
    comfyui_commit: str = "a8c44f9b2a0678ac4082e3529a3f43db7472acfe"
    comfy_cli_version: str = "1.18.0"
    execution_timeout_ms: int = 900_000
    ttl_ms: int = 3_600_000
    request_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not RUNPOD_ENDPOINT_PATTERN.fullmatch(self.endpoint_id):
            raise RunPodAdapterError("invalid RunPod endpoint id")
        if not self.api_key or self.api_key.strip() != self.api_key:
            raise RunPodAdapterError("RunPod API key is not configured")
        if not DIGEST_PATTERN.fullmatch(self.image_digest):
            raise RunPodAdapterError("worker image must be locked by sha256 digest")
        if self.workflow_id != "fast_wan_i2v_720_v1":
            raise RunPodAdapterError("unsupported workflow id")
        if not SHA256_PATTERN.fullmatch(self.workflow_sha256):
            raise RunPodAdapterError("workflow must be locked by sha256")
        if not self.model_sha256 or any(
            not name or not SHA256_PATTERN.fullmatch(digest)
            for name, digest in self.model_sha256.items()
        ):
            raise RunPodAdapterError("every model must be locked by sha256")
        if (
            self.worker_version != "5.8.7"
            or self.worker_commit != "a1981e99b1f5a7201f387653420ad1f275b97d0a"
            or self.comfyui_version != "0.29.0"
            or self.comfyui_commit != "a8c44f9b2a0678ac4082e3529a3f43db7472acfe"
            or self.comfy_cli_version != "1.18.0"
        ):
            raise RunPodAdapterError("worker runtime versions do not match the locked release")
        if not 5_000 <= self.execution_timeout_ms <= 604_800_000:
            raise RunPodAdapterError("execution timeout is outside RunPod limits")
        if not 10_000 <= self.ttl_ms <= 604_800_000:
            raise RunPodAdapterError("job ttl is outside RunPod limits")
        if self.ttl_ms <= self.execution_timeout_ms:
            raise RunPodAdapterError("job ttl must exceed execution timeout")
        if not 0 < self.request_timeout_seconds <= 60:
            raise RunPodAdapterError("request timeout must be between 0 and 60 seconds")
        object.__setattr__(
            self,
            "model_sha256",
            MappingProxyType(dict(sorted(self.model_sha256.items()))),
        )


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _WorkerArtifact(_StrictModel):
    claim: str = Field(pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
    object_key: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_./-]+$")
    media_type: Literal["video/mp4"]
    duration_ms: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0, le=240, allow_inf_nan=False)
    codec: str = Field(min_length=1, max_length=32)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def reject_unsafe_object_key(self) -> _WorkerArtifact:
        parts = self.object_key.split("/")
        if self.object_key.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("unsafe output object key")
        return self


class _WorkerMetrics(_StrictModel):
    gpu: str = Field(min_length=1, max_length=200)
    queue_ms: int = Field(ge=0)
    cold_start_ms: int = Field(ge=0)
    runtime_ms: int = Field(ge=0)
    billable_ms: int = Field(ge=0)
    cost_minor: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    cost_source: Literal["ACTUAL", "ESTIMATE"]


class _WorkerVersions(_StrictModel):
    worker_comfyui: str
    worker_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    comfyui_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    comfy_cli: str
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    workflow_id: str
    workflow_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_sha256: dict[str, str]

    @model_validator(mode="after")
    def validate_model_hashes(self) -> _WorkerVersions:
        if not 1 <= len(self.model_sha256) <= 32 or any(
            not 1 <= len(name) <= 255 or not SHA256_PATTERN.fullmatch(digest)
            for name, digest in self.model_sha256.items()
        ):
            raise ValueError("invalid model sha256 map")
        return self


class _WorkerError(_StrictModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1000)


class _WorkerResponse(_StrictModel):
    attempt_id: uuid.UUID
    status: Literal["SUCCEEDED", "FAILED"]
    output: _WorkerArtifact | None
    metrics: _WorkerMetrics
    versions: _WorkerVersions
    error: _WorkerError | None

    @model_validator(mode="after")
    def validate_discriminated_fields(self) -> _WorkerResponse:
        if self.status == "SUCCEEDED" and (self.output is None or self.error is not None):
            raise ValueError("successful worker response must contain only output")
        if self.status == "FAILED" and (self.output is not None or self.error is None):
            raise ValueError("failed worker response must contain only error")
        return self


class _RunPodEnvelope(_StrictModel):
    id: str = Field(min_length=3, max_length=255)
    status: Literal[
        "IN_QUEUE",
        "IN_PROGRESS",
        "COMPLETED",
        "FAILED",
        "TIMED_OUT",
        "CANCELLED",
    ]
    output: dict[str, Any] | None = None
    error: str | dict[str, Any] | None = None
    delayTime: int | None = Field(default=None, ge=0)
    executionTime: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_job_id(self) -> _RunPodEnvelope:
        if not RUNPOD_JOB_PATTERN.fullmatch(self.id):
            raise ValueError("invalid RunPod job id")
        return self


WORKER_FAILURE_CODES = frozenset(
    {
        FailureCode.INVALID_INPUT,
        FailureCode.POLICY_REJECTED,
        FailureCode.WORKER_INTERRUPTED,
        FailureCode.MODEL_LOAD_FAILED,
        FailureCode.OUT_OF_MEMORY,
        FailureCode.WORKFLOW_FAILED,
        FailureCode.ASSET_DOWNLOAD_FAILED,
        FailureCode.OUTPUT_UPLOAD_FAILED,
        FailureCode.OUTPUT_MISSING,
        FailureCode.OUTPUT_CORRUPTED,
        FailureCode.INTERNAL_ERROR,
    }
)


class RunPodVideoProvider:
    """Thin, fail-closed adapter for one immutable RunPod Serverless route."""

    def __init__(
        self,
        config: RunPodAdapterConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(
            base_url=RUNPOD_API_ORIGIN,
            headers={
                "authorization": f"Bearer {config.api_key}",
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=config.request_timeout_seconds,
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def _endpoint_path(self) -> str:
        return f"/v2/{self._config.endpoint_id}"

    async def submit(self, request: SubmitRequest) -> SubmitResult:
        try:
            worker_input = self._worker_input(request)
            response = await self._client.post(
                f"{self._endpoint_path}/run",
                json={
                    "input": worker_input,
                    "policy": {
                        "executionTimeout": self._config.execution_timeout_ms,
                        "ttl": self._config.ttl_ms,
                    },
                },
            )
            response.raise_for_status()
            envelope = _RunPodEnvelope.model_validate(self._response_json(response))
            if envelope.status not in {"IN_QUEUE", "IN_PROGRESS"}:
                raise RunPodAdapterError("RunPod returned an invalid submit status")
            return SubmitResult(
                disposition=SubmitDisposition.ACCEPTED,
                provider_job_id=envelope.id,
            )
        except RunPodAdapterError as exc:
            raise ProviderSubmissionError(
                ProviderFailure(FailureCode.INVALID_INPUT, str(exc))
            ) from exc
        except httpx.HTTPStatusError as exc:
            code = self._submit_http_failure_code(exc.response.status_code)
            if code is not None:
                raise ProviderSubmissionError(
                    ProviderFailure(code, "RunPod rejected the submit request")
                ) from exc
            return SubmitResult(disposition=SubmitDisposition.UNKNOWN)
        except (httpx.HTTPError, ValidationError, ValueError, TypeError):
            # A disconnected /run request may already have been accepted. The execution
            # layer must keep the Attempt in SUBMITTING and reconcile; never resubmit.
            return SubmitResult(disposition=SubmitDisposition.UNKNOWN)

    async def poll(self, attempt: ProviderAttempt) -> PollResult:
        if attempt.provider_job_id is None:
            return PollResult(
                status=ProviderStatus.UNKNOWN,
                failure=ProviderFailure(
                    FailureCode.NETWORK_TIMEOUT,
                    "RunPod submit outcome is unknown and has no provider job id",
                ),
            )
        try:
            envelope = await self._status(attempt.provider_job_id)
        except httpx.TimeoutException:
            return self._failed(
                attempt.provider_job_id,
                FailureCode.NETWORK_TIMEOUT,
                "RunPod status request timed out",
            )
        except httpx.HTTPStatusError as exc:
            return self._http_failure(attempt.provider_job_id, exc.response.status_code)
        except (httpx.HTTPError, ValidationError, ValueError, TypeError):
            return self._failed(
                attempt.provider_job_id,
                FailureCode.INTERNAL_ERROR,
                "RunPod returned an invalid status response",
            )

        if envelope.status == "IN_QUEUE":
            return PollResult(ProviderStatus.PENDING, provider_job_id=envelope.id)
        if envelope.status == "IN_PROGRESS":
            return PollResult(ProviderStatus.RUNNING, provider_job_id=envelope.id)
        if envelope.status == "CANCELLED":
            return PollResult(ProviderStatus.CANCELLED, provider_job_id=envelope.id)
        if envelope.status == "TIMED_OUT":
            return self._failed(
                envelope.id,
                FailureCode.QUEUE_TIMEOUT,
                "RunPod job timed out",
            )
        if envelope.status == "FAILED":
            return self._worker_failure(envelope, attempt.attempt_id)
        return self._worker_success(envelope, attempt.attempt_id)

    async def cancel(self, attempt: ProviderAttempt) -> CancelResult:
        if attempt.provider_job_id is None:
            return CancelResult(accepted=False, status=ProviderStatus.UNKNOWN)
        try:
            response = await self._client.post(
                f"{self._endpoint_path}/cancel/{attempt.provider_job_id}"
            )
            response.raise_for_status()
            envelope = _RunPodEnvelope.model_validate(self._response_json(response))
            if envelope.id != attempt.provider_job_id:
                raise RunPodAdapterError("RunPod cancel job id mismatch")
        except (httpx.HTTPError, ValidationError, ValueError, TypeError):
            return CancelResult(accepted=False, status=ProviderStatus.UNKNOWN)
        status = self._provider_status(envelope.status)
        return CancelResult(
            accepted=status == ProviderStatus.CANCELLED,
            status=status,
        )

    async def verify_webhook(self, request: WebhookVerificationRequest) -> ProviderEvent:
        del request
        # RunPod's documented callback has no cryptographic signature. Accepting it
        # directly would allow spoofed completions, so this route uses polling only.
        raise WebhookVerificationError("unsigned RunPod webhooks are disabled")

    async def read_cost(self, attempt: ProviderAttempt) -> CostResult | None:
        result = await self.poll(attempt)
        metrics = result.output.metrics if result.output is not None else None
        if metrics is None:
            return None
        return CostResult(
            amount_minor=metrics.cost_minor,
            currency=metrics.currency,
            source=metrics.cost_source,
        )

    def _worker_input(self, request: SubmitRequest) -> dict[str, Any]:
        if request.job_id is None:
            raise RunPodAdapterError("job id is required")
        if request.idempotency_key != f"attempt:{request.attempt_id}:submit:v1":
            raise RunPodAdapterError("idempotency key is not bound to attempt id")
        if request.workflow_id != self._config.workflow_id:
            raise RunPodAdapterError("workflow id does not match the locked release")
        if request.duration_ms != 5_000 or request.resolution != "720p":
            raise RunPodAdapterError("only the fixed 5-second 720p route is allowed")
        if request.aspect_ratio not in {"16:9", "9:16"}:
            raise RunPodAdapterError("unsupported aspect ratio")
        if not 1 <= len(request.prompt) <= 2_000:
            raise RunPodAdapterError("prompt length is outside the worker contract")
        if request.negative_prompt is not None:
            raise RunPodAdapterError("negative prompt is not enabled for this release")
        claims = {
            "input_claim": request.input_claim,
            "output_claim": request.output_claim,
            "callback_claim": request.callback_claim,
        }
        if any(
            not isinstance(value, str)
            or not 3 <= len(value) <= 4_096
            or not CLAIM_PATTERN.fullmatch(value)
            for value in claims.values()
        ):
            raise RunPodAdapterError("all storage access must use signed claims")
        return {
            "job_id": str(request.job_id),
            "attempt_id": str(request.attempt_id),
            "workflow_id": self._config.workflow_id,
            "prompt": request.prompt,
            "duration_ms": request.duration_ms,
            "aspect_ratio": request.aspect_ratio,
            "resolution": "720p",
            **claims,
        }

    async def _status(self, provider_job_id: str) -> _RunPodEnvelope:
        if not RUNPOD_JOB_PATTERN.fullmatch(provider_job_id):
            raise RunPodAdapterError("invalid RunPod job id")
        response = await self._client.get(f"{self._endpoint_path}/status/{provider_job_id}")
        response.raise_for_status()
        envelope = _RunPodEnvelope.model_validate(self._response_json(response))
        if envelope.id != provider_job_id:
            raise RunPodAdapterError("RunPod status job id mismatch")
        return envelope

    def _worker_success(self, envelope: _RunPodEnvelope, attempt_id: uuid.UUID) -> PollResult:
        try:
            worker = self._validated_worker_response(envelope, attempt_id)
        except (ValidationError, ValueError, RunPodAdapterError):
            return self._failed(
                envelope.id,
                FailureCode.POLICY_REJECTED,
                "RunPod worker output violated the locked release contract",
            )
        if worker.status == "FAILED":
            return self._parsed_worker_failure(envelope.id, worker)
        if worker.output is None:
            return self._failed(
                envelope.id,
                FailureCode.WORKFLOW_FAILED,
                "RunPod completed without a successful worker output",
            )
        artifact = worker.output
        metrics, version_snapshot, cost_snapshot = self._snapshots(worker)
        return PollResult(
            ProviderStatus.SUCCEEDED,
            provider_job_id=envelope.id,
            output=ProviderOutput(
                content=None,
                media_type=artifact.media_type,
                duration_ms=artifact.duration_ms,
                width=artifact.width,
                height=artifact.height,
                fps=artifact.fps,
                codec=artifact.codec,
                object_key=artifact.object_key,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
                metrics=metrics,
                versions=version_snapshot,
            ),
            metrics=metrics,
            versions=version_snapshot,
            cost=cost_snapshot,
        )

    def _worker_failure(self, envelope: _RunPodEnvelope, attempt_id: uuid.UUID) -> PollResult:
        try:
            worker = self._validated_worker_response(envelope, attempt_id)
            return self._parsed_worker_failure(envelope.id, worker)
        except (ValidationError, ValueError, RunPodAdapterError):
            return self._failed(
                envelope.id,
                FailureCode.WORKFLOW_FAILED,
                "RunPod worker failed without a valid structured error",
            )

    def _validated_worker_response(
        self, envelope: _RunPodEnvelope, attempt_id: uuid.UUID
    ) -> _WorkerResponse:
        worker = _WorkerResponse.model_validate(envelope.output)
        if worker.attempt_id != attempt_id:
            raise RunPodAdapterError("worker attempt id mismatch")
        versions = worker.versions
        expected = self._config
        if (
            versions.worker_comfyui != expected.worker_version
            or versions.worker_commit != expected.worker_commit
            or versions.comfyui_commit != expected.comfyui_commit
            or versions.comfy_cli != expected.comfy_cli_version
            or versions.image_digest != expected.image_digest
            or versions.workflow_id != expected.workflow_id
            or versions.workflow_sha256 != expected.workflow_sha256
            or dict(versions.model_sha256) != dict(expected.model_sha256)
        ):
            raise RunPodAdapterError("worker release provenance mismatch")
        return worker

    def _parsed_worker_failure(self, provider_job_id: str, worker: _WorkerResponse) -> PollResult:
        if worker.status != "FAILED" or worker.error is None:
            raise RunPodAdapterError("missing worker error")
        try:
            code = FailureCode(worker.error.code)
        except ValueError:
            return self._failed(
                provider_job_id,
                FailureCode.WORKFLOW_FAILED,
                "RunPod worker returned an invalid structured error",
            )
        if code not in WORKER_FAILURE_CODES:
            return self._failed(
                provider_job_id,
                FailureCode.WORKFLOW_FAILED,
                "RunPod worker returned a forbidden error code",
            )
        metrics, versions, cost = self._snapshots(worker)
        return PollResult(
            ProviderStatus.FAILED,
            provider_job_id=provider_job_id,
            failure=ProviderFailure(code, worker.error.message),
            metrics=metrics,
            versions=versions,
            cost=cost,
        )

    def _snapshots(
        self, worker: _WorkerResponse
    ) -> tuple[ProviderMetrics, ProviderVersions, CostResult]:
        metrics = self._provider_metrics(worker.metrics)
        raw_versions = worker.versions
        versions = ProviderVersions(
            image_digest=raw_versions.image_digest,
            worker_version=raw_versions.worker_comfyui,
            worker_commit=raw_versions.worker_commit,
            comfyui_version=self._config.comfyui_version,
            comfyui_commit=raw_versions.comfyui_commit,
            workflow_version=raw_versions.workflow_id,
            workflow_hash=raw_versions.workflow_sha256,
            model_hashes=MappingProxyType(dict(raw_versions.model_sha256)),
        )
        cost = CostResult(
            amount_minor=metrics.cost_minor or 0,
            currency=metrics.currency or "USD",
            source=metrics.cost_source or CostSource.ESTIMATE,
        )
        return metrics, versions, cost

    @staticmethod
    def _response_json(response: httpx.Response) -> Any:
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise RunPodAdapterError("RunPod response exceeded the adapter limit")
        return response.json()

    @staticmethod
    def _provider_metrics(metrics: _WorkerMetrics) -> ProviderMetrics:
        cost_source = (
            CostSource.ESTIMATE
            if metrics.cost_source == "ESTIMATE"
            else CostSource.ACTUAL
        )
        return ProviderMetrics(
            gpu_type=metrics.gpu,
            queue_ms=metrics.queue_ms,
            cold_start_ms=metrics.cold_start_ms,
            runtime_ms=metrics.runtime_ms,
            billable_ms=metrics.billable_ms,
            cost_minor=metrics.cost_minor,
            currency=metrics.currency,
            cost_source=cost_source,
        )

    @staticmethod
    def _provider_status(status: str) -> ProviderStatus:
        return {
            "IN_QUEUE": ProviderStatus.PENDING,
            "IN_PROGRESS": ProviderStatus.RUNNING,
            "COMPLETED": ProviderStatus.SUCCEEDED,
            "FAILED": ProviderStatus.FAILED,
            "TIMED_OUT": ProviderStatus.FAILED,
            "CANCELLED": ProviderStatus.CANCELLED,
        }.get(status, ProviderStatus.UNKNOWN)

    @staticmethod
    def _failed(
        provider_job_id: str,
        code: FailureCode,
        message: str,
    ) -> PollResult:
        return PollResult(
            ProviderStatus.FAILED,
            provider_job_id=provider_job_id,
            failure=ProviderFailure(code, message),
        )

    def _http_failure(self, provider_job_id: str, status_code: int) -> PollResult:
        if status_code in {401, 403}:
            code = FailureCode.PROVIDER_AUTHENTICATION
        elif status_code == 429:
            code = FailureCode.PROVIDER_CAPACITY
        elif status_code >= 500:
            code = FailureCode.PROVIDER_5XX
        elif status_code == 404:
            code = FailureCode.OUTPUT_MISSING
        else:
            code = FailureCode.INTERNAL_ERROR
        return self._failed(
            provider_job_id,
            code,
            f"RunPod status request failed with HTTP {status_code}",
        )

    @staticmethod
    def _submit_http_failure_code(status_code: int) -> FailureCode | None:
        if status_code in {400, 422}:
            return FailureCode.INVALID_INPUT
        if status_code in {401, 403}:
            return FailureCode.PROVIDER_AUTHENTICATION
        if status_code == 404:
            return FailureCode.NO_ROUTE
        if status_code == 409:
            return FailureCode.STATE_CONFLICT
        if status_code == 429:
            return FailureCode.PROVIDER_CAPACITY
        return None
