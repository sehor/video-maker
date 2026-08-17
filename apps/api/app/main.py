import logging
import time
import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import router
from app.config import get_settings
from app.errors import ApiError

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

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url="/docs" if settings.environment != "production" else None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
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
