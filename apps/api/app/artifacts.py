from __future__ import annotations

import hashlib
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.errors import ApiError
from app.media import MediaFacts, MediaPolicy, MediaValidationError, MediaValidator
from app.provider import FailureCode, ProviderFailure, ProviderOutput
from app.storage import ObjectStorage, StoredObject

READ_CHUNK_BYTES = 1024 * 1024
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class ArtifactReceiptError(Exception):
    def __init__(self, failure: ProviderFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    stored: StoredObject
    facts: MediaFacts


class RemoteArtifactReceiver:
    """Downloads, verifies, and publishes one provider-owned video artifact."""

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
        size_bytes = output.size_bytes
        sha256 = output.sha256
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
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes <= 0
            or size_bytes > self._max_bytes
        ):
            self._reject(FailureCode.OUTPUT_CORRUPTED, "Provider 输出大小声明无效")
        if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
            self._reject(FailureCode.OUTPUT_CORRUPTED, "Provider 输出 SHA-256 声明无效")

        try:
            stat = self._storage.stat(object_key)
        except (ApiError, OSError) as exc:
            raise ArtifactReceiptError(
                ProviderFailure(FailureCode.OUTPUT_MISSING, "Provider 输出对象不存在")
            ) from exc
        if (
            stat.key != object_key
            or stat.size_bytes != size_bytes
            or stat.sha256 != sha256
        ):
            self._reject(FailureCode.OUTPUT_CORRUPTED, "Provider 输出对象元数据不匹配")

        try:
            with tempfile.TemporaryDirectory(prefix="video-maker-artifact-") as isolated:
                candidate = Path(isolated) / "candidate.mp4"
                self._download_verified(object_key, candidate, size_bytes, sha256)
                facts = self._media_validator.validate(candidate, policy)
                claim = self._storage.write_claim(
                    f"outputs/{job_id}",
                    mime_type="video/mp4",
                    max_bytes=size_bytes,
                )
                with candidate.open("rb") as source:
                    stored = self._storage.put(claim, source, "video/mp4")
        except ArtifactReceiptError:
            raise
        except MediaValidationError as exc:
            raise ArtifactReceiptError(
                ProviderFailure(exc.failure_code, exc.message)
            ) from exc
        except (ApiError, OSError) as exc:
            raise ArtifactReceiptError(
                ProviderFailure(FailureCode.OUTPUT_CORRUPTED, "Provider 输出接收失败")
            ) from exc

        if stored.size_bytes != size_bytes or stored.sha256 != sha256:
            self._storage.delete(stored.key)
            self._reject(FailureCode.OUTPUT_CORRUPTED, "最终 Output 完整性校验失败")
        return PublishedArtifact(stored=stored, facts=facts)

    def _download_verified(
        self,
        object_key: str,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        digest = hashlib.sha256()
        received = 0
        try:
            claim = self._storage.read_claim(object_key)
            with self._storage.open(claim) as source, destination.open("xb") as target:
                while chunk := source.read(READ_CHUNK_BYTES):
                    received += len(chunk)
                    if received > expected_size or received > self._max_bytes:
                        self._reject(
                            FailureCode.OUTPUT_CORRUPTED,
                            "Provider 输出实际大小超过声明",
                        )
                    digest.update(chunk)
                    target.write(chunk)
        except ArtifactReceiptError:
            raise
        except (ApiError, OSError) as exc:
            raise ArtifactReceiptError(
                ProviderFailure(FailureCode.OUTPUT_MISSING, "Provider 输出对象下载失败")
            ) from exc
        if received != expected_size or digest.hexdigest() != expected_sha256:
            self._reject(FailureCode.OUTPUT_CORRUPTED, "Provider 输出大小或 SHA-256 不匹配")

    @staticmethod
    def _reject(code: FailureCode, message: str) -> None:
        raise ArtifactReceiptError(ProviderFailure(code, message))
