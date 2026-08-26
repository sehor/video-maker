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
from app.provider import MockVideoProvider
from app.storage import LocalObjectStorage
from app.workflow import GenerationWorkflowInput

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
    name="GenerationSubmit",
    version="v1",
    input_validator=GenerationWorkflowInput,
)
async def generation_submit(
    input: GenerationWorkflowInput,
    _context: Context,
) -> dict[str, str]:
    logger.info(
        "hatchet.child_started",
        job_id=str(input.job_id),
        child_key=f"job:{input.job_id}:submit:v1",
    )
    settings = get_settings()
    await MockVideoProvider(LocalObjectStorage(settings.storage_root)).submit(input.job_id)
    logger.info(
        "hatchet.child_completed",
        job_id=str(input.job_id),
        child_key=f"job:{input.job_id}:submit:v1",
    )
    return {"job_id": str(input.job_id)}


@generation_workflow.durable_task(name="Orchestrate")
async def orchestrate_generation(
    input: GenerationWorkflowInput,
    _context: DurableContext,
) -> dict[str, str]:
    await generation_submit.aio_run(
        input,
        wait_for_result=True,
        child_key=f"job:{input.job_id}:submit:v1",
    )
    return {"job_id": str(input.job_id)}


WORKFLOWS = [generation_workflow, generation_submit]
