import asyncio
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobStatus,
    LedgerTransaction,
    SettlementStatus,
)
from app.provider import (
    CancelResult,
    CostResult,
    PollResult,
    ProviderAttempt,
    ProviderOutput,
    ProviderStatus,
    SubmitDisposition,
    SubmitRequest,
    SubmitResult,
    WebhookVerificationError,
    WebhookVerificationRequest,
)
from app.provider_execution import GenerationExecutionService
from app.routing import MOCK_ROUTE_VERSION, get_route_registry
from app.simulators import SimulatedRunPodMetrics, SimulatedRunPodProvenance
from app.storage import LocalObjectStorage, ObjectStorage
from tests.test_projects_permissions import create_project


class ArtifactProvider:
    def __init__(
        self,
        storage: ObjectStorage,
        content: bytes,
        *,
        object_key: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self._storage = storage
        self._content = content
        self._object_key = object_key
        self._size_bytes = size_bytes
        self._sha256 = sha256
        self._metadata = metadata or {}
        self._requests: dict[uuid.UUID, SubmitRequest] = {}
        self._outputs: dict[uuid.UUID, ProviderOutput] = {}
        self._provenance = SimulatedRunPodProvenance()
        self._metrics = SimulatedRunPodMetrics()
        self.poll_calls = 0

    async def submit(self, request: SubmitRequest) -> SubmitResult:
        self._requests[request.attempt_id] = request
        return SubmitResult(
            SubmitDisposition.ACCEPTED,
            f"artifact-{request.attempt_id.hex}",
        )

    async def poll(self, attempt: ProviderAttempt) -> PollResult:
        self.poll_calls += 1
        output = self._outputs.get(attempt.attempt_id)
        if output is None:
            request = self._requests[attempt.attempt_id]
            claim = self._storage.write_claim(
                f"provider-outputs/{request.job_id}/{request.attempt_id}",
                mime_type="video/mp4",
                max_bytes=max(1, len(self._content)),
            )
            stored = self._storage.put(claim, self._content, "video/mp4")
            output = ProviderOutput(
                media_type="video/mp4",
                object_key=self._object_key or stored.key,
                size_bytes=(
                    stored.size_bytes
                    if self._size_bytes is None
                    else self._size_bytes
                ),
                sha256=self._sha256 or stored.sha256,
            )
            width, height = (1280, 720) if request.aspect_ratio == "16:9" else (720, 1280)
            output = replace(output, **{
                "duration_ms": request.duration_ms, "width": width, "height": height,
                "fps": 16, "codec": "h264", **self._metadata,
            })
            self._outputs[attempt.attempt_id] = output
        return PollResult(
            ProviderStatus.SUCCEEDED,
            provider_job_id=attempt.provider_job_id,
            output=output,
            metrics=self._metrics.provider_metrics(self._provenance.gpu_type),
            versions=replace(
                self._provenance.versions(),
                workflow_version=self._requests[attempt.attempt_id].workflow_id,
            ),
        )

    async def cancel(self, attempt: ProviderAttempt) -> CancelResult:
        return CancelResult(False, ProviderStatus.UNKNOWN)

    async def verify_webhook(self, request: WebhookVerificationRequest):
        raise WebhookVerificationError("not used")

    async def read_cost(self, attempt: ProviderAttempt) -> CostResult | None:
        return CostResult(
            amount_minor=self._metrics.cost_minor,
            currency=self._metrics.currency,
            source=self._metrics.cost_source,
        )


@pytest.fixture(autouse=True)
def use_mock_route():
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(MOCK_ROUTE_VERSION)
    routes.set_enabled(MOCK_ROUTE_VERSION, True)
    yield
    routes.activate(previous)


def _create_job(client: TestClient, *, aspect_ratio: str) -> uuid.UUID:
    project = create_project(client)
    shot = client.post(
        f"/v1/projects/{project['id']}/shots",
        json={
            "title": "Remote artifact receipt",
            "prompt": "A controlled media validation fixture.",
            "duration_seconds": 5,
            "aspect_ratio": aspect_ratio,
        },
    )
    assert shot.status_code == 201
    shot_id = shot.json()["id"]
    grant = client.post(
        "/v1/wallet/test-grants",
        json={
            "tier": "FAST",
            "amount_ms": 10_000,
            "idempotency_key": f"artifact-grant:{uuid.uuid4()}",
            "reason": "remote artifact test",
        },
    )
    assert grant.status_code == 201
    quote = client.post(
        "/v1/quotes",
        json={"shot_id": shot_id, "tier": "FAST", "resolution": "720P"},
    )
    assert quote.status_code == 201
    created = client.post(
        "/v1/generations",
        json={"shot_id": shot_id, "quote_id": quote.json()["id"]},
    )
    assert created.status_code == 202
    return uuid.UUID(created.json()["id"])


def _storage() -> LocalObjectStorage:
    settings = get_settings()
    return LocalObjectStorage(
        settings.storage_root,
        settings.storage_claim_secret.get_secret_value().encode(),
    )


def _execute(
    raw_client: TestClient,
    content: bytes,
    *,
    aspect_ratio: str = "16:9",
    object_key: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    metadata: dict | None = None,
) -> tuple[uuid.UUID, ArtifactProvider, GenerationExecutionService]:
    job_id = _create_job(raw_client, aspect_ratio=aspect_ratio)
    storage = _storage()
    provider = ArtifactProvider(
        storage,
        content,
        object_key=object_key,
        size_bytes=size_bytes,
        sha256=sha256,
        metadata=metadata,
    )
    executor = GenerationExecutionService(storage, provider=provider)
    completed = asyncio.run(executor.execute(job_id))
    assert completed.is_complete
    return job_id, provider, executor


@pytest.mark.parametrize("aspect_ratio", ["16:9", "9:16"])
def test_valid_remote_artifact_publishes_and_settles_once(
    raw_client: TestClient,
    aspect_ratio: str,
) -> None:
    content = b"opaque-provider-video"
    job_id, provider, executor = _execute(
        raw_client,
        content,
        aspect_ratio=aspect_ratio,
    )
    replay = asyncio.run(executor.execute(job_id))
    assert replay.is_complete
    assert provider.poll_calls == 1

    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED, (
            job.failure_code,
            job.error_message,
        )
        assert job.settlement_status == SettlementStatus.SETTLED
        attempt = job.attempts[0]
        assert attempt.cost_source == "SIMULATED"
        assert attempt.gpu_type == "NVIDIA L40S (SIMULATED)"
        assert attempt.image_digest == SimulatedRunPodProvenance().image_digest
        assert attempt.workflow_hash == SimulatedRunPodProvenance().workflow_sha256
        output = db.scalar(
            select(GenerationOutput).where(GenerationOutput.job_id == job_id)
        )
        assert output is not None
        expected_dimensions = (1280, 720) if aspect_ratio == "16:9" else (720, 1280)
        assert (output.width, output.height) == expected_dimensions
        assert output.codec == "h264"
        assert output.duration_ms == 5_000
        assert output.validation_status.value == "VALID"
        assert db.scalar(
            select(func.count())
            .select_from(GenerationOutput)
            .where(GenerationOutput.job_id == job_id)
        ) == 1
        assert db.scalar(
            select(func.count())
            .select_from(LedgerTransaction)
            .where(
                LedgerTransaction.reference_id == str(job_id),
                LedgerTransaction.tx_type == "SETTLE",
            )
        ) == 1


@pytest.mark.parametrize(
    ("metadata", "size_bytes", "sha256", "expected_failure"),
    [
        ({"duration_ms": None}, None, None, "OUTPUT_INVALID_MEDIA"),
        ({"width": 1920}, None, None, "OUTPUT_INVALID_MEDIA"),
        ({"codec": "vp9"}, None, None, "OUTPUT_INVALID_MEDIA"),
    ],
)
def test_invalid_remote_artifact_fails_refunds_and_never_publishes(
    raw_client: TestClient,
    metadata: dict,
    size_bytes: int | None,
    sha256: str | None,
    expected_failure: str,
) -> None:
    content = b"opaque-provider-video"
    job_id, _, executor = _execute(
        raw_client,
        content,
        size_bytes=size_bytes,
        sha256=sha256,
        metadata=metadata,
    )
    assert asyncio.run(executor.execute(job_id)).is_complete

    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        assert job.status == JobStatus.FAILED_FINAL
        assert job.settlement_status == SettlementStatus.RELEASED
        assert job.failure_code == expected_failure
        assert db.scalar(
            select(func.count())
            .select_from(GenerationOutput)
            .where(GenerationOutput.job_id == job_id)
        ) == 0
        assert db.scalar(
            select(func.count())
            .select_from(LedgerTransaction)
            .where(
                LedgerTransaction.reference_id == str(job_id),
                LedgerTransaction.tx_type == "RELEASE",
            )
        ) == 1


def test_uncontrolled_object_key_is_rejected_before_download(
    raw_client: TestClient,
) -> None:
    content = b"opaque-provider-video"
    job_id, _, _ = _execute(
        raw_client,
        content,
        object_key="https://attacker.invalid/output.mp4",
    )

    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        assert job.status == JobStatus.FAILED_FINAL
        assert job.settlement_status == SettlementStatus.RELEASED
        assert job.failure_code == "OUTPUT_CORRUPTED"
        attempt = db.scalar(
            select(GenerationAttempt).where(GenerationAttempt.job_id == job_id)
        )
        assert attempt is not None
        assert attempt.status.value == "FAILED_FINAL"


def test_legacy_hash_and_size_declarations_do_not_gate_acceptance(raw_client):
    content = b"opaque-provider-video"
    job_id, _, _ = _execute(raw_client, content, size_bytes=1, sha256="not-a-media-hash")
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        assert job.status == JobStatus.SUCCEEDED
        assert job.settlement_status == SettlementStatus.SETTLED
        output = db.get(GenerationOutput, job.final_output_id)
        assert output.size_bytes == len(content)
        assert output.sha256 is None


def test_empty_remote_object_is_rejected(raw_client):
    job_id, _, _ = _execute(raw_client, b"")
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        assert job.status == JobStatus.FAILED_FINAL
        assert job.settlement_status == SettlementStatus.RELEASED
        assert job.final_output_id is None


pytestmark = [pytest.mark.database, pytest.mark.media]
