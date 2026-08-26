import base64
import binascii
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from app.errors import ApiError

ALLOWED_TYPES = {
    "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
    "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
    "image/webp": (b"RIFF", ".webp"),
    "video/mp4": (b"", ".mp4"),
}
MAX_CLAIM_TTL = timedelta(minutes=15)
READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    mime_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ObjectStat:
    key: str
    size_bytes: int
    sha256: str


class ClaimOperation(StrEnum):
    READ = "READ"
    WRITE = "WRITE"


@dataclass(frozen=True, slots=True)
class StorageClaim:
    token: str
    object_key: str
    operation: ClaimOperation
    expires_at: datetime


class ObjectStorage(Protocol):
    def put(
        self,
        claim: StorageClaim,
        content: bytes | BinaryIO,
        mime_type: str,
    ) -> StoredObject: ...

    def open(self, claim: StorageClaim) -> BinaryIO: ...

    def stat(self, key: str) -> ObjectStat: ...

    def delete(self, key: str) -> None: ...

    def read_claim(
        self,
        key: str,
        *,
        expires_in: timedelta = timedelta(minutes=5),
    ) -> StorageClaim: ...

    def write_claim(
        self,
        namespace: str,
        *,
        mime_type: str,
        max_bytes: int,
        expires_in: timedelta = timedelta(minutes=5),
    ) -> StorageClaim: ...


class RemoteObjectBackend(Protocol):
    """Minimal private-object client surface required by the remote adapter."""

    def put(self, key: str, content: bytes, mime_type: str) -> None: ...

    def open(self, key: str) -> BinaryIO: ...

    def stat(self, key: str) -> ObjectStat | None: ...

    def delete(self, key: str) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if _b64encode(decoded) != value:
        raise binascii.Error("non-canonical base64")
    return decoded


def _valid_object_key(value: str) -> str:
    if not value or "\\" in value or ":" in value:
        raise ApiError(400, "STORAGE_KEY_INVALID", "无效的存储对象")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ApiError(400, "STORAGE_KEY_INVALID", "无效的存储对象")
    if any(part in {"", "."} for part in path.parts):
        raise ApiError(400, "STORAGE_KEY_INVALID", "无效的存储对象")
    return value


def _valid_namespace(value: str) -> str:
    _valid_object_key(f"{value}/placeholder")
    if PurePosixPath(value).as_posix() != value:
        raise ApiError(400, "STORAGE_KEY_INVALID", "无效的存储命名空间")
    return value


def validate_media_header(mime_type: str, first: bytes) -> None:
    if mime_type not in ALLOWED_TYPES:
        raise ApiError(415, "UPLOAD_TYPE_NOT_ALLOWED", "仅支持 JPEG、PNG、WebP 和 MP4")
    expected_magic = ALLOWED_TYPES[mime_type][0]
    if mime_type == "video/mp4":
        if len(first) < 12 or first[4:8] != b"ftyp":
            raise ApiError(422, "UPLOAD_CONTENT_INVALID", "文件内容与 MP4 类型不匹配")
    elif mime_type == "image/webp":
        if not (first.startswith(b"RIFF") and first[8:12] == b"WEBP"):
            raise ApiError(422, "UPLOAD_CONTENT_INVALID", "文件内容与 WebP 类型不匹配")
    elif not first.startswith(expected_magic):
        raise ApiError(422, "UPLOAD_CONTENT_INVALID", "文件内容与声明类型不匹配")


