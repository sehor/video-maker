from typing import Annotated

from fastapi import Depends, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.bootstrap import storage_claim_ttl
from app.db import get_db
from app.generation_options import InternalGenerationOptions, get_internal_generation_options
from app.storage import ObjectStorage

Db = Annotated[Session, Depends(get_db)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
GenerationOptions = Annotated[InternalGenerationOptions, Depends(get_internal_generation_options)]


def storage_response(store: ObjectStorage, key: str, media_type: str) -> StreamingResponse:
    stat = store.stat(key)
    source = store.open(store.read_claim(key, expires_in=storage_claim_ttl()))
    return StreamingResponse(
        source,
        media_type=media_type,
        headers={"Content-Length": str(stat.size_bytes)},
        background=BackgroundTask(source.close),
    )
