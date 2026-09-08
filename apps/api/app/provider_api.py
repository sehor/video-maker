from fastapi import APIRouter, Request

from app.blocking_io import run_blocking
from app.bootstrap import provider_executor
from app.config import get_settings
from app.errors import ApiError
from app.provider import WebhookVerificationError, WebhookVerificationRequest
from app.schemas import ProviderWebhookAck

router = APIRouter(prefix="/v1")


@router.post(
    "/provider-webhooks/{provider_code}",
    response_model=ProviderWebhookAck,
    status_code=202,
    include_in_schema=False,
)
async def receive_provider_webhook(
    provider_code: str,
    request: Request,
) -> ProviderWebhookAck:
    max_bytes = get_settings().provider_webhook_max_bytes
    parts: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_bytes:
            raise ApiError(413, "PROVIDER_WEBHOOK_TOO_LARGE", "Provider webhook 请求过大")
        parts.append(chunk)
    body = b"".join(parts)

    executor = await run_blocking(provider_executor, provider_code, require_webhook_secret=True)
    try:
        result = await executor.handle_webhook(
            provider_code,
            WebhookVerificationRequest(headers=request.headers, body=body),
        )
    except WebhookVerificationError as exc:
        raise ApiError(
            401,
            "PROVIDER_WEBHOOK_INVALID",
            "Provider webhook 验证失败",
        ) from exc
    return ProviderWebhookAck(event_id=result.event_id, status=result.status)
