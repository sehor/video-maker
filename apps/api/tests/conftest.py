import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from test_support import (
    isolated_schema,
    migrate,
    reset_committed_data,
    rollback_sessions,
    test_database_url,
)

test_runtime_root = Path(
    os.environ.get("TEST_RUNTIME_ROOT") or tempfile.mkdtemp(prefix="video-tests-")
).resolve()
test_runtime_root.mkdir(parents=True, exist_ok=True)

# A nonconnecting placeholder allows pure tests without any configured database.
os.environ["DATABASE_URL"] = "postgresql+psycopg://unused:unused@127.0.0.1:1/unused_test"
os.environ["ENVIRONMENT"] = "development"
os.environ["GENERATION_ROUTE_VERSION"] = "mock_video_v1"
os.environ["RUNPOD_PROVIDER_ENABLED"] = "false"
os.environ["STORAGE_ROOT"] = str(test_runtime_root / "storage")
os.environ["OUTBOX_DISPATCHER_ENABLED"] = "false"
os.environ["RECONCILER_ENABLED"] = "false"
os.environ["WORKFLOW_BACKEND"] = "local"
os.environ["MOCK_PROVIDER_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["STORAGE_CLAIM_SECRET"] = "test-storage-claim-secret-at-least-32-bytes"
os.environ["ADMIN_AUTH_SUBJECTS"] = "admin-user"

from app import bootstrap as public_api  # noqa: E402
from app import main  # noqa: E402
from app.auth import Identity, get_identity  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
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


@pytest.fixture(scope="session")
def database_engine():
    with isolated_schema(test_database_url()) as url:
        migrate(url)
        isolated = create_engine(url, pool_pre_ping=True)
        original_pool, original_url = engine.pool, engine.url
        engine.pool, engine.url = isolated.pool, url
        try:
            yield engine
        finally:
            engine.dispose()
            engine.pool, engine.url = original_pool, original_url


@pytest.fixture
def clean_database(database_engine, database_reset_state, request):
    # A new schema and rollback-only tests are already clean. Reset only after a
    # real-commit test; retain immutable ledger triggers and independent connections.
    started = time.perf_counter()
    if not database_reset_state["dirty"]:
        request.node.user_properties.append(("db_reset_seconds", 0.0))
        return
    with database_engine.begin() as connection:
        # Cleanup retains the normal 30s statement bound and all DB protections.
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        roots = reset_committed_data(connection, Base.metadata)
    database_reset_state["dirty"] = False
    request.node.user_properties.append(("db_reset_seconds", time.perf_counter() - started))
    request.node.user_properties.append(("db_truncate_roots", ",".join(roots)))


@pytest.fixture(scope="session")
def database_reset_state():
    return {"dirty": False}


@pytest.fixture
def database_isolation(clean_database, database_engine, database_reset_state, request, monkeypatch):
    if request.node.get_closest_marker("db_rollback"):
        request.node.user_properties.append(("db_isolation", "rollback"))
        with rollback_sessions(SessionLocal, database_engine):
            def unexpected_connection(*args, **kwargs):
                pytest.fail("db_rollback tests must use sequential SessionLocal sessions")
            with monkeypatch.context() as patch:
                patch.setattr(engine, "connect", unexpected_connection)
                yield
    else:
        request.node.user_properties.append(("db_isolation", "committed"))
        # Mark before execution so failed tests are also cleaned before the next one.
        database_reset_state["dirty"] = True
        yield


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch, request):
    settings = get_settings()
    original = settings.model_copy(deep=True)
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    if request.node.get_closest_marker("database"):
        if "migration_database" in request.fixturenames:
            request.node.user_properties.append(("db_isolation", "migration"))
        else:
            request.getfixturevalue("database_isolation")
            monkeypatch.setattr(
                settings, "database_url", engine.url.render_as_string(hide_password=False)
            )
    else:
        def unexpected_connection(*args, **kwargs):
            pytest.fail("Database access requires @pytest.mark.database")
        monkeypatch.setattr(engine, "connect", unexpected_connection)
    overrides = app.dependency_overrides.copy()
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(overrides)
    for key, value in original.__dict__.items():
        setattr(settings, key, value)


@pytest.fixture
def migration_database():
    with isolated_schema(test_database_url()) as url:
        temporary = create_engine(url)
        try:
            yield url.render_as_string(hide_password=False), temporary
        finally:
            temporary.dispose()


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

