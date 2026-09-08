"""Application dependency assembly and background entry points."""

import uuid
from datetime import timedelta

from app.artifact_lifecycle import ArtifactCleanupDispatcher
from app.blocking_io import run_blocking
from app.config import get_settings
from app.db import SessionLocal
from app.errors import ApiError, not_found
from app.local_workflow import LocalWorkflowStarter
from app.outbox import (
    DispatchResult,
    OutboxDispatcher,
    generation_workflow_key,
)
from app.project_cleanup import StorageCleanupDispatcher
from app.provider_cancel_outbox import (
    ProviderCancelDispatcher,
    ProviderCancelRequest,
)
from app.provider_execution import GenerationExecutionService
from app.provider_registry import ProviderNotConfiguredError
from app.routing import get_provider_registry
from app.storage import LocalObjectStorage, ObjectStorage
from app.workflow import WorkflowStartRequest, create_workflow_starter

workflow_starter = create_workflow_starter(get_settings())


def storage() -> ObjectStorage:
    settings = get_settings()
    return LocalObjectStorage(
        settings.storage_root, settings.storage_claim_secret.get_secret_value().encode()
    )


def storage_claim_ttl() -> timedelta:
    return timedelta(seconds=get_settings().storage_claim_ttl_seconds)


def provider_executor(
    provider_code: str, *, require_webhook_secret: bool = False
) -> GenerationExecutionService:
    providers = get_provider_registry()
    try:
        providers.get(provider_code)
    except ProviderNotConfiguredError as exc:
        raise not_found("provider") from exc
    secret = get_settings().mock_provider_webhook_secret
    if require_webhook_secret and provider_code == "mock" and (secret is None):
        raise ApiError(503, "PROVIDER_WEBHOOK_DISABLED", "Provider webhook 未配置")
    return GenerationExecutionService(storage(), provider_registry=providers)


async def dispatch_generation_outbox() -> DispatchResult:
    return await OutboxDispatcher(
        SessionLocal, workflow_starter, max_attempts=get_settings().outbox_max_attempts
    ).dispatch_once()


async def execute_provider_cancel(request: ProviderCancelRequest) -> None:
    await (await run_blocking(provider_executor, request.provider_code)).request_cancel(
        request.job_id, request.attempt_id, request.idempotency_key
    )


async def dispatch_provider_cancel_outbox() -> DispatchResult:
    return await ProviderCancelDispatcher(
        SessionLocal, execute_provider_cancel, max_attempts=get_settings().outbox_max_attempts
    ).dispatch_once()


async def dispatch_storage_cleanup_outbox() -> DispatchResult:
    artifact_result = await ArtifactCleanupDispatcher(
        SessionLocal, (await run_blocking(storage))
    ).dispatch_once()
    if artifact_result != DispatchResult.IDLE:
        return artifact_result
    return await StorageCleanupDispatcher(
        SessionLocal, (await run_blocking(storage)), max_attempts=get_settings().outbox_max_attempts
    ).dispatch_once()


async def reconcile_generation_job(job_id: uuid.UUID) -> object:
    if isinstance(workflow_starter, LocalWorkflowStarter):
        return await workflow_starter.start(
            WorkflowStartRequest(job_id, generation_workflow_key(job_id), {"job_id": str(job_id)})
        )
    return await (await run_blocking(lambda: GenerationExecutionService(storage()))).execute(job_id)
