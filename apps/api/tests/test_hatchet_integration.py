import asyncio
import os
import uuid

import pytest

from app.workflow import HatchetWorkflowStarter, WorkflowStartRequest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_HATCHET_INTEGRATION") != "1",
        reason="set RUN_HATCHET_INTEGRATION=1 with the Compose Hatchet services running",
    ),
]


def test_real_hatchet_duplicate_start_reuses_workflow_run() -> None:
    from app.hatchet_workflows import hatchet

    job_id = uuid.uuid4()
    request = WorkflowStartRequest(
        job_id=job_id,
        idempotency_key=f"generation-job:{job_id}:v1",
        payload={"job_id": str(job_id)},
    )
    starter = HatchetWorkflowStarter()

    async def start_complete_and_replay():
        first = await starter.start(request)
        await hatchet.runs.get_run_ref(first.workflow_id).aio_result()
        duplicate = await starter.start(request)
        return first, duplicate

    first, duplicate = asyncio.run(start_complete_and_replay())

    assert duplicate.workflow_id == first.workflow_id
