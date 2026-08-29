import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from hatchet_sdk.exceptions import IdempotencyCollisionError

from app.workflow import (
    GenerationWorkflowInput,
    HatchetWorkflowStarter,
    WorkflowStartRequest,
    run_durable_generation,
)


@dataclass
class FakeWorkflowReference:
    workflow_run_id: str


class FakeHatchetWorkflow:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[GenerationWorkflowInput, bool]] = []

    async def aio_run(
        self,
        input: GenerationWorkflowInput,
        *,
        wait_for_result: bool,
    ) -> object:
        self.calls.append((input, wait_for_result))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeGenerationStep:
    def __init__(self, results: list[dict[str, object]]) -> None:
        self.results = iter(results)
        self.child_keys: list[str] = []

    async def aio_run(
        self,
        input: GenerationWorkflowInput,
        *,
        wait_for_result: bool,
        child_key: str,
    ) -> object:
        assert wait_for_result is True
        self.child_keys.append(child_key)
        return next(self.results)


class FakeDurableContext:
    def __init__(self) -> None:
        self.sleeps: list[tuple[timedelta, str | None]] = []

    async def aio_sleep_for(
        self,
        duration: timedelta,
        label: str | None = None,
    ) -> object:
        self.sleeps.append((duration, label))
        return object()


def workflow_request() -> WorkflowStartRequest:
    job_id = uuid.uuid4()
    return WorkflowStartRequest(
        job_id=job_id,
        idempotency_key=f"generation-job:{job_id}:v1",
        payload={"job_id": str(job_id)},
    )


def test_hatchet_starter_returns_accepted_workflow_run() -> None:
    workflow = FakeHatchetWorkflow(FakeWorkflowReference("run-1"))
    request = workflow_request()

    result = asyncio.run(HatchetWorkflowStarter(workflow).start(request))

    assert result.workflow_id == "run-1"
    assert workflow.calls == [
        (
            GenerationWorkflowInput(
                job_id=request.job_id,
                idempotency_key=request.idempotency_key,
                payload=request.payload,
            ),
            False,
        )
    ]


def test_hatchet_starter_reuses_collision_workflow_run() -> None:
    workflow = FakeHatchetWorkflow(IdempotencyCollisionError("existing-run"))

    result = asyncio.run(HatchetWorkflowStarter(workflow).start(workflow_request()))

    assert result.workflow_id == "existing-run"


def test_hatchet_starter_rejects_invalid_sdk_response() -> None:
    workflow = FakeHatchetWorkflow(object())

    with pytest.raises(RuntimeError, match="invalid workflow run reference"):
        asyncio.run(HatchetWorkflowStarter(workflow).start(workflow_request()))


def test_durable_generation_uses_unique_steps_and_durable_backoff() -> None:
    request = workflow_request()
    input = GenerationWorkflowInput(
        job_id=request.job_id,
        idempotency_key=request.idempotency_key,
        payload=request.payload,
    )
    step = FakeGenerationStep(
        [
            {"is_complete": False, "poll_count": 1, "retry_after_ms": 1_000},
            {"is_complete": False, "poll_count": 2, "retry_after_ms": 2_000},
            {"is_complete": True, "poll_count": 3, "retry_after_ms": 0},
        ]
    )
    context = FakeDurableContext()

    result = asyncio.run(run_durable_generation(input, context, step))

    assert result == {"job_id": str(request.job_id)}
    assert step.child_keys == [
        f"job:{request.job_id}:provider-step:1:v1",
        f"job:{request.job_id}:provider-step:2:v1",
        f"job:{request.job_id}:provider-step:3:v1",
    ]
    assert context.sleeps == [
        (timedelta(seconds=1), "provider-poll-1"),
        (timedelta(seconds=2), "provider-poll-2"),
    ]
