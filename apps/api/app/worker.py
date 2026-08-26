import structlog

from app.hatchet_workflows import WORKFLOWS, hatchet

logger = structlog.get_logger()


def main() -> None:
    logger.info("hatchet.worker_started", worker_name="generation-worker-v1")
    hatchet.worker("generation-worker-v1", workflows=WORKFLOWS).start()


if __name__ == "__main__":
    main()
