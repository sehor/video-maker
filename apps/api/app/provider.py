import asyncio
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobEvent,
    JobStatus,
    OutputValidationStatus,
)
from app.storage import LocalObjectStorage


class VideoProvider(Protocol):
    async def submit(self, job_id: uuid.UUID) -> None: ...

    async def poll(self, provider_job_id: str) -> str: ...

    async def cancel(self, provider_job_id: str) -> None: ...


ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.CREATED: {JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.SUCCEEDED, JobStatus.FAILED_FINAL, JobStatus.CANCELLED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED_FINAL: set(),
    JobStatus.CANCELLED: set(),
}


def transition(
    db: Session,
    job: GenerationJob,
    to_status: JobStatus,
    event_type: str,
    dedup_key: str,
) -> bool:
    if db.scalar(select(JobEvent.id).where(JobEvent.dedup_key == dedup_key)):
        return False
    if to_status not in ALLOWED_TRANSITIONS[job.status]:
        return False
    from_status = job.status
    job.status = to_status
    db.add(
        JobEvent(
            job_id=job.id,
            event_type=event_type,
            from_status=from_status.value,
            to_status=to_status.value,
            dedup_key=dedup_key,
        )
    )
    return True


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
            transition(db, job, JobStatus.RUNNING, "provider.started", f"{provider_id}:running")
            attempt.status = AttemptStatus.RUNNING
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

        content = self._create_mp4(job.duration_ms / 1000)
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
            if transition(
                db, job, JobStatus.FAILED_FINAL, "provider.failed", f"{provider_id}:failed"
            ):
                job.failure_code = code
                job.error_message = message
                job.finished_at = datetime.now(UTC)
                attempt.failure_code = code
                attempt.finished_at = datetime.now(UTC)
                attempt.status = (
                    AttemptStatus.TIMED_OUT
                    if code == "MOCK_TIMEOUT"
                    else AttemptStatus.FAILED_FINAL
                )
                db.commit()

    def _finish_corrupt(self, job_id: uuid.UUID, provider_id: str) -> None:
        stored = self.storage.write_bytes("outputs", b"not-an-mp4", "video/mp4")
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
            if job is None or attempt is None or job.status == JobStatus.CANCELLED:
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
            transition(db, job, JobStatus.FAILED_FINAL, "output.invalid", f"{provider_id}:corrupt")
            job.failure_code = "OUTPUT_INVALID_MP4"
            job.error_message = "Provider 输出不是有效 MP4"
            job.finished_at = datetime.now(UTC)
            attempt.failure_code = "OUTPUT_INVALID_MP4"
            attempt.finished_at = datetime.now(UTC)
            attempt.status = AttemptStatus.FAILED_FINAL
            db.commit()

    def _finish_success(self, job_id: uuid.UUID, provider_id: str, content: bytes) -> None:
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
            if job is None or attempt is None or job.status != JobStatus.RUNNING:
                return
            dedup_key = f"{provider_id}:completed"
            if db.scalar(select(JobEvent.id).where(JobEvent.dedup_key == dedup_key)):
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
            transition(db, job, JobStatus.SUCCEEDED, "provider.completed", dedup_key)
            job.finished_at = datetime.now(UTC)
            attempt.status = AttemptStatus.SUCCEEDED
            attempt.finished_at = datetime.now(UTC)
            db.commit()

    @staticmethod
    def _create_mp4(duration_seconds: float) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mock.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c=0x111827:s=1280x720:d={duration_seconds}",
                    "-c:v",
                    "mpeg4",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    "-y",
                    str(path),
                ],
                check=True,
                timeout=15,
            )
            return path.read_bytes()
