import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings

logger = structlog.get_logger()


class GenerationWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    payload: dict[str, object]


class GenerationStepResult(BaseModel):
    """Validated child-task result consumed by the durable orchestrator."""

    model_config = ConfigDict(extra="forbid")

    is_complete: bool
    poll_count: int = Field(ge=0)
    retry_after_ms: int = Field(ge=0, le=300_000)


@dataclass(frozen=True, slots=True)
class WorkflowStartRequest:
    job_id: uuid.UUID
    idempotency_key: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class WorkflowStartResult:
    workflow_id: str


class WorkflowStarter(Protocol):
    """Starts one business workflow for each stable idempotency key."""

    async def start(self, request: WorkflowStartRequest) -> WorkflowStartResult:
        """Return the existing workflow when the idempotency key was already accepted."""
        ...

    def ready(self) -> bool: ...


class HatchetWorkflowRunnable(Protocol):
    async def aio_run(
        self,
        input: GenerationWorkflowInput,
        *,
        wait_for_result: bool,
    ) -> object: ...


class HatchetGenerationStepRunnable(Protocol):
    async def aio_run(
        self,
        input: GenerationWorkflowInput,
        *,
        wait_for_result: bool,
        child_key: str,
    ) -> object: ...


class DurablePollingContext(Protocol):
    async def aio_sleep_for(
        self,
        duration: timedelta,
        label: str | None = None,
    ) -> object: ...


async def run_durable_generation(
    input: GenerationWorkflowInput,
    context: DurablePollingContext,
    step: HatchetGenerationStepRunnable,
) -> dict[str, str]:
    """Replay-safe durable loop; PostgreSQL remains the execution source of truth."""

    step_index = 1
    while True:
        raw_result = await step.aio_run(
            input,
            wait_for_result=True,
            child_key=f"job:{input.job_id}:provider-step:{step_index}:v1",
        )
        result = GenerationStepResult.model_validate(raw_result)
        if result.is_complete:
            return {"job_id": str(input.job_id)}
        if result.retry_after_ms:
            await context.aio_sleep_for(
                timedelta(milliseconds=result.retry_after_ms),
                label=f"provider-poll-{result.poll_count}",
            )
        step_index += 1


class HatchetWorkflowStarter:
    """Starts the durable generation workflow through the pinned Hatchet SDK."""

    def __init__(
        self,
        workflow: HatchetWorkflowRunnable | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._workflow = workflow
        self._settings = settings

    def _get_workflow(self) -> HatchetWorkflowRunnable:
        if self._workflow is None:
            from app.hatchet_workflows import create_hatchet_workflows

            self._workflow = create_hatchet_workflows(
                self._settings or get_settings()
            ).generation_workflow
        return self._workflow

    def ready(self) -> bool:
        return callable(getattr(self._get_workflow(), "aio_run", None))

    async def start(self, request: WorkflowStartRequest) -> WorkflowStartResult:
        from hatchet_sdk.exceptions import IdempotencyCollisionError

        workflow_input = GenerationWorkflowInput(
            job_id=request.job_id,
            idempotency_key=request.idempotency_key,
            payload=request.payload,
        )
        try:
            reference = await self._get_workflow().aio_run(
                workflow_input,
                wait_for_result=False,
            )
            workflow_id = getattr(reference, "workflow_run_id", None)
            reused = False
        except IdempotencyCollisionError as exc:
            workflow_id = exc.existing_run_external_id
            reused = True

        if not isinstance(workflow_id, str) or not workflow_id:
            raise RuntimeError("Hatchet returned an invalid workflow run reference")

        logger.info(
            "hatchet.workflow_accepted",
            job_id=str(request.job_id),
            workflow_key=request.idempotency_key,
            workflow_id=workflow_id,
            reused=reused,
        )
        return WorkflowStartResult(workflow_id=workflow_id)


def create_workflow_starter(settings: Settings) -> WorkflowStarter:
    if settings.workflow_backend == "local":
        if settings.environment == "production":
            raise ValueError(
                "WORKFLOW_BACKEND=local is development-only; use hatchet in production"
            )
        from app.local_workflow import LocalWorkflowStarter

        return LocalWorkflowStarter(settings)
    if settings.workflow_backend == "hatchet":
        settings.get_hatchet_token()
        return HatchetWorkflowStarter(settings=settings)
    raise ValueError("WORKFLOW_BACKEND must be local or hatchet")
