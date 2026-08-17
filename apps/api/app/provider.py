import asyncio
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.ledger import finish_reservation
from app.models import Attempt, AttemptStatus, Job, JobEvent, JobStatus, Output
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
    job: Job,
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
            job = db.get(Job, job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return
            attempt = db.scalar(select(Attempt).where(Attempt.job_id == job_id))
            if attempt is None or attempt.provider_job_id is not None:
                return
            attempt.provider_job_id = provider_id
            transition(db, job, JobStatus.RUNNING, "provider.started", f"{provider_id}:running")
            attempt.status = AttemptStatus.RUNNING
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

        content = self._create_mp4()
        self._finish_success(job_id, provider_id, content)
        if job.mock_mode == "duplicate":
            self._finish_success(job_id, provider_id, content)

    async def poll(self, provider_job_id: str) -> str:
        return "completed"

    async def cancel(self, provider_job_id: str) -> None:
        return None

    def _is_cancelled(self, job_id: uuid.UUID) -> bool:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            return job is None or job.status == JobStatus.CANCELLED

    def _fail(self, job_id: uuid.UUID, provider_id: str, code: str, message: str) -> None:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            attempt = db.scalar(select(Attempt).where(Attempt.job_id == job_id))
            if job is None or attempt is None or job.status == JobStatus.CANCELLED:
                return
            if transition(
                db, job, JobStatus.FAILED_FINAL, "provider.failed", f"{provider_id}:failed"
            ):
                job.error_code = code
                job.error_message = message
                attempt.status = AttemptStatus.FAILED
                finish_reservation(db, job, settle=False)
                db.commit()

    def _finish_corrupt(self, job_id: uuid.UUID, provider_id: str) -> None:
        stored = self.storage.write_bytes("outputs", b"not-an-mp4", "video/mp4")
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            attempt = db.scalar(select(Attempt).where(Attempt.job_id == job_id))
            if job is None or attempt is None or job.status == JobStatus.CANCELLED:
                self.storage.delete(stored.key)
                return
            db.add(
                Output(
                    job_id=job.id,
                    attempt_id=attempt.id,
                    storage_key=stored.key,
                    mime_type=stored.mime_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    is_valid=False,
                )
            )
            transition(db, job, JobStatus.FAILED_FINAL, "output.invalid", f"{provider_id}:corrupt")
            job.error_code = "OUTPUT_INVALID_MP4"
            job.error_message = "Provider 输出不是有效 MP4"
            attempt.status = AttemptStatus.FAILED
            finish_reservation(db, job, settle=False)
            db.commit()

    def _finish_success(self, job_id: uuid.UUID, provider_id: str, content: bytes) -> None:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            attempt = db.scalar(select(Attempt).where(Attempt.job_id == job_id))
            if job is None or attempt is None or job.status != JobStatus.RUNNING:
                return
            dedup_key = f"{provider_id}:completed"
            if db.scalar(select(JobEvent.id).where(JobEvent.dedup_key == dedup_key)):
                return
            stored = self.storage.write_bytes("outputs", content, "video/mp4")
            db.add(
                Output(
                    job_id=job.id,
                    attempt_id=attempt.id,
                    storage_key=stored.key,
                    mime_type=stored.mime_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    is_valid=True,
                )
            )
            transition(db, job, JobStatus.SUCCEEDED, "provider.completed", dedup_key)
            attempt.status = AttemptStatus.SUCCEEDED
            finish_reservation(db, job, settle=True)
            db.commit()

    @staticmethod
    def _create_mp4() -> bytes:
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
                    "color=c=0x111827:s=640x360:d=1",
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
