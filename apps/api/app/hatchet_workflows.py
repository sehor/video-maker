import os
from datetime import timedelta

import structlog
from hatchet_sdk import (
    Context,
    DurableContext,
    Hatchet,
    TTLBasedIdempotencyConfig,
)
from hatchet_sdk.config import ClientConfig, ClientTLSConfig

from app.config import get_settings
from app.provider_execution import GenerationExecutionService
from app.storage import LocalObjectStorage
from app.workflow import GenerationWorkflowInput, run_durable_generation

logger = structlog.get_logger()


def create_hatchet() -> Hatchet:
    settings = get_settings()
    token = os.environ.get("HATCHET_CLIENT_TOKEN", "").strip()
    if settings.hatchet_client_token_file is not None:
        try:
            token = settings.hatchet_client_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("Hatchet client token file is unavailable") from exc
    if not token:
        raise RuntimeError("Hatchet client token is not configured")

    return Hatchet(
        config=ClientConfig(
            token=token,
            host_port=settings.hatchet_client_host_port,
            server_url=settings.hatchet_server_url,
            tls_config=ClientTLSConfig(strategy="none"),
        )
    )


hatchet = create_hatchet()

generation_workflow = hatchet.workflow(
    name="GenerationWorkflow",
    version="v1",
    input_validator=GenerationWorkflowInput,
    idempotency=TTLBasedIdempotencyConfig(
        key_expression="input.idempotency_key",
        ttl=timedelta(days=36_500),
    ),
)


@hatchet.task(
    name="GenerationProviderStep",
    version="v1",
    input_validator=GenerationWorkflowInput,
)
async def generation_provider_step(
    input: GenerationWorkflowInput,
    _context: Context,
) -> dict[str, object]:
    logger.info(
        "hatchet.child_started",
        job_id=str(input.job_id),
        child_kind="provider_step",
    )
    settings = get_settings()
    store = LocalObjectStorage(
        settings.storage_root,
        settings.storage_claim_secret.get_secret_value().encode(),
    )
    result = await GenerationExecutionService(store).execute(input.job_id)
    logger.info(
        "hatchet.child_completed",
        job_id=str(input.job_id),
        child_kind="provider_step",
        is_complete=result.is_complete,
        poll_count=result.poll_count,
    )
    return {
        "is_complete": result.is_complete,
        "poll_count": result.poll_count,
        "retry_after_ms": int(result.retry_after.total_seconds() * 1000)
        if result.retry_after is not None
        else 0,
    }


@generation_workflow.durable_task(name="Orchestrate")
async def orchestrate_generation(
    input: GenerationWorkflowInput,
    context: DurableContext,
) -> dict[str, str]:
    return await run_durable_generation(input, context, generation_provider_step)


WORKFLOWS = [generation_workflow, generation_provider_step]
