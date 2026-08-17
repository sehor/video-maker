from datetime import timedelta
from functools import lru_cache

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.idempotency import TTLBasedIdempotencyConfig
from pydantic import BaseModel

from app.config import get_settings
from app.provider import MockVideoProvider
from app.storage import LocalObjectStorage


class GenerationWorkflowInput(BaseModel):
    job_id: str
    attempt_id: str
    workflow_key: str


@lru_cache
def get_hatchet_generation_task():
    hatchet = Hatchet()

    @hatchet.task(
        name="generation-job-v1",
        input_validator=GenerationWorkflowInput,
        retries=2,
        backoff_factor=2,
        schedule_timeout="15m",
        execution_timeout="15m",
        idempotency=TTLBasedIdempotencyConfig(
            key_expression="input.workflow_key",
            ttl=timedelta(days=7),
        ),
    )
    async def generation_job(
        input: GenerationWorkflowInput, _ctx: Context
    ) -> dict[str, str]:
        import uuid

        settings = get_settings()
        provider = MockVideoProvider(LocalObjectStorage(settings.storage_root))
        await provider.submit(uuid.UUID(input.job_id), uuid.UUID(input.attempt_id))
        return {"job_id": input.job_id, "attempt_id": input.attempt_id}

    return hatchet, generation_job


def main() -> None:
    hatchet, generation_job = get_hatchet_generation_task()
    worker = hatchet.worker(
        "video-generation-worker-v1",
        slots=4,
        workflows=[generation_job],
    )
    worker.start()


if __name__ == "__main__":
    main()
