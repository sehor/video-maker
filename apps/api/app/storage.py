import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from fastapi import UploadFile

from app.errors import ApiError

ALLOWED_TYPES = {
    "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
    "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
    "image/webp": (b"RIFF", ".webp"),
    "video/mp4": (b"", ".mp4"),
}


@dataclass(frozen=True)
class StoredObject:
    key: str
    mime_type: str
    size_bytes: int
    sha256: str


class ObjectStorage(Protocol):
    async def save_upload(
        self, namespace: str, upload: UploadFile, max_bytes: int
    ) -> StoredObject: ...

    def write_bytes(self, namespace: str, content: bytes, mime_type: str) -> StoredObject: ...

    def path_for(self, key: str) -> Path: ...

    def delete(self, key: str) -> None: ...


class LocalObjectStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _allocate(self, namespace: str, suffix: str) -> tuple[str, Path]:
        safe_namespace = str(PurePosixPath(namespace))
        if safe_namespace.startswith("/") or ".." in PurePosixPath(safe_namespace).parts:
            raise ValueError("invalid namespace")
        key = f"{safe_namespace}/{uuid.uuid4().hex}{suffix}"
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("storage key escapes root")
        path.parent.mkdir(parents=True, exist_ok=True)
        return key, path

    def path_for(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ApiError(400, "STORAGE_KEY_INVALID", "无效的存储对象")
        return path

    async def save_upload(self, namespace: str, upload: UploadFile, max_bytes: int) -> StoredObject:
        mime_type = upload.content_type or "application/octet-stream"
        if mime_type not in ALLOWED_TYPES:
            raise ApiError(415, "UPLOAD_TYPE_NOT_ALLOWED", "仅支持 JPEG、PNG、WebP 和 MP4")
        expected_magic, suffix = ALLOWED_TYPES[mime_type]
        key, path = self._allocate(namespace, suffix)
        digest = hashlib.sha256()
        size = 0
        first = b""
        try:
            with path.open("xb") as target:
                while chunk := await upload.read(1024 * 1024):
                    if not first:
                        first = chunk[:16]
                    size += len(chunk)
                    if size > max_bytes:
                        raise ApiError(413, "UPLOAD_TOO_LARGE", "上传文件超过大小限制")
                    digest.update(chunk)
                    target.write(chunk)
            self._validate_header(mime_type, expected_magic, first)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return StoredObject(key, mime_type, size, digest.hexdigest())

    @staticmethod
    def _validate_header(mime_type: str, expected_magic: bytes, first: bytes) -> None:
        if mime_type == "video/mp4":
            if len(first) < 12 or first[4:8] != b"ftyp":
                raise ApiError(422, "UPLOAD_CONTENT_INVALID", "文件内容与 MP4 类型不匹配")
        elif mime_type == "image/webp":
            if not (first.startswith(b"RIFF") and first[8:12] == b"WEBP"):
                raise ApiError(422, "UPLOAD_CONTENT_INVALID", "文件内容与 WebP 类型不匹配")
        elif not first.startswith(expected_magic):
            raise ApiError(422, "UPLOAD_CONTENT_INVALID", "文件内容与声明类型不匹配")

    def write_bytes(self, namespace: str, content: bytes, mime_type: str) -> StoredObject:
        suffix = ALLOWED_TYPES.get(mime_type, (b"", ".bin"))[1]
        key, path = self._allocate(namespace, suffix)
        with path.open("xb") as target:
            target.write(content)
        return StoredObject(key, mime_type, len(content), hashlib.sha256(content).hexdigest())

    def delete(self, key: str) -> None:
        try:
            self.path_for(key).unlink(missing_ok=True)
        except OSError:
            pass
