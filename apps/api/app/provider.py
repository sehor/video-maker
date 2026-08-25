import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select

from app.db import SessionLocal
from app.ledger import finish_reservation
from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobStatus,
    OutputValidationStatus,
)
from app.state_machine import transition_attempt, transition_job
from app.storage import LocalObjectStorage

MOCK_VIDEO_FIXTURE = Path(__file__).with_name("fixtures") / "mock-success.mp4"

# Backward-compatible module alias for existing internal callers.
transition = transition_job


class VideoProvider(Protocol):
    async def submit(self, job_id: uuid.UUID) -> None: ...

    async def poll(self, provider_job_id: str) -> str: ...

    async def cancel(self, provider_job_id: str) -> None: ...


class MockVideoProvider:
    def __init__(self, storage: LocalObjectStorage) -> None:
        self.storage = storage

    async def submit(self, job_id: uuid.UUID) -> None:
        provider_id = f"mock-{uuid.uuid4().hex}"
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            if job is None or job.status == JobStatus.CANCELLED:
                return
            attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
            if attempt is None:
                return
            attempt.provider_job_id = provider_id
            if not (
                transition_job(
                    db, job, JobStatus.ROUTING, "job.routing", f"job:{job.id}:routing:v1"
                )
                and transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.SUBMITTING,
                    "attempt.submitting",
                    f"attempt:{attempt.id}:submitting:v1",
                )
                and transition_job(
                    db,
                    job,
                    JobStatus.SUBMITTED,
                    "provider.submitted",
                    f"attempt:{attempt.id}:job-submitted:v1",
                    {"provider_job_id": provider_id},
                )
                and transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.SUBMITTED,
                    "attempt.submitted",
                    f"attempt:{attempt.id}:submitted:v1",
                    {"provider_job_id": provider_id},
                )
                and transition_job(
                    db,
                    job,
                    JobStatus.RUNNING,
                    "provider.started",
                    f"attempt:{attempt.id}:job-running:v1",
                )
                and transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.RUNNING,
                    "attempt.running",
                    f"attempt:{attempt.id}:running:v1",
                )
            ):
                db.rollback()
                return
            attempt.started_at = datetime.now(UTC)
            job.started_at = datetime.now(UTC)
            db.commit()

        if job.mock_mode == "delayed":
            await asyncio.sleep(1)
        elif job.mock_mode == "timeout":
            await asyncio.sleep(0.1)
            self._fail(job_id, provider_id, "MOCK_TIMEOUT", "Mock Provider 超时")
            return
        elif job.mock_mode == "failure":
            self._fail(job_id, provider_id, "MOCK_PROVIDER_FAILED", "Mock Provider 返回失败")
            return

        if self._is_cancelled(job_id):
            return
        if job.mock_mode == "corrupt":
            self._finish_corrupt(job_id, provider_id)
            return

        content = MOCK_VIDEO_FIXTURE.read_bytes()
        self._finish_success(job_id, provider_id, content)
        if job.mock_mode == "duplicate":
            self._finish_success(job_id, provider_id, content)

    async def poll(self, provider_job_id: str) -> str:
        return "completed"

    async def cancel(self, provider_job_id: str) -> None:
        return None

    def _is_cancelled(self, job_id: uuid.UUID) -> bool:
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            return job is None or job.status == JobStatus.CANCELLED

    def _fail(self, job_id: uuid.UUID, provider_id: str, code: str, message: str) -> None:
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
            if job is None or attempt is None or job.status == JobStatus.CANCELLED:
                return
            attempt_status = (
                AttemptStatus.TIMED_OUT if code == "MOCK_TIMEOUT" else AttemptStatus.FAILED_FINAL
            )
            if transition_job(
                db, job, JobStatus.FAILED_FINAL, "provider.failed", f"{provider_id}:failed"
            ) and transition_attempt(
                db,
                attempt,
                attempt_status,
                "attempt.failed",
                f"{provider_id}:attempt-failed",
                {"failure_code": code},
            ):
                job.failure_code = code
                job.error_message = message
                job.finished_at = datetime.now(UTC)
                attempt.failure_code = code
                attempt.finished_at = datetime.now(UTC)
                finish_reservation(db, job, settle=False)
                db.commit()
            else:
                db.rollback()

    def _finish_corrupt(self, job_id: uuid.UUID, provider_id: str) -> None:
        stored = self.storage.write_bytes("outputs", b"not-an-mp4", "video/mp4")
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
            if job is None or attempt is None or job.status == JobStatus.CANCELLED:
                self.storage.delete(stored.key)
                return
            if not transition_job(
                db,
                job,
                JobStatus.POSTPROCESSING,
                "output.postprocessing",
                f"{provider_id}:postprocessing",
            ):
                self.storage.delete(stored.key)
                return
            db.add(
                GenerationOutput(
                    job_id=job.id,
                    attempt_id=attempt.id,
                    object_key=stored.key,
                    media_type=stored.mime_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    validation_status=OutputValidationStatus.INVALID,
                )
            )
            if not transition_job(
                db,
                job,
                JobStatus.VALIDATING,
                "output.validating",
                f"{provider_id}:validating",
            ) or not transition_job(
                db,
                job,
                JobStatus.FAILED_FINAL,
                "output.invalid",
                f"{provider_id}:corrupt",
            ) or not transition_attempt(
                db,
                attempt,
                AttemptStatus.FAILED_FINAL,
                "attempt.failed",
                f"{provider_id}:attempt-corrupt",
                {"failure_code": "OUTPUT_INVALID_MP4"},
            ):
                db.rollback()
                self.storage.delete(stored.key)
                return
            job.failure_code = "OUTPUT_INVALID_MP4"
            job.error_message = "Provider 输出不是有效 MP4"
            job.finished_at = datetime.now(UTC)
            attempt.failure_code = "OUTPUT_INVALID_MP4"
            attempt.finished_at = datetime.now(UTC)
            finish_reservation(db, job, settle=False)
            db.commit()

    def _finish_success(self, job_id: uuid.UUID, provider_id: str, content: bytes) -> None:
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
            if job is None or attempt is None or job.status != JobStatus.RUNNING:
                return
            if not transition_job(
                db,
                job,
                JobStatus.POSTPROCESSING,
                "output.postprocessing",
                f"{provider_id}:postprocessing",
            ):
                return
            stored = self.storage.write_bytes("outputs", content, "video/mp4")
            output = GenerationOutput(
                job_id=job.id,
                attempt_id=attempt.id,
                object_key=stored.key,
                media_type=stored.mime_type,
                duration_ms=job.duration_ms,
                width=1280,
                height=720,
                fps=25,
                codec="mpeg4",
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                validation_status=OutputValidationStatus.VALID,
            )
            db.add(output)
            db.flush()
            job.final_output_id = output.id
            if not transition_job(
                db,
                job,
                JobStatus.VALIDATING,
                "output.validating",
                f"{provider_id}:validating",
            ) or not transition_job(
                db,
                job,
                JobStatus.SUCCEEDED,
                "provider.completed",
                f"{provider_id}:completed",
                {"output_id": str(output.id)},
            ) or not transition_attempt(
                db,
                attempt,
                AttemptStatus.SUCCEEDED,
                "attempt.succeeded",
                f"{provider_id}:attempt-completed",
                {"output_id": str(output.id)},
            ):
                db.rollback()
                self.storage.delete(stored.key)
                return
            job.finished_at = datetime.now(UTC)
            attempt.finished_at = datetime.now(UTC)
            finish_reservation(db, job, settle=True)
            db.commit()
