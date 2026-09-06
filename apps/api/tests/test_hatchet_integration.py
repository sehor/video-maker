import asyncio
import os
import uuid

import pytest

from app.workflow import HatchetWorkflowStarter, WorkflowStartRequest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_HATCHET_INTEGRATION") != "1",
        reason="set RUN_HATCHET_INTEGRATION=1 with a configured Hatchet server and worker",
    ),
]


def test_real_hatchet_duplicate_start_reuses_workflow_run() -> None:
    from app.config import get_settings
    from app.hatchet_workflows import create_hatchet_workflows

    workflows = create_hatchet_workflows(get_settings())

    job_id = uuid.uuid4()
    request = WorkflowStartRequest(
        job_id=job_id,
        idempotency_key=f"generation-job:{job_id}:v1",
        payload={"job_id": str(job_id)},
    )
    starter = HatchetWorkflowStarter(workflows.generation_workflow)

    async def start_complete_and_replay():
        first = await starter.start(request)
        await workflows.hatchet.runs.get_run_ref(first.workflow_id).aio_result()
        duplicate = await starter.start(request)
        return first, duplicate

    async def bounded_integration():
        return await asyncio.wait_for(start_complete_and_replay(), timeout=120)

    first, duplicate = asyncio.run(bounded_integration())

    assert duplicate.workflow_id == first.workflow_id
