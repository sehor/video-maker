import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier
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

    def put(self, key: str, content: BinaryIO, mime_type: str) -> None:
        if key in self.objects:
            raise FileExistsError(key)
        self.objects[key] = content.read()

    def open(self, key: str) -> BinaryIO:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return BytesIO(self.objects[key])

    def stat(self, key: str) -> ObjectStat | None:
        content = self.objects.get(key)
        if content is None:
            return None
        return ObjectStat(key, len(content))

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


def test_media_storage_does_not_hash_content_or_read_it_for_stat(tmp_path, monkeypatch):
    original_sha256 = hashlib.sha256

    def guarded_sha256(data=b"", *args, **kwargs):
        assert not data, "Media content must not be hashed"
        return original_sha256(data, *args, **kwargs)

    # Claim signatures may still construct an empty digest and update it via HMAC.
    monkeypatch.setattr(hashlib, "sha256", guarded_sha256)
    store = LocalObjectStorage(tmp_path / "objects", CLAIM_SECRET)
    stored = put_png(store)
    assert stored.sha256 is None

    def reject_open(*args, **kwargs):
        raise AssertionError("Stat must not read media content")

    monkeypatch.setattr(Path, "open", reject_open)
    stat = store.stat(stored.key)
    assert stat.size_bytes == len(PNG)
    assert stat.sha256 is None
    assert store.read_claim(stored.key)


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


def assert_object_content(store: ObjectStorage, key: str, expected: bytes) -> None:
    assert store.stat(key).size_bytes == len(expected)
    with store.open(store.read_claim(key)) as source:
        assert source.read() == expected


def test_duplicate_write_preserves_committed_object(storage_factory: StorageFactory) -> None:
    store = storage_factory(MutableClock())
    claim = store.write_claim("assets", mime_type="image/png", max_bytes=100)
    store.put(claim, PNG, "image/png")

    for replacement in (b"different content", PNG):
        with pytest.raises(ApiError) as raised:
            store.put(claim, replacement, "image/png")
        assert (raised.value.status_code, raised.value.code) == (409, "STORAGE_OBJECT_EXISTS")
        assert_object_content(store, claim.object_key, PNG)


@pytest.mark.parametrize("existing", [False, True])
def test_stream_read_failure_preserves_storage(
    storage_factory: StorageFactory,
    existing: bool,
) -> None:
    store = storage_factory(MutableClock())
    claim = store.write_claim("assets", mime_type="image/png", max_bytes=100)
    if existing:
        store.put(claim, PNG, "image/png")

    class FailingStream(BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise OSError("injected stream read failure")
            return super().read(4)

    with pytest.raises(OSError, match="injected stream read failure"):
        store.put(claim, FailingStream(PNG), "image/png")

    if existing:
        assert_object_content(store, claim.object_key, PNG)
    else:
        assert_error_code("STORAGE_OBJECT_NOT_FOUND", lambda: store.stat(claim.object_key))


def test_local_concurrent_writers_preserve_winner(tmp_path: Path, monkeypatch) -> None:
    stores = [LocalObjectStorage(tmp_path / "objects", CLAIM_SECRET) for _ in range(2)]
    claim = stores[0].write_claim("assets", mime_type="image/png", max_bytes=100)
    (stores[0].root / "assets").mkdir()
    original_open = Path.open
    barrier = Barrier(2)

    def concurrent_open(path, mode="r", *args, **kwargs):
        if mode == "xb":
            barrier.wait(timeout=10)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", concurrent_open)
    contents = [PNG, PNG + b"second writer"]

    def write(index):
        try:
            stores[index].put(claim, contents[index], "image/png")
        except Exception as exc:
            return exc
        return index

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, range(2)))
    winners = [index for index in results if isinstance(index, int)]
    assert len(winners) == 1, results
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(failures) == 1 and isinstance(failures[0], ApiError), results
    assert (failures[0].status_code, failures[0].code) == (409, "STORAGE_OBJECT_EXISTS")
    assert_object_content(stores[0], claim.object_key, contents[winners[0]])


def test_local_open_failure_preserves_committed_object(tmp_path: Path, monkeypatch) -> None:
    store = LocalObjectStorage(tmp_path / "objects", CLAIM_SECRET)
    claim = store.write_claim("assets", mime_type="image/png", max_bytes=100)
    store.put(claim, PNG, "image/png")
    original_open = Path.open

    def denied_open(path, mode="r", *args, **kwargs):
        if mode == "xb":
            raise PermissionError("injected access denial")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    with pytest.raises(PermissionError, match="injected access denial"):
        store.put(claim, PNG + b"replacement", "image/png")
    assert_object_content(store, claim.object_key, PNG)


@pytest.mark.parametrize("failure", ["create", "write", "close"])
def test_local_disk_failure_cleans_only_new_object(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    store = LocalObjectStorage(tmp_path / "objects", CLAIM_SECRET)
    committed = put_png(store)
    claim = store.write_claim("assets", mime_type="image/png", max_bytes=100)
    original_open = Path.open

    @contextmanager
    def failing_target(path, *args, **kwargs):
        if failure == "create":
            raise OSError("injected disk failure")
        with original_open(path, "xb", *args, **kwargs) as target:
            if failure == "write":

                class FailingWriter:
                    def write(self, content):
                        target.write(content[:4])
                        target.flush()
                        raise OSError("injected disk failure")

                yield FailingWriter()
            else:
                yield target
        if failure == "close":
            raise OSError("injected disk failure")

    def failing_open(path, mode="r", *args, **kwargs):
        if mode == "xb":
            return failing_target(path, *args, **kwargs)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OSError, match="injected disk failure"):
        store.put(claim, PNG, "image/png")
    assert_error_code("STORAGE_OBJECT_NOT_FOUND", lambda: store.stat(claim.object_key))
    assert_object_content(store, committed.key, PNG)
    assert list((tmp_path / "objects").rglob("*.png")) == [store.root / committed.key]


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
