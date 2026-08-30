import ast
import asyncio
import uuid
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal
from app.models import GenerationJob, JobEvent
from app.provider import (
    RETRYABLE_FAILURE_CODES,
    FailureCode,
    MockVideoProvider,
    PollResult,
    ProviderFailure,
    ProviderStatus,
    ProviderSubmissionError,
    is_retryable_failure,
)
from app.provider_execution import AttemptBudget, GenerationExecutionService
from app.storage import LocalObjectStorage
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_mock_jobs import create_shot, generate


def test_provider_module_has_no_orm_or_storage_dependencies() -> None:
    provider_path = Path(__file__).parents[1] / "app" / "provider.py"
    tree = ast.parse(provider_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert not any(
        name == "sqlalchemy" or name.startswith("sqlalchemy.") for name in imports
    )
    assert "app.db" not in imports
    assert "app.models" not in imports
    assert "app.storage" not in imports


def test_provider_and_domain_layers_do_not_depend_on_local_paths() -> None:
    app_root = Path(__file__).parents[1] / "app"
    for module_name in ["provider.py", "provider_execution.py", "models.py", "schemas.py"]:
        source = (app_root / module_name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert "pathlib" not in imports
        assert "LocalObjectStorage" not in source
        assert "path_for(" not in source


def test_failure_classification_is_explicit_and_closed() -> None:
    assert RETRYABLE_FAILURE_CODES == {
        FailureCode.NETWORK_TIMEOUT,
        FailureCode.PROVIDER_5XX,
        FailureCode.PROVIDER_CAPACITY,
        FailureCode.QUEUE_TIMEOUT,
        FailureCode.WORKER_INTERRUPTED,
    }
    assert is_retryable_failure(FailureCode.NETWORK_TIMEOUT)
    assert not is_retryable_failure(FailureCode.WORKFLOW_FAILED)
    assert not is_retryable_failure(FailureCode.INVALID_INPUT)


def test_attempt_budget_enforces_candidate_and_job_limits() -> None:
    assert AttemptBudget(total_attempts=2, candidate_attempts=1).allows_retry
    assert not AttemptBudget(total_attempts=2, candidate_attempts=2).allows_retry
    assert not AttemptBudget(total_attempts=3, candidate_attempts=1).allows_retry


def test_submit_unknown_reconciles_without_duplicate_submit(raw_client: TestClient) -> None:
    class PendingOnceProvider(MockVideoProvider):
        def __init__(self) -> None:
            super().__init__()
            self.poll_calls = 0

        async def poll(self, attempt):
            self.poll_calls += 1
            if self.poll_calls == 1:
                return PollResult(status=ProviderStatus.UNKNOWN)
            return await super().poll(attempt)

    queued = generate(raw_client, create_shot(raw_client)["id"], "success")
    job_id = uuid.UUID(queued["id"])
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        job.mock_mode = "submit_unknown"
        db.commit()
    provider = PendingOnceProvider()
    settings = get_settings()
    executor = GenerationExecutionService(
        LocalObjectStorage(
            settings.storage_root,
            settings.storage_claim_secret.get_secret_value().encode(),
        ),
        provider=provider,
    )

    asyncio.run(executor.execute(job_id))
    reconciling = raw_client.get(f"/v1/generations/{queued['id']}").json()
    assert reconciling["status"] == "ROUTING"
    assert reconciling["attempts"][0]["status"] == "SUBMITTING"
    assert len(provider.submit_calls) == 1

    asyncio.run(executor.execute(job_id))

    refreshed = raw_client.get(f"/v1/generations/{queued['id']}").json()
    assert refreshed["status"] == "SUCCEEDED"
    assert len(refreshed["attempts"]) == 1
    assert len(provider.submit_calls) == 1
    assert provider.poll_calls == 2
    with SessionLocal() as db:
        event_types = set(
            db.scalars(
                select(JobEvent.event_type).where(JobEvent.job_id == job_id)
            )
        )
    assert event_types >= {
        "provider.submit_unknown",
        "provider.reconciled",
        "attempt.reconciled",
    }


def test_submit_rejection_fails_without_unknown_reconciliation(
    raw_client: TestClient,
) -> None:
    class RejectingProvider(MockVideoProvider):
        async def submit(self, request):
            raise ProviderSubmissionError(
                ProviderFailure(
                    FailureCode.PROVIDER_AUTHENTICATION,
                    "Provider authentication failed",
                )
            )

    queued = generate(raw_client, create_shot(raw_client)["id"], "success")
    job_id = uuid.UUID(queued["id"])
    settings = get_settings()
    executor = GenerationExecutionService(
        LocalObjectStorage(
            settings.storage_root,
            settings.storage_claim_secret.get_secret_value().encode(),
        ),
        provider=RejectingProvider(),
    )

    asyncio.run(executor.execute(job_id))

    failed = raw_client.get(f"/v1/generations/{queued['id']}").json()
    assert failed["status"] == "FAILED_FINAL"
    assert failed["failure_code"] == "GENERATION_FAILED"
    assert failed["attempts"][0]["status"] == "FAILED_FINAL"
    assert len(failed["attempts"]) == 1
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        assert job.failure_code == FailureCode.PROVIDER_AUTHENTICATION.value
        event_types = set(
            db.scalars(select(JobEvent.event_type).where(JobEvent.job_id == job_id))
        )
    assert "provider.submit_unknown" not in event_types
