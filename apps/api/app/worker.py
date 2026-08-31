import structlog

from app.config import get_settings

logger = structlog.get_logger()


def main() -> None:
    settings = get_settings()
    if settings.workflow_backend != "hatchet":
        raise RuntimeError("The Hatchet worker requires WORKFLOW_BACKEND=hatchet")
    from app.hatchet_workflows import create_hatchet_workflows

    workflows = create_hatchet_workflows(settings)
    logger.info("hatchet.worker_started", worker_name="generation-worker-v1")
    workflows.hatchet.worker(
        "generation-worker-v1",
        workflows=[workflows.generation_workflow, workflows.generation_provider_step],
    ).start()


if __name__ == "__main__":
    main()
