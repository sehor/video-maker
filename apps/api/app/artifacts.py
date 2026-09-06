from __future__ import annotations

import tempfile
import uuid
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


class RemoteArtifactReceiver:
    """Accepts declared video metadata and copies a nonempty, bounded artifact."""

    def __init__(
        self,
        storage: ObjectStorage,
        media_validator: MediaValidator,
        *,
        max_bytes: int,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._storage = storage
        self._media_validator = media_validator
        self._max_bytes = max_bytes

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
