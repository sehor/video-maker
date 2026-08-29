import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pytest

from app.errors import ApiError
from app.simulators import FakeRemoteStorage
from app.storage import (
    ClaimOperation,
    LocalObjectStorage,
    ObjectStat,
    ObjectStorage,
    RemoteObjectStorage,
)

CLAIM_SECRET = b"contract-test-storage-claim-secret-32-bytes"
PNG = b"\x89PNG\r\n\x1a\ncontract-content"


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 26, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeRemoteBackend:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, content: bytes, mime_type: str) -> None:
        if key in self.objects:
            raise FileExistsError(key)
        self.objects[key] = content

    def open(self, key: str) -> BinaryIO:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return BytesIO(self.objects[key])

    def stat(self, key: str) -> ObjectStat | None:
        content = self.objects.get(key)
        if content is None:
            return None
        return ObjectStat(key, len(content), hashlib.sha256(content).hexdigest())

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


StorageFactory = Callable[[MutableClock], ObjectStorage]


@pytest.fixture(params=["local", "remote", "fake-remote"])
def storage_factory(request: pytest.FixtureRequest, tmp_path: Path) -> StorageFactory:
    if request.param == "local":
        return lambda clock: LocalObjectStorage(tmp_path / "objects", CLAIM_SECRET, clock=clock)
    if request.param == "fake-remote":
        return lambda clock: FakeRemoteStorage(CLAIM_SECRET, clock=clock)
    return lambda clock: RemoteObjectStorage(FakeRemoteBackend(), CLAIM_SECRET, clock=clock)


def put_png(store: ObjectStorage, *, namespace: str = "assets"):
    claim = store.write_claim(namespace, mime_type="image/png", max_bytes=len(PNG))
    return store.put(claim, PNG, "image/png")


def assert_error_code(code: str, operation: Callable[[], object]) -> None:
    with pytest.raises(ApiError) as raised:
        operation()
    assert raised.value.code == code


def test_local_and_remote_follow_put_open_stat_delete_contract(
    storage_factory: StorageFactory,
) -> None:
    store = storage_factory(MutableClock())
    stored = put_png(store)

    stat = store.stat(stored.key)
    assert (stat.size_bytes, stat.sha256) == (stored.size_bytes, stored.sha256)
    with store.open(store.read_claim(stored.key)) as source:
        assert source.read() == PNG

    store.delete(stored.key)
    store.delete(stored.key)
    assert_error_code("STORAGE_OBJECT_NOT_FOUND", lambda: store.stat(stored.key))


def test_claims_are_bound_to_operation_object_and_limits(
    storage_factory: StorageFactory,
) -> None:
    store = storage_factory(MutableClock())
    stored = put_png(store)
    read_claim = store.read_claim(stored.key)
    write_claim = store.write_claim("outputs", mime_type="video/mp4", max_bytes=8)

    assert read_claim.operation == ClaimOperation.READ
    assert write_claim.operation == ClaimOperation.WRITE
    assert_error_code(
        "STORAGE_CLAIM_FORBIDDEN",
        lambda: store.put(read_claim, PNG, "image/png"),
    )
    assert_error_code("STORAGE_CLAIM_FORBIDDEN", lambda: store.open(write_claim))
    assert_error_code(
        "STORAGE_CLAIM_INVALID",
        lambda: store.open(replace(read_claim, object_key="assets/other.png")),
    )
    assert_error_code(
        "UPLOAD_TOO_LARGE",
        lambda: store.put(write_claim, b"123456789", "video/mp4"),
    )


def test_expired_and_tampered_claims_are_rejected(
    storage_factory: StorageFactory,
) -> None:
    clock = MutableClock()
    store = storage_factory(clock)
    stored = put_png(store)
    claim = store.read_claim(stored.key, expires_in=timedelta(seconds=2))

    clock.now += timedelta(seconds=3)
    assert_error_code("STORAGE_CLAIM_EXPIRED", lambda: store.open(claim))
    tampered = replace(claim, token=f"{claim.token[:-1]}x")
    assert_error_code("STORAGE_CLAIM_INVALID", lambda: store.open(tampered))


@pytest.mark.parametrize(
    "invalid_key",
    ["../escape", "/absolute", "assets/../escape", "assets\\escape", "C:/escape"],
)
def test_path_traversal_is_rejected(
    storage_factory: StorageFactory,
    invalid_key: str,
) -> None:
    store = storage_factory(MutableClock())
    assert_error_code("STORAGE_KEY_INVALID", lambda: store.read_claim(invalid_key))
    assert_error_code("STORAGE_KEY_INVALID", lambda: store.stat(invalid_key))
    assert_error_code(
        "STORAGE_KEY_INVALID",
        lambda: store.write_claim(invalid_key, mime_type="image/png", max_bytes=100),
    )