class _ClaimCodec:
    def __init__(self, secret: bytes, clock: Callable[[], datetime]) -> None:
        if len(secret) < 32:
            raise ValueError("storage claim secret must contain at least 32 bytes")
        self._secret = secret
        self._clock = clock

    def issue(
        self,
        operation: ClaimOperation,
        key: str,
        expires_in: timedelta,
        *,
        mime_type: str | None = None,
        max_bytes: int | None = None,
    ) -> StorageClaim:
        if expires_in <= timedelta(0) or expires_in > MAX_CLAIM_TTL:
            raise ValueError("storage claim TTL must be between 1 second and 15 minutes")
        now = self._clock().astimezone(UTC)
        expires_at = now + expires_in
        payload: dict[str, str | int] = {
            "v": 1,
            "op": operation.value,
            "key": _valid_object_key(key),
            "exp": int(expires_at.timestamp()),
        }
        if mime_type is not None:
            payload["mime_type"] = mime_type
        if max_bytes is not None:
            payload["max_bytes"] = max_bytes
        encoded = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = _b64encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        return StorageClaim(
            token=f"{encoded}.{signature}",
            object_key=key,
            operation=operation,
            expires_at=expires_at,
        )

    def verify(self, claim: StorageClaim, operation: ClaimOperation) -> dict[str, str | int]:
        try:
            encoded, supplied_signature = claim.token.split(".", 1)
            expected_signature = hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(_b64decode(supplied_signature), expected_signature):
                raise ValueError("invalid signature")
            payload = json.loads(_b64decode(encoded))
            payload_operation = ClaimOperation(payload["op"])
            key = _valid_object_key(payload["key"])
            expires_at = datetime.fromtimestamp(payload["exp"], UTC)
            if payload.get("v") != 1:
                raise ValueError("unsupported claim version")
        except (
            ApiError,
            binascii.Error,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ApiError(403, "STORAGE_CLAIM_INVALID", "存储访问声明无效") from exc
        if expires_at <= self._clock().astimezone(UTC):
            raise ApiError(403, "STORAGE_CLAIM_EXPIRED", "存储访问声明已过期")
        if payload_operation != operation:
            raise ApiError(403, "STORAGE_CLAIM_FORBIDDEN", "存储访问声明权限不足")
        if (
            claim.object_key != key
            or claim.operation != payload_operation
            or int(claim.expires_at.timestamp()) != int(expires_at.timestamp())
        ):
            raise ApiError(403, "STORAGE_CLAIM_INVALID", "存储访问声明无效")
        return payload


class ClaimingObjectStorage:
    def __init__(
        self,
        claim_secret: bytes,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._claims = _ClaimCodec(claim_secret, clock)

    def put(
        self,
        claim: StorageClaim,
        content: bytes | BinaryIO,
        mime_type: str,
    ) -> StoredObject:
        payload = self._claims.verify(claim, ClaimOperation.WRITE)
        claimed_mime_type = payload.get("mime_type")
        max_bytes = payload.get("max_bytes")
        if claimed_mime_type != mime_type or not isinstance(max_bytes, int):
            raise ApiError(403, "STORAGE_CLAIM_FORBIDDEN", "存储写入声明与内容不匹配")
        body = self._read_content(content, max_bytes)
        digest = hashlib.sha256(body).hexdigest()
        try:
            self._put_bytes(claim.object_key, body, mime_type)
        except FileExistsError as exc:
            raise ApiError(409, "STORAGE_OBJECT_EXISTS", "存储对象已存在") from exc
        return StoredObject(claim.object_key, mime_type, len(body), digest)

    def open(self, claim: StorageClaim) -> BinaryIO:
        self._claims.verify(claim, ClaimOperation.READ)
        try:
            return self._open_object(claim.object_key)
        except FileNotFoundError as exc:
            raise ApiError(404, "STORAGE_OBJECT_NOT_FOUND", "存储对象不存在") from exc

    def stat(self, key: str) -> ObjectStat:
        key = _valid_object_key(key)
        result = self._stat_object(key)
        if result is None:
            raise ApiError(404, "STORAGE_OBJECT_NOT_FOUND", "存储对象不存在")
        return result

    def delete(self, key: str) -> None:
        self._delete_object(_valid_object_key(key))

    def read_claim(
        self,
        key: str,
        *,
        expires_in: timedelta = timedelta(minutes=5),
    ) -> StorageClaim:
        self.stat(key)
        return self._claims.issue(ClaimOperation.READ, key, expires_in)

    def write_claim(
        self,
        namespace: str,
        *,
        mime_type: str,
        max_bytes: int,
        expires_in: timedelta = timedelta(minutes=5),
    ) -> StorageClaim:
        if mime_type not in ALLOWED_TYPES:
            raise ApiError(415, "UPLOAD_TYPE_NOT_ALLOWED", "仅支持 JPEG、PNG、WebP 和 MP4")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        suffix = ALLOWED_TYPES[mime_type][1]
        key = f"{_valid_namespace(namespace)}/{uuid.uuid4().hex}{suffix}"
        return self._claims.issue(
            ClaimOperation.WRITE,
            key,
            expires_in,
            mime_type=mime_type,
            max_bytes=max_bytes,
        )

    @staticmethod
    def _read_content(content: bytes | BinaryIO, max_bytes: int) -> bytes:
        if isinstance(content, bytes):
            if len(content) > max_bytes:
                raise ApiError(413, "UPLOAD_TOO_LARGE", "上传文件超过大小限制")
            return content
        chunks: list[bytes] = []
        size = 0
        while chunk := content.read(READ_CHUNK_BYTES):
            size += len(chunk)
            if size > max_bytes:
                raise ApiError(413, "UPLOAD_TOO_LARGE", "上传文件超过大小限制")
            chunks.append(chunk)
        return b"".join(chunks)

    def _put_bytes(self, key: str, content: bytes, mime_type: str) -> None:
        raise NotImplementedError

    def _open_object(self, key: str) -> BinaryIO:
        raise NotImplementedError

    def _stat_object(self, key: str) -> ObjectStat | None:
        raise NotImplementedError

    def _delete_object(self, key: str) -> None:
        raise NotImplementedError


class LocalObjectStorage(ClaimingObjectStorage):
    def __init__(
        self,
        root: Path,
        claim_secret: bytes,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(claim_secret, clock=clock)
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        path = (self.root / _valid_object_key(key)).resolve()
        if self.root not in path.parents:
            raise ApiError(400, "STORAGE_KEY_INVALID", "无效的存储对象")
        return path

    def _put_bytes(self, key: str, content: bytes, mime_type: str) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as target:
                target.write(content)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def _open_object(self, key: str) -> BinaryIO:
        return self._path_for(key).open("rb")

    def _stat_object(self, key: str) -> ObjectStat | None:
        path = self._path_for(key)
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(READ_CHUNK_BYTES):
                digest.update(chunk)
        return ObjectStat(key, path.stat().st_size, digest.hexdigest())

    def _delete_object(self, key: str) -> None:
        self._path_for(key).unlink(missing_ok=True)


class RemoteObjectStorage(ClaimingObjectStorage):
    """Adapter for a private remote object client; provider-specific SDKs stay behind it."""

    def __init__(
        self,
        backend: RemoteObjectBackend,
        claim_secret: bytes,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(claim_secret, clock=clock)
        self._backend = backend

    def _put_bytes(self, key: str, content: bytes, mime_type: str) -> None:
        self._backend.put(key, content, mime_type)

    def _open_object(self, key: str) -> BinaryIO:
        return self._backend.open(key)

    def _stat_object(self, key: str) -> ObjectStat | None:
        return self._backend.stat(key)

    def _delete_object(self, key: str) -> None:
        self._backend.delete(key)
