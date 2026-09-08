import asyncio
from io import BytesIO

import pytest
from starlette.requests import ClientDisconnect

from app.http_dependencies import StorageStreamingResponse
from app.storage import READ_CHUNK_BYTES, LocalObjectStorage, RemoteObjectStorage
from tests.test_storage_contract import CLAIM_SECRET


class LazyStream:
    def __init__(self, size):
        self.remaining = size
        self.reads = []

    def read(self, count):
        assert 0 < count <= READ_CHUNK_BYTES
        self.reads.append(count)
        count = min(count, self.remaining)
        self.remaining -= count
        return b"x" * count


@pytest.mark.parametrize("fail", [False, True])
def test_remote_backend_gets_seekable_stream_closed_after_return_or_failure(fail):
    streams = []

    class Backend:
        def put(self, key, content, mime_type):
            streams.append(content)
            assert content.seekable()
            assert content.read(8) == b"x" * 8
            content.seek(0)
            if fail:
                raise OSError("remote failure")
            size = 0
            while chunk := content.read(READ_CHUNK_BYTES):
                assert chunk == b"x" * len(chunk)
                size += len(chunk)
            assert size == 2 * READ_CHUNK_BYTES + 7

    storage = RemoteObjectStorage(Backend(), CLAIM_SECRET)
    size = 2 * READ_CHUNK_BYTES + 7
    source = LazyStream(size)
    claim = storage.write_claim("outputs", mime_type="video/mp4", max_bytes=size)
    if fail:
        with pytest.raises(OSError, match="remote failure"):
            storage.put(claim, source, "video/mp4")
    else:
        assert storage.put(claim, source, "video/mp4").size_bytes == size
    assert source.remaining == 0
    assert streams[0].closed


def test_oversize_stream_stops_at_limit_plus_one_without_creating_destination(tmp_path):
    from app.errors import ApiError

    storage = LocalObjectStorage(tmp_path, CLAIM_SECRET)
    source = LazyStream(100 * READ_CHUNK_BYTES)
    claim = storage.write_claim("outputs", mime_type="video/mp4", max_bytes=17)
    with pytest.raises(ApiError) as failure:
        storage.put(claim, source, "video/mp4")
    assert failure.value.code == "UPLOAD_TOO_LARGE"
    assert source.reads == [18]
    assert not storage._path_for(claim.object_key).exists()


@pytest.mark.parametrize("disconnect", [False, True])
def test_download_chunks_and_closes_on_disconnect(disconnect):
    source = BytesIO(b"x" * (2 * READ_CHUNK_BYTES + 5))
    body_sizes = []

    async def send(message):
        if message["type"] == "http.response.body":
            if disconnect:
                raise OSError("client disconnected")
            body_sizes.append(len(message.get("body", b"")))

    async def receive():
        return {"type": "http.disconnect"}

    async def scenario():
        response = StorageStreamingResponse(source, media_type="video/mp4")
        scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
        if disconnect:
            with pytest.raises(ClientDisconnect):
                await response(scope, receive, send)
        else:
            await response(scope, receive, send)

    asyncio.run(scenario())
    assert source.closed
    if not disconnect:
        assert max(body_sizes) == READ_CHUNK_BYTES
        assert sum(body_sizes) == 2 * READ_CHUNK_BYTES + 5
