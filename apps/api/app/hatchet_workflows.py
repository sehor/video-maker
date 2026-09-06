from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

from app.config import Settings
from app.workflow import (
    GenerationWorkflowInput,
    HatchetGenerationStepRunnable,
    HatchetWorkflowRunnable,
    run_durable_generation,
)

if TYPE_CHECKING:
    from hatchet_sdk import Hatchet

logger = structlog.get_logger()


def create_hatchet(settings: Settings) -> "Hatchet":
    from hatchet_sdk import Hatchet
    from hatchet_sdk.config import ClientConfig, ClientTLSConfig
    from hatchet_sdk.token import get_addresses_from_jwt

    try:
        token = settings.get_hatchet_token()
        token_server, token_host = get_addresses_from_jwt(token)
        host_port = settings.hatchet_client_host_port or token_host
        server_url = settings.hatchet_server_url or token_server
        settings.validate_hatchet_addresses(host_port, server_url)
        return Hatchet(
            config=ClientConfig(
                _env_file=None,
                token=token,
                host_port=host_port,
                server_url=server_url,
                namespace=settings.hatchet_client_namespace,
                debug=False,
                tls_config=ClientTLSConfig(
                    _env_file=None,
                    strategy=settings.hatchet_client_tls_strategy,
                    server_name=settings.hatchet_client_tls_server_name,
                    root_ca_file=str(settings.hatchet_client_tls_root_ca_file)
                    if settings.hatchet_client_tls_root_ca_file is not None else None,
                    cert_file=None,
                    key_file=None,
                ),
            )
        )
    except (ValueError, OSError):
        # SDK validation errors may include the raw token in the input/traceback.
        raise RuntimeError(
            "Hatchet client configuration is invalid; check HATCHET_CLIENT_TOKEN, "
            "HATCHET_CLIENT_TOKEN_FILE, server addresses and TLS settings"
        ) from None


@dataclass(frozen=True)
class HatchetWorkflows:
    hatchet: "Hatchet"
    generation_workflow: HatchetWorkflowRunnable
    generation_provider_step: HatchetGenerationStepRunnable


def create_hatchet_workflows(settings: Settings) -> HatchetWorkflows:
    """Register workflows only when the Hatchet backend or worker is explicitly used."""
    from hatchet_sdk import Context, DurableContext, TTLBasedIdempotencyConfig

    from app.provider_execution import GenerationExecutionService
    from app.storage import LocalObjectStorage

    hatchet = create_hatchet(settings)
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

    return HatchetWorkflows(hatchet, generation_workflow, generation_provider_step)
