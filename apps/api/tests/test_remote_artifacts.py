import asyncio
import shutil
import subprocess
import uuid
from pathlib import Path

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
from app.storage import LocalObjectStorage, ObjectStorage
from tests.test_projects_permissions import create_project

MEDIA_FIXTURES = Path(__file__).parent / "fixtures" / "media"


class ArtifactProvider:
    def __init__(
        self,
        storage: ObjectStorage,
        content: bytes,
        *,
        object_key: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
    ) -> None:
        self._storage = storage
        self._content = content
        self._object_key = object_key
        self._size_bytes = size_bytes
        self._sha256 = sha256
        self._requests: dict[uuid.UUID, SubmitRequest] = {}
        self._outputs: dict[uuid.UUID, ProviderOutput] = {}
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
                max_bytes=len(self._content),
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
            self._outputs[attempt.attempt_id] = output
        return PollResult(
            ProviderStatus.SUCCEEDED,
            provider_job_id=attempt.provider_job_id,
            output=output,
        )

    async def cancel(self, attempt: ProviderAttempt) -> CancelResult:
        return CancelResult(False, ProviderStatus.UNKNOWN)

    async def verify_webhook(self, request: WebhookVerificationRequest):
        raise WebhookVerificationError("not used")

    async def read_cost(self, attempt: ProviderAttempt) -> CostResult | None:
        return None


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
) -> tuple[uuid.UUID, ArtifactProvider, GenerationExecutionService]:
    job_id = _create_job(raw_client, aspect_ratio=aspect_ratio)
    storage = _storage()
    provider = ArtifactProvider(
        storage,
        content,
        object_key=object_key,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    executor = GenerationExecutionService(storage, provider=provider)
    completed = asyncio.run(executor.execute(job_id))
    assert completed.is_complete
    return job_id, provider, executor


def _portrait_fixture(path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("local ffmpeg is required by the media-validation issue")
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=720x1280:r=1:d=5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(path),
        ],
        check=True,
        timeout=30,
        shell=False,
    )
    return path.read_bytes()


@pytest.mark.parametrize("aspect_ratio", ["16:9", "9:16"])
def test_valid_remote_artifact_publishes_and_settles_once(
    raw_client: TestClient,
    tmp_path: Path,
    aspect_ratio: str,
) -> None:
    content = (
        (MEDIA_FIXTURES / "valid-720p-h264.mp4").read_bytes()
        if aspect_ratio == "16:9"
        else _portrait_fixture(tmp_path / "portrait.mp4")
    )
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
        assert job.status == JobStatus.SUCCEEDED
        assert job.settlement_status == SettlementStatus.SETTLED
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
    ("fixture", "size_bytes", "sha256", "expected_failure"),
    [
        ("corrupt-decode.mp4", None, None, "OUTPUT_CORRUPTED"),
        ("wrong-resolution.mp4", None, None, "OUTPUT_INVALID_MEDIA"),
        ("wrong-codec.mp4", None, None, "OUTPUT_INVALID_MEDIA"),
        ("valid-720p-h264.mp4", 1, None, "OUTPUT_CORRUPTED"),
        ("valid-720p-h264.mp4", None, "0" * 64, "OUTPUT_CORRUPTED"),
    ],
)
def test_invalid_remote_artifact_fails_refunds_and_never_publishes(
    raw_client: TestClient,
    fixture: str,
    size_bytes: int | None,
    sha256: str | None,
    expected_failure: str,
) -> None:
    content = (MEDIA_FIXTURES / fixture).read_bytes()
    job_id, _, executor = _execute(
        raw_client,
        content,
        size_bytes=size_bytes,
        sha256=sha256,
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
    content = (MEDIA_FIXTURES / "valid-720p-h264.mp4").read_bytes()
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
