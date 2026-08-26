import asyncio
import uuid
from dataclasses import dataclass

import pytest
from hatchet_sdk.exceptions import IdempotencyCollisionError

from app.workflow import (
    GenerationWorkflowInput,
    HatchetWorkflowStarter,
    WorkflowStartRequest,
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
