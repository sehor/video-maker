import uuid
from dataclasses import dataclass
from typing import Protocol

from app.provider import MockVideoProvider


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


class MockWorkflowStarter:
    """Stage-two adapter that keeps the orchestration boundary free of Hatchet."""

    def __init__(self, provider: MockVideoProvider) -> None:
        self.provider = provider

    async def start(self, request: WorkflowStartRequest) -> WorkflowStartResult:
        await self.provider.submit(request.job_id)
        return WorkflowStartResult(workflow_id=request.idempotency_key)
