from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from io import BytesIO
from types import MappingProxyType
from typing import BinaryIO

from app.errors import ApiError
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
    ProviderVersions,
    SubmitDisposition,
    SubmitRequest,
    SubmitResult,
    WebhookVerificationError,
    WebhookVerificationRequest,
    mock_video_fixture,
    validate_fixed_worker_request,
)
from app.storage import (
    ALLOWED_TYPES,
    ClaimOperation,
    ObjectStat,
    RemoteObjectStorage,
    StorageClaim,
    StoredObject,
    _valid_namespace,
)

DEFAULT_CLOCK_TIME = datetime(2026, 1, 1, tzinfo=UTC)
SIMULATED_IMAGE_DIGEST = "sha256:" + hashlib.sha256(
    b"video-maker:deterministic-runpod-image:v1"
).hexdigest()
SIMULATED_MODEL_HASHES = MappingProxyType(
    {
        name: hashlib.sha256(f"video-maker:simulated-model:{name}:v1".encode()).hexdigest()
        for name in (
            "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "wan2.1_i2v_14B_fp8_e4m3fn.safetensors",
            "wan_2.1_vae.safetensors",
        )
    }
)


class ManualClock:
    """Timezone-aware clock whose progress is controlled entirely by tests."""

    def __init__(self, initial: datetime = DEFAULT_CLOCK_TIME) -> None:
        if initial.tzinfo is None:
            raise ValueError("manual clock requires a timezone-aware datetime")
        self._now = initial.astimezone(UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("manual clock cannot move backwards")
        self._now += delta
        return self._now


class FaultPoint(StrEnum):
    RUNPOD_SUBMIT = "RUNPOD_SUBMIT"
    RUNPOD_POLL = "RUNPOD_POLL"
    RUNPOD_CANCEL = "RUNPOD_CANCEL"
    RUNPOD_COST = "RUNPOD_COST"
    STORAGE_PUT = "STORAGE_PUT"
    STORAGE_OPEN = "STORAGE_OPEN"
    STORAGE_STAT = "STORAGE_STAT"
    STORAGE_DELETE = "STORAGE_DELETE"


class FaultInjector:
    """FIFO, fail-once fault injection with no randomness or sleeping."""

    def __init__(self) -> None:
        self._faults: dict[FaultPoint, deque[Callable[[], Exception]]] = defaultdict(deque)

    def fail_next(
        self,
        point: FaultPoint,
        error: Exception | Callable[[], Exception],
    ) -> None:
        factory = error if callable(error) else lambda: error
        self._faults[point].append(factory)

    def trigger(self, point: FaultPoint) -> None:
        faults = self._faults[point]
        if faults:
            raise faults.popleft()()

    def pending(self, point: FaultPoint) -> int:
        return len(self._faults[point])


@dataclass(frozen=True, slots=True)
class FakeObjectMetadata:
    key: str
    mime_type: str
    size_bytes: int
    sha256: str | None
    etag: str
    created_at: datetime


class FakeRemoteBackend:
    """In-memory private-object backend used by FakeRemoteStorage."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        faults: FaultInjector | None = None,
    ) -> None:
        self._clock = clock
        self._faults = faults or FaultInjector()
        self._objects: dict[str, tuple[bytes, FakeObjectMetadata]] = {}

    def put(self, key: str, content: bytes, mime_type: str) -> None:
        self._faults.trigger(FaultPoint.STORAGE_PUT)
        if key in self._objects:
            raise FileExistsError(key)
        body = bytes(content)
        metadata = FakeObjectMetadata(
            key=key,
            mime_type=mime_type,
            size_bytes=len(body),
            sha256=None,
            etag=f'"{uuid.uuid4().hex}"',
            created_at=self._clock().astimezone(UTC),
        )
        self._objects[key] = (body, metadata)

    def open(self, key: str) -> BinaryIO:
        self._faults.trigger(FaultPoint.STORAGE_OPEN)
        stored = self._objects.get(key)
        if stored is None:
            raise FileNotFoundError(key)
        return BytesIO(stored[0])

    def stat(self, key: str) -> ObjectStat | None:
        self._faults.trigger(FaultPoint.STORAGE_STAT)
        stored = self._objects.get(key)
        if stored is None:
            return None
        metadata = stored[1]
        return ObjectStat(metadata.key, metadata.size_bytes, metadata.sha256)

    def delete(self, key: str) -> None:
        self._faults.trigger(FaultPoint.STORAGE_DELETE)
        self._objects.pop(key, None)

    def metadata(self, key: str) -> FakeObjectMetadata:
        stored = self._objects.get(key)
        if stored is None:
            raise ApiError(404, "STORAGE_OBJECT_NOT_FOUND", "storage object not found")
        return stored[1]

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._objects))


class FakeRemoteStorage(RemoteObjectStorage):
    """Deterministic, signed-claim remote storage with inspectable metadata."""

    def __init__(
        self,
        claim_secret: bytes,
        *,
        clock: Callable[[], datetime],
        faults: FaultInjector | None = None,
    ) -> None:
        self._fake_backend = FakeRemoteBackend(clock=clock, faults=faults)
        super().__init__(self._fake_backend, claim_secret, clock=clock)
        self._claim_sequence = 0
        self._issued_claims: dict[str, StorageClaim] = {}

    def write_claim(
        self,
        namespace: str,
        *,
        mime_type: str,
        max_bytes: int,
        expires_in: timedelta = timedelta(minutes=5),
    ) -> StorageClaim:
        if mime_type not in ALLOWED_TYPES:
            raise ApiError(415, "UPLOAD_TYPE_NOT_ALLOWED", "unsupported upload type")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._claim_sequence += 1
        suffix = ALLOWED_TYPES[mime_type][1]
        key = f"{_valid_namespace(namespace)}/{self._claim_sequence:032x}{suffix}"
        claim = self._claims.issue(
            ClaimOperation.WRITE,
            key,
            expires_in,
            mime_type=mime_type,
            max_bytes=max_bytes,
        )
        self._issued_claims[claim.token] = claim
        return claim

    def read_claim(
        self,
        key: str,
        *,
        expires_in: timedelta = timedelta(minutes=5),
    ) -> StorageClaim:
        claim = super().read_claim(key, expires_in=expires_in)
        self._issued_claims[claim.token] = claim
        return claim

    def resolve_claim(self, token: str) -> StorageClaim:
        try:
            return self._issued_claims[token]
        except KeyError as exc:
            raise ApiError(403, "STORAGE_CLAIM_INVALID", "unknown storage claim") from exc

    def metadata(self, key: str) -> FakeObjectMetadata:
        return self._fake_backend.metadata(key)

    def keys(self) -> tuple[str, ...]:
        return self._fake_backend.keys()


class SimulatedRunPodOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True, slots=True)
class SimulatedRunPodProvenance:
    gpu_type: str = "NVIDIA L40S (SIMULATED)"
    image_digest: str = SIMULATED_IMAGE_DIGEST
    worker_version: str = "worker-comfyui-simulator/1.0.0"
    worker_commit: str = "a1981e99b1f5a7201f387653420ad1f275b97d0a"
    comfyui_version: str = "v0.29.0"
    comfyui_commit: str = "a8c44f9b2a0678ac4082e3529a3f43db7472acfe"
    workflow_id: str = "fast_wan_i2v_720_v1"
    workflow_sha256: str = "454f03238c881b751529524c1db47c16619cb0dce13cb7c694f55653acd35fad"
    model_sha256: Mapping[str, str] = field(
        default_factory=lambda: SIMULATED_MODEL_HASHES
    )

    def versions(self) -> ProviderVersions:
        return ProviderVersions(
            image_digest=self.image_digest,
            worker_version=self.worker_version,
            worker_commit=self.worker_commit,
            comfyui_version=self.comfyui_version,
            comfyui_commit=self.comfyui_commit,
            workflow_version=self.workflow_id,
            workflow_hash=self.workflow_sha256,
            model_hashes=self.model_sha256,
        )


@dataclass(frozen=True, slots=True)
class SimulatedRunPodMetrics:
    queue_ms: int = 2_000
    cold_start_ms: int = 500
    runtime_ms: int = 3_000
    billable_ms: int = 3_500
    cost_minor: int = 7
    currency: str = "USD"
    cost_source: CostSource = CostSource.SIMULATED

    def provider_metrics(self, gpu_type: str) -> ProviderMetrics:
        return ProviderMetrics(
            gpu_type=gpu_type,
            queue_ms=self.queue_ms,
            cold_start_ms=self.cold_start_ms,
            runtime_ms=self.runtime_ms,
            billable_ms=self.billable_ms,
        )


@dataclass(frozen=True, slots=True)
class SimulatedRunPodJob:
    provider_job_id: str
    attempt_id: uuid.UUID
    idempotency_key: str
    status: ProviderStatus
    outcome: SimulatedRunPodOutcome
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    failure: ProviderFailure | None
    provenance: SimulatedRunPodProvenance
    metrics: SimulatedRunPodMetrics


@dataclass(slots=True)
class _MutableSimulatedRunPodJob:
    request: SubmitRequest
    provider_job_id: str
    outcome: SimulatedRunPodOutcome
    status: ProviderStatus
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure: ProviderFailure | None = None


class DeterministicRunPodSimulator:
    """Stateful, fully offline RunPod contract simulator."""

    _TERMINAL = frozenset(
        {ProviderStatus.SUCCEEDED, ProviderStatus.FAILED, ProviderStatus.CANCELLED}
    )

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        faults: FaultInjector | None = None,
        provenance: SimulatedRunPodProvenance | None = None,
        metrics: SimulatedRunPodMetrics | None = None,
        timeout_after: timedelta = timedelta(seconds=4),
    ) -> None:
        if timeout_after <= timedelta(0):
            raise ValueError("timeout_after must be positive")
        self._clock = clock
        self._faults = faults or FaultInjector()
        self._provenance = provenance or SimulatedRunPodProvenance()
        self._metrics = metrics or SimulatedRunPodMetrics()
        self._queue_duration = timedelta(milliseconds=self._metrics.queue_ms)
        self._runtime_duration = timedelta(milliseconds=self._metrics.runtime_ms)
        self._timeout_after = timeout_after
        self._jobs: dict[str, _MutableSimulatedRunPodJob] = {}
        self._job_ids_by_idempotency_key: dict[str, str] = {}

    @staticmethod
    def _provider_job_id(idempotency_key: str) -> str:
        value = uuid.uuid5(uuid.NAMESPACE_URL, f"video-maker:runpod-simulator:{idempotency_key}")
        return f"runpod-sim-{value.hex}"

    async def submit(self, request: SubmitRequest) -> SubmitResult:
        validate_fixed_worker_request(
            request,
            workflow_id=self._provenance.workflow_id,
        )
        self._faults.trigger(FaultPoint.RUNPOD_SUBMIT)
        existing_id = self._job_ids_by_idempotency_key.get(request.idempotency_key)
        if existing_id is not None:
            existing = self._jobs[existing_id]
            if existing.request.attempt_id != request.attempt_id:
                raise ValueError("idempotency key belongs to another attempt")
            return SubmitResult(SubmitDisposition.ACCEPTED, existing_id)

        try:
            outcome = SimulatedRunPodOutcome(request.mode.upper())
        except ValueError as exc:
            raise ValueError("simulator mode must be success, failure, or timeout") from exc
        provider_job_id = self._provider_job_id(request.idempotency_key)
        job = _MutableSimulatedRunPodJob(
            request=request,
            provider_job_id=provider_job_id,
            outcome=outcome,
            status=ProviderStatus.PENDING,
            submitted_at=self._clock().astimezone(UTC),
        )
        self._jobs[provider_job_id] = job
        self._job_ids_by_idempotency_key[request.idempotency_key] = provider_job_id
        return SubmitResult(SubmitDisposition.ACCEPTED, provider_job_id)

    async def poll(self, attempt: ProviderAttempt) -> PollResult:
        self._faults.trigger(FaultPoint.RUNPOD_POLL)
        job = self._find_job(attempt)
        if job is None or job.request.attempt_id != attempt.attempt_id:
            return PollResult(ProviderStatus.UNKNOWN, provider_job_id=attempt.provider_job_id)
        self._refresh(job)
        if job.status == ProviderStatus.SUCCEEDED:
            return PollResult(
                status=job.status,
                provider_job_id=job.provider_job_id,
                output=self._output(job.request),
                metrics=self._metrics.provider_metrics(self._provenance.gpu_type),
                versions=self._provenance.versions(),
            )
        return PollResult(
            status=job.status,
            provider_job_id=job.provider_job_id,
            failure=job.failure,
            metrics=(
                self._metrics.provider_metrics(self._provenance.gpu_type)
                if job.status in self._TERMINAL
                else None
            ),
            versions=(self._provenance.versions() if job.status in self._TERMINAL else None),
        )

    async def cancel(self, attempt: ProviderAttempt) -> CancelResult:
        self._faults.trigger(FaultPoint.RUNPOD_CANCEL)
        job = self._find_job(attempt)
        if job is None or job.request.attempt_id != attempt.attempt_id:
            return CancelResult(False, ProviderStatus.UNKNOWN)
        self._refresh(job)
        if job.status in self._TERMINAL:
            return CancelResult(False, job.status)
        now = self._clock().astimezone(UTC)
        job.status = ProviderStatus.CANCELLED
        job.finished_at = now
        job.failure = ProviderFailure(FailureCode.USER_CANCELLED, "simulated job cancelled")
        return CancelResult(True, ProviderStatus.CANCELLED)

    async def verify_webhook(
        self, request: WebhookVerificationRequest
    ) -> ProviderEvent:
        raise WebhookVerificationError("RunPod simulator webhooks are disabled; use polling")

    async def read_cost(self, attempt: ProviderAttempt) -> CostResult | None:
        self._faults.trigger(FaultPoint.RUNPOD_COST)
        job = self._find_job(attempt)
        if job is None or job.request.attempt_id != attempt.attempt_id:
            return None
        return CostResult(
            amount_minor=self._metrics.cost_minor,
            currency=self._metrics.currency,
            source=self._metrics.cost_source,
        )

    def job(self, provider_job_id: str) -> SimulatedRunPodJob:
        try:
            job = self._jobs[provider_job_id]
        except KeyError as exc:
            raise KeyError(f"unknown simulated RunPod job: {provider_job_id}") from exc
        self._refresh(job)
        return SimulatedRunPodJob(
            provider_job_id=job.provider_job_id,
            attempt_id=job.request.attempt_id,
            idempotency_key=job.request.idempotency_key,
            status=job.status,
            outcome=job.outcome,
            submitted_at=job.submitted_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            failure=job.failure,
            provenance=self._provenance,
            metrics=self._metrics,
        )

    def jobs(self) -> tuple[SimulatedRunPodJob, ...]:
        return tuple(self.job(job_id) for job_id in sorted(self._jobs))

    def submission(self, provider_job_id: str) -> SubmitRequest:
        """Return the immutable submitted DTO for contract-test inspection."""

        try:
            return self._jobs[provider_job_id].request
        except KeyError as exc:
            raise KeyError(f"unknown simulated RunPod job: {provider_job_id}") from exc

    def _find_job(self, attempt: ProviderAttempt) -> _MutableSimulatedRunPodJob | None:
        provider_job_id = attempt.provider_job_id or self._job_ids_by_idempotency_key.get(
            attempt.idempotency_key
        )
        return self._jobs.get(provider_job_id) if provider_job_id is not None else None

    def _refresh(self, job: _MutableSimulatedRunPodJob) -> None:
        if job.status in self._TERMINAL:
            return
        now = self._clock().astimezone(UTC)
        elapsed = now - job.submitted_at
        if job.outcome == SimulatedRunPodOutcome.TIMEOUT and elapsed >= self._timeout_after:
            job.status = ProviderStatus.FAILED
            job.started_at = job.started_at or job.submitted_at + self._queue_duration
            job.finished_at = job.submitted_at + self._timeout_after
            job.failure = ProviderFailure(FailureCode.QUEUE_TIMEOUT, "simulated RunPod timeout")
            return
        if elapsed >= self._queue_duration + self._runtime_duration:
            job.started_at = job.started_at or job.submitted_at + self._queue_duration
            job.finished_at = job.submitted_at + self._queue_duration + self._runtime_duration
            if job.outcome == SimulatedRunPodOutcome.FAILURE:
                job.status = ProviderStatus.FAILED
                job.failure = ProviderFailure(
                    FailureCode.WORKFLOW_FAILED,
                    "simulated workflow failure",
                )
            else:
                job.status = ProviderStatus.SUCCEEDED
            return
        if elapsed >= self._queue_duration:
            job.status = ProviderStatus.RUNNING
            job.started_at = job.submitted_at + self._queue_duration

    @staticmethod
    def _output(request: SubmitRequest) -> ProviderOutput:
        width, height = (720, 1280) if request.aspect_ratio == "9:16" else (1280, 720)
        return ProviderOutput(
            content=mock_video_fixture(),
            media_type="video/mp4",
            duration_ms=request.duration_ms,
            width=width,
            height=height,
            fps=16,
            codec="h264",
        )


def put_fake_object(
    storage: FakeRemoteStorage,
    *,
    namespace: str,
    content: bytes,
    mime_type: str,
) -> tuple[StoredObject, FakeObjectMetadata]:
    """Small helper for deterministic simulator fixtures."""

    claim = storage.write_claim(namespace, mime_type=mime_type, max_bytes=len(content))
    stored = storage.put(claim, content, mime_type)
    return stored, storage.metadata(stored.key)
