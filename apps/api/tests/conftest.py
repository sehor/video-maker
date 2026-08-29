import asyncio
import os
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Header
from fastapi.testclient import TestClient

api_root = Path(__file__).resolve().parents[1]
test_runtime_root = Path(
    os.environ.get("TEST_RUNTIME_ROOT", api_root / ".test-tmp")
).resolve()
test_runtime_root.mkdir(parents=True, exist_ok=True)

test_database_url = os.environ.get("TEST_DATABASE_URL")
if test_database_url:
    database_name = test_database_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not database_name.endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL must target a database ending in _test")
    os.environ["DATABASE_URL"] = test_database_url
else:
    os.environ["DATABASE_URL"] = (
        f"sqlite+pysqlite:///{test_runtime_root / 'test.db'}"
    )
os.environ["STORAGE_ROOT"] = str(test_runtime_root / "storage")
os.environ["OUTBOX_DISPATCHER_ENABLED"] = "false"
os.environ["MOCK_PROVIDER_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["STORAGE_CLAIM_SECRET"] = "test-storage-claim-secret-at-least-32-bytes"
os.environ["ADMIN_AUTH_SUBJECTS"] = "admin-user"

from app.auth import Identity, get_identity  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation_options import (  # noqa: E402
    InternalGenerationOptions,
    get_internal_generation_options,
)
from app.main import app  # noqa: E402
from app.outbox import DispatchResult, OutboxDispatcher  # noqa: E402
from app.provider_execution import GenerationExecutionService  # noqa: E402
from app.storage import LocalObjectStorage  # noqa: E402
from app.workflow import WorkflowStartRequest, WorkflowStartResult  # noqa: E402

get_settings.cache_clear()


def test_identity(x_test_user: Annotated[str | None, Header()] = None) -> Identity:
    return Identity(subject=x_test_user or "user-a")


app.dependency_overrides[get_identity] = test_identity


def test_generation_options(
    x_test_generation_modes: Annotated[str | None, Header()] = None,
) -> InternalGenerationOptions:
    modes = tuple(x_test_generation_modes.split(",")) if x_test_generation_modes else ()
    return InternalGenerationOptions(modes=modes)  # type: ignore[arg-type]


app.dependency_overrides[get_internal_generation_options] = test_generation_options


@pytest.fixture(autouse=True)
def clean_database(tmp_path: Path):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    settings = get_settings()
    settings.storage_root = tmp_path / "storage"
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def raw_client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


class InlineWorkflowStarter:
    """Runs the Hatchet child boundary in-process for Docker-free unit tests."""

    async def start(self, request: WorkflowStartRequest) -> WorkflowStartResult:
        settings = get_settings()
        store = LocalObjectStorage(
            settings.storage_root,
            settings.storage_claim_secret.get_secret_value().encode(),
        )
        executor = GenerationExecutionService(store)
        await executor.execute(request.job_id)
        return WorkflowStartResult(workflow_id=f"test:{request.idempotency_key}")


@pytest.fixture
def client(raw_client: TestClient) -> TestClient:
    original_post = raw_client.post
    starter = InlineWorkflowStarter()

    def post_and_dispatch(url: str, *args, **kwargs):
        response = original_post(url, *args, **kwargs)
        if url == "/v1/generations" and response.status_code == 202:
            result = asyncio.run(OutboxDispatcher(SessionLocal, starter).dispatch_once())
            assert result in {DispatchResult.IDLE, DispatchResult.PUBLISHED}
        return response

    raw_client.post = post_and_dispatch  # type: ignore[method-assign]
    return raw_client
