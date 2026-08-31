import sys

import structlog

from app.config import get_settings

logger = structlog.get_logger()


def create_generation_worker(workflows):
    registered = [workflows.generation_workflow, workflows.generation_provider_step]
    if sys.platform == "win32":
        from hatchet_sdk.utils.slots import normalize_slot_config, resolve_worker_slot_config

        from app.windows_hatchet_worker import WindowsHatchetWorker

        return WindowsHatchetWorker(
            name="generation-worker-v1",
            config=workflows.hatchet.config,
            slot_config=normalize_slot_config(
                resolve_worker_slot_config(None, None, None, registered)
            ),
            workflows=registered,
        )
    return workflows.hatchet.worker("generation-worker-v1", workflows=registered)


def main() -> None:
    settings = get_settings()
    if settings.workflow_backend != "hatchet":
        raise RuntimeError("The Hatchet worker requires WORKFLOW_BACKEND=hatchet")
    from app.hatchet_workflows import create_hatchet_workflows

    workflows = create_hatchet_workflows(settings)
    logger.info("hatchet.worker_started", worker_name="generation-worker-v1")
    create_generation_worker(workflows).start()


if __name__ == "__main__":
    main()
