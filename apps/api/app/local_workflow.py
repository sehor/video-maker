from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog

from app.blocking_io import run_blocking
from app.config import Settings
from app.workflow import GenerationWorkflowInput, WorkflowStartRequest, WorkflowStartResult

if TYPE_CHECKING:
    from app.provider_execution import ProviderExecutionStep

logger = structlog.get_logger()


class GenerationExecutor(Protocol):
    async def execute(self, job_id: uuid.UUID) -> ProviderExecutionStep: ...


@dataclass(frozen=True)
class LocalRun:
    job_id: uuid.UUID
    task: asyncio.Task[None]


class LocalWorkflowStarter:
    """Development-only, one-loop scheduler; PostgreSQL owns business state and recovery."""

    def __init__(
        self,
        settings: Settings,
        *,
        executor_factory: Callable[[], GenerationExecutor] | None = None,
    ) -> None:
        self._settings = settings
        self._require_development()
        self._executor_factory = executor_factory or self._create_executor
        self._loop: asyncio.AbstractEventLoop | None = None
        self._accepting = False
        self._runs: dict[str, LocalRun] = {}

    def _require_development(self) -> None:
        if self._settings.environment == "production":
            raise ValueError("Local workflow runner is development-only")

    def _create_executor(self) -> GenerationExecutor:
        from app.provider_execution import GenerationExecutionService
        from app.storage import LocalObjectStorage

        return GenerationExecutionService(
            LocalObjectStorage(
                self._settings.storage_root,
                self._settings.storage_claim_secret.get_secret_value().encode(),
            )
        )

    def ready(self) -> bool:
        return self._accepting and self._loop is not None and self._loop.is_running()

    def _require_loop(self) -> None:
        if self._loop is not asyncio.get_running_loop():
            raise RuntimeError("Local workflow runner must be used on its lifespan event loop")

    async def startup(self) -> None:
        self._require_development()
        if self._loop is not None:
            self._require_loop()
            if not self._accepting:
                raise RuntimeError("Local workflow runner is shutting down")
            return
        self._loop = asyncio.get_running_loop()
        self._accepting = True
        logger.warning("local_workflow.started", development_only=True)

    async def start(self, request: WorkflowStartRequest) -> WorkflowStartResult:
        if not self.ready():
            raise RuntimeError("Local workflow runner is not running; start the API lifespan first")
        self._require_loop()
        validated = GenerationWorkflowInput(
            job_id=request.job_id, idempotency_key=request.idempotency_key, payload=request.payload
        )
        key = validated.idempotency_key
        workflow_id = f"local:{uuid.uuid5(uuid.NAMESPACE_URL, 'video-maker:local:' + key)}"
        previous = self._runs.get(key)
        if previous is not None:
            if previous.job_id != request.job_id:
                raise ValueError("Local workflow idempotency key belongs to another job")
            task = previous.task
            if not task.done() or (not task.cancelled() and task.exception() is None):
                return WorkflowStartResult(workflow_id)

        # There is no await before registering the task: duplicate starts on this loop
        # see the same run. Failed/cancelled runs may re-enter the idempotent DB path.
        task = asyncio.create_task(self._execute(request.job_id), name=workflow_id)
        self._runs[key] = LocalRun(request.job_id, task)
        task.add_done_callback(self._observe_completion)
        logger.info("local_workflow.accepted", job_id=str(request.job_id), workflow_id=workflow_id)
        return WorkflowStartResult(workflow_id)

    async def _execute(self, job_id: uuid.UUID) -> None:
        executor = await run_blocking(self._executor_factory)
        while True:
            step = await executor.execute(job_id)
            if step.is_complete:
                return
            delay = step.retry_after.total_seconds() if step.retry_after is not None else 0
            # Yield even when a contended DB lease reports no retry delay.
            await asyncio.sleep(max(delay, 0.01))

    @staticmethod
    def _observe_completion(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "local_workflow.failed",
                workflow_id=task.get_name(),
                error_type=type(error).__name__,
            )
        else:
            logger.info("local_workflow.completed", workflow_id=task.get_name())

    async def wait_idle(self) -> None:
        """Wait for currently accepted work; cancelling the waiter does not cancel jobs."""
        self._require_loop()
        await asyncio.gather(*(asyncio.shield(run.task) for run in self._runs.values()))

    async def shutdown(self) -> None:
        if self._loop is None:
            return
        self._require_loop()
        self._accepting = False
        tasks = [run.task for run in self._runs.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()
        self._loop = None
        logger.info("local_workflow.stopped")
