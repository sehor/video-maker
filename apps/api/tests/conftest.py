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
os.environ["RECONCILER_ENABLED"] = "false"
os.environ["WORKFLOW_BACKEND"] = "local"
os.environ["MOCK_PROVIDER_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["STORAGE_CLAIM_SECRET"] = "test-storage-claim-secret-at-least-32-bytes"
os.environ["ADMIN_AUTH_SUBJECTS"] = "admin-user"

from app import main, public_api  # noqa: E402
from app.auth import Identity, get_identity  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.generation_options import (  # noqa: E402
    InternalGenerationOptions,
    get_internal_generation_options,
)
from app.local_workflow import LocalWorkflowStarter  # noqa: E402
from app.main import app  # noqa: E402
from app.outbox import DispatchResult  # noqa: E402

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
def raw_client(monkeypatch) -> TestClient:
    # Use the same runner/settings on the TestClient lifespan loop and API dispatch path.
    settings = get_settings()
    runner = LocalWorkflowStarter(settings)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "workflow_starter", runner)
    monkeypatch.setattr(public_api, "workflow_starter", runner)
    with TestClient(app) as test_client:
        yield test_client


async def dispatch_local_outbox() -> DispatchResult:
    result = await public_api.dispatch_generation_outbox()
    runner = public_api.workflow_starter
    assert isinstance(runner, LocalWorkflowStarter)
    await asyncio.wait_for(runner.wait_idle(), timeout=30)
    return result


@pytest.fixture
def client(raw_client: TestClient) -> TestClient:
    original_post = raw_client.post

    def post_and_dispatch(url: str, *args, **kwargs):
        response = original_post(url, *args, **kwargs)
        if url == "/v1/generations" and response.status_code == 202:
            assert raw_client.portal is not None
            result = raw_client.portal.call(dispatch_local_outbox)
            assert result in {DispatchResult.IDLE, DispatchResult.PUBLISHED}
        return response

    raw_client.post = post_and_dispatch  # type: ignore[method-assign]
    return raw_client
