import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import dispatch_generation_outbox, dispatch_provider_cancel_outbox, router
from app.config import get_settings
from app.errors import ApiError
from app.outbox import DispatchResult

logging.basicConfig(level=logging.INFO, format="%(message)s")
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger()
settings = get_settings()


async def outbox_dispatcher_loop(stop: asyncio.Event) -> None:
    logger.info("outbox.dispatcher_started")
    while not stop.is_set():
        try:
            result = await dispatch_generation_outbox()
        except Exception as exc:
            logger.exception("outbox.dispatcher_failed", error_type=type(exc).__name__)
            result = None
        if result == DispatchResult.PUBLISHED:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.outbox_poll_interval_seconds)
        except TimeoutError:
            pass
    logger.info("outbox.dispatcher_stopped")


async def provider_cancel_dispatcher_loop(stop: asyncio.Event) -> None:
    logger.info("provider_cancel_outbox.dispatcher_started")
    while not stop.is_set():
        try:
            result = await dispatch_provider_cancel_outbox()
        except Exception as exc:
            logger.exception(
                "provider_cancel_outbox.dispatcher_failed",
                error_type=type(exc).__name__,
            )
            result = None
        if result == DispatchResult.PUBLISHED:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.outbox_poll_interval_seconds)
        except TimeoutError:
            pass
    logger.info("provider_cancel_outbox.dispatcher_stopped")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    stop = asyncio.Event()
    tasks = (
        [
            asyncio.create_task(outbox_dispatcher_loop(stop)),
            asyncio.create_task(provider_cancel_dispatcher_loop(stop)),
        ]
        if settings.outbox_dispatcher_enabled
        else []
    )
    try:
        yield
    finally:
        if tasks:
            stop.set()
            await asyncio.gather(*tasks)

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url="/docs" if settings.environment != "production" else None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request.failed", request_id=request_id, path=request.url.path)
        raise
    response.headers["x-request-id"] = request_id
    logger.info(
        "request.completed",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    return response


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    body = {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "request_id": getattr(request.state, "request_id", "unknown"),
        }
    }
    if exc.details is not None:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_FAILED",
                "message": "请求参数无效",
                "request_id": getattr(request.state, "request_id", "unknown"),
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(router)
