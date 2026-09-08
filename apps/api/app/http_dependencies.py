from typing import Annotated

import anyio
from fastapi import Depends, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.types import Receive, Scope, Send

from app.blocking_io import run_blocking
from app.bootstrap import storage_claim_ttl
from app.db import get_db
from app.generation_options import InternalGenerationOptions, get_internal_generation_options
from app.storage import READ_CHUNK_BYTES, ObjectStorage

Db = Annotated[Session, Depends(get_db)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
GenerationOptions = Annotated[InternalGenerationOptions, Depends(get_internal_generation_options)]


class StorageStreamingResponse(StreamingResponse):
    def __init__(self, source, **kwargs):
        self._source = source

        async def chunks():
            while chunk := await run_blocking(source.read, READ_CHUNK_BYTES):
                yield chunk

        super().__init__(chunks(), **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await run_blocking(self._source.close)


def storage_response(store: ObjectStorage, key: str, media_type: str) -> StreamingResponse:
    stat = store.stat(key)
    source = store.open(store.read_claim(key, expires_in=storage_claim_ttl()))

    return StorageStreamingResponse(
        source,
        media_type=media_type,
        headers={"Content-Length": str(stat.size_bytes)},
    )
