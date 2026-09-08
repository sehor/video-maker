from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.errors import ApiError
from app.media import MediaFacts, MediaPolicy, MediaValidationError, MediaValidator
from app.provider import FailureCode, ProviderFailure, ProviderOutput
from app.storage import ObjectStorage, StoredObject

READ_CHUNK_BYTES = 1024 * 1024


class ArtifactReceiptError(Exception):
    def __init__(self, failure: ProviderFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    stored: StoredObject
    facts: MediaFacts


class MockArtifactReceiver:
    """Accept embedded offline fixtures; never used by remote providers."""

    def __init__(
        self,
        storage: ObjectStorage,
        register_write: Callable[..., None],
        max_bytes: int,
        validator: MediaValidator,
    ) -> None:
        self._storage = storage
        self._register_write = register_write
        self._max_bytes = max_bytes
        self._validator = validator

    def receive(
        self,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        output: ProviderOutput,
        policy: MediaPolicy,
    ) -> tuple[PublishedArtifact, bool]:
        content = output.content
        if not content:
            raise ArtifactReceiptError(ProviderFailure(FailureCode.OUTPUT_MISSING, "Mock 输出为空"))
        try:
            facts = self._validator.validate(output, policy)
        except MediaValidationError as exc:
            raise ArtifactReceiptError(ProviderFailure(exc.failure_code, exc.message)) from exc
        claim = self._storage.write_claim(
            f"outputs/{job_id}/{attempt_id}",
            mime_type=output.media_type,
            max_bytes=self._max_bytes,
        )
        self._register_write(job_id, attempt_id, claim.object_key, "FINAL")
        stored = self._storage.put(claim, content, output.media_type)
        # Preserve the mock corruption fixture's INVALID row for diagnostics.
        valid = len(content) >= 8 and content[4:8] == b"ftyp"
        return PublishedArtifact(stored, facts), valid


class RemoteArtifactReceiver:
    """Accepts declared video metadata and copies a nonempty, bounded artifact."""

    def __init__(
        self,
        storage: ObjectStorage,
        media_validator: MediaValidator,
        *,
        max_bytes: int,
        register_write: Callable[[uuid.UUID, uuid.UUID, str], None] | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._storage = storage
        self._media_validator = media_validator
        self._max_bytes = max_bytes
        self._register_write = register_write

    def receive_and_publish(
        self,
        *,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        output: ProviderOutput,
        policy: MediaPolicy,
    ) -> PublishedArtifact:
        object_key = output.object_key
        expected_prefix = f"provider-outputs/{job_id}/{attempt_id}/"
        if (
            object_key is None
            or not object_key.startswith(expected_prefix)
            or "://" in object_key
            or "\\" in object_key
        ):
            self._reject(FailureCode.OUTPUT_CORRUPTED, "Provider 输出对象不在受控命名空间")
        if output.media_type != "video/mp4":
            self._reject(FailureCode.OUTPUT_INVALID_MEDIA, "Provider 输出媒体类型无效")
        # Reject invalid declarations before any object download or publication.
        try:
            facts = self._media_validator.validate(output, policy)
        except MediaValidationError as exc:
            raise ArtifactReceiptError(ProviderFailure(exc.failure_code, exc.message)) from exc

        try:
            stat = self._storage.stat(object_key)
        except (ApiError, OSError) as exc:
            raise ArtifactReceiptError(
                ProviderFailure(FailureCode.OUTPUT_MISSING, "Provider 输出对象不存在")
            ) from exc
        if stat.key != object_key:
            self._reject(FailureCode.OUTPUT_CORRUPTED, "Provider 输出对象不匹配")
        if not 0 < stat.size_bytes <= self._max_bytes:
            self._reject(FailureCode.OUTPUT_CORRUPTED, "Provider 输出对象为空或超过大小限制")

        try:
            with tempfile.TemporaryDirectory(prefix="video-maker-artifact-") as isolated:
                candidate = Path(isolated) / "candidate.mp4"
                self._download(object_key, candidate)
                claim = self._storage.write_claim(
                    f"outputs/{job_id}",
                    mime_type="video/mp4",
                    max_bytes=self._max_bytes,
                )
                if self._register_write is not None:
                    self._register_write(job_id, attempt_id, claim.object_key)
                with candidate.open("rb") as source:
                    stored = self._storage.put(claim, source, "video/mp4")
        except ArtifactReceiptError:
            raise
        except (ApiError, OSError) as exc:
            raise ArtifactReceiptError(
                ProviderFailure(FailureCode.OUTPUT_CORRUPTED, "Provider 输出接收失败")
            ) from exc

        return PublishedArtifact(stored=stored, facts=facts)

    def _download(
        self,
        object_key: str,
        destination: Path,
    ) -> None:
        received = 0
        try:
            claim = self._storage.read_claim(object_key)
            with self._storage.open(claim) as source, destination.open("xb") as target:
                while chunk := source.read(READ_CHUNK_BYTES):
                    received += len(chunk)
                    if received > self._max_bytes:
                        self._reject(
                            FailureCode.OUTPUT_CORRUPTED,
                            "Provider 输出超过大小限制",
                        )
                    target.write(chunk)
        except ArtifactReceiptError:
            raise
        except (ApiError, OSError) as exc:
            raise ArtifactReceiptError(
                ProviderFailure(FailureCode.OUTPUT_MISSING, "Provider 输出对象下载失败")
            ) from exc
        if received == 0:
            self._reject(FailureCode.OUTPUT_MISSING, "Provider 输出对象为空")

    @staticmethod
    def _reject(code: FailureCode, message: str) -> None:
        raise ArtifactReceiptError(ProviderFailure(code, message))
