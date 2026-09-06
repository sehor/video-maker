import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.workflow import (
    HatchetWorkflowStarter,
    WorkflowStartRequest,
    create_workflow_starter,
)


@pytest.fixture(autouse=True)
def clean_database():
    """Configuration and import checks must not open or reset any database."""
    yield


def settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "environment": "development",
            "workflow_backend": "local",
            "hatchet_client_token": "",
            "hatchet_client_token_file": None,
            **overrides,
        },
    )


@pytest.mark.parametrize("backend", ["", "unknown", "LOCAL"])
def test_backend_rejects_unknown_values(backend: str) -> None:
    with pytest.raises(ValidationError, match="workflow_backend"):
        settings(workflow_backend=backend)


def test_local_backend_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="WORKFLOW_BACKEND=local is development-only"):
        settings(environment="production")


def test_hatchet_backend_allows_production_with_credentials() -> None:
    configured = settings(
        environment="production",
        workflow_backend="hatchet",
        hatchet_client_token="test-token",
        storage_claim_secret="s" * 32,
        provider_callback_claim_secret="c" * 32,
    )
    assert isinstance(create_workflow_starter(configured), HatchetWorkflowStarter)


@pytest.mark.parametrize("token", ["", " \n "])
def test_hatchet_requires_credentials(token: str) -> None:
    with pytest.raises(ValidationError, match="HATCHET_CLIENT_TOKEN"):
        settings(workflow_backend="hatchet", hatchet_client_token=token)


def test_configuration_errors_do_not_expose_credentials() -> None:
    with pytest.raises(ValidationError) as error:
        settings(environment="production", hatchet_client_token="private-test-token")
    assert "private-test-token" not in str(error.value)


def test_hatchet_token_can_be_loaded_from_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HATCHET_CLIENT_TOKEN", raising=False)
    monkeypatch.delenv("HATCHET_CLIENT_TOKEN_FILE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("HATCHET_CLIENT_TOKEN=dotenv-test-token\n", encoding="utf-8")
    configured = Settings(_env_file=env_file, workflow_backend="hatchet")
    assert configured.get_hatchet_token() == "dotenv-test-token"


def test_hatchet_token_file_has_precedence(tmp_path: Path) -> None:
    token_file = tmp_path / "token with spaces"
    token_file.write_text(" file-token\n", encoding="utf-8")
    configured = settings(
        workflow_backend="hatchet",
        hatchet_client_token="environment-token",
        hatchet_client_token_file=token_file,
    )
    assert configured.get_hatchet_token() == "file-token"


@pytest.mark.parametrize("exists", [False, True])
def test_invalid_hatchet_token_file_does_not_fall_back(tmp_path: Path, exists: bool) -> None:
    token_file = tmp_path / "token"
    if exists:
        token_file.write_text(" \n", encoding="utf-8")
    with pytest.raises(ValidationError, match="HATCHET_CLIENT_TOKEN"):
        settings(
            workflow_backend="hatchet",
            hatchet_client_token="environment-token",
            hatchet_client_token_file=token_file,
        )


def test_local_does_not_read_hatchet_credentials(tmp_path: Path) -> None:
    starter = create_workflow_starter(settings(hatchet_client_token_file=tmp_path / "missing"))
    assert starter.ready() is False
    with pytest.raises(RuntimeError, match="lifespan"):
        asyncio.run(starter.start(WorkflowStartRequest(uuid.uuid4(), "local-test", {})))


def test_local_api_and_worker_import_without_hatchet_sdk(tmp_path: Path) -> None:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("HATCHET_")
    }
    env.update(
        WORKFLOW_BACKEND="local",
        ENVIRONMENT="development",
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        DATABASE_URL="postgresql+psycopg://unused:unused@127.0.0.1:1/unused_test",
        STORAGE_ROOT=str(tmp_path / "storage"),
    )
    code = """
import importlib.abc
import sys

class RejectHatchet(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'hatchet_sdk' or fullname.startswith('hatchet_sdk.'):
            raise AssertionError('Local mode imported Hatchet SDK')

sys.meta_path.insert(0, RejectHatchet())
import app.main
import app.worker
import app.hatchet_workflows
assert not app.main.workflow_starter.ready()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_hatchet_client_is_created_once_on_first_use(monkeypatch) -> None:
    from app import hatchet_workflows

    workflow = SimpleNamespace(aio_run=Mock())
    factory = Mock(return_value=SimpleNamespace(generation_workflow=workflow))
    monkeypatch.setattr(hatchet_workflows, "create_hatchet_workflows", factory)
    configured = settings(workflow_backend="hatchet", hatchet_client_token="test-token")
    starter = create_workflow_starter(configured)
    factory.assert_not_called()
    assert starter.ready() is True
    assert starter.ready() is True
    factory.assert_called_once_with(configured)


def test_hatchet_initialization_failure_does_not_fall_back(monkeypatch) -> None:
    from app import hatchet_workflows

    monkeypatch.setattr(
        hatchet_workflows,
        "create_hatchet_workflows",
        Mock(side_effect=RuntimeError("invalid token")),
    )
    starter = create_workflow_starter(
        settings(workflow_backend="hatchet", hatchet_client_token="invalid-token")
    )
    with pytest.raises(RuntimeError, match="invalid token"):
        starter.ready()


def test_invalid_sdk_token_error_does_not_expose_token() -> None:
    starter = create_workflow_starter(
        settings(workflow_backend="hatchet", hatchet_client_token="private-invalid-token")
    )
    with pytest.raises(RuntimeError, match="Hatchet client configuration is invalid") as error:
        starter.ready()
    assert "private-invalid-token" not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_pinned_sdk_registers_workflows_without_server(monkeypatch) -> None:
    import socket

    import jwt

    from app.hatchet_workflows import create_hatchet_workflows

    monkeypatch.setattr(
        socket, "create_connection", Mock(side_effect=AssertionError("No network in registration"))
    )
    token = jwt.encode(
        {
            "sub": "00000000-0000-0000-0000-000000000001",
            "server_url": "http://localhost:8888",
            "grpc_broadcast_address": "localhost:7077",
        },
        "offline-registration-test-secret-32",
        algorithm="HS256",
    )
    workflows = create_hatchet_workflows(
        settings(
            workflow_backend="hatchet", hatchet_client_token=token,
            hatchet_client_tls_strategy="none",
        )
    )
    assert callable(workflows.generation_workflow.aio_run)
    assert callable(workflows.generation_provider_step.aio_run)


def test_local_worker_fails_before_loading_hatchet(monkeypatch) -> None:
    from app import worker

    monkeypatch.setattr(worker, "get_settings", lambda: settings())
    with pytest.raises(RuntimeError, match="requires WORKFLOW_BACKEND=hatchet"):
        worker.main()


@pytest.mark.parametrize("dispatcher,reconciler", [(True, False), (False, True), (True, True)])
def test_lifespan_starts_runner_before_dispatchers_and_stops_it(
    monkeypatch, dispatcher: bool, reconciler: bool
) -> None:
    from app import main

    configured = settings(
        outbox_dispatcher_enabled=dispatcher,
        reconciler_enabled=reconciler,
    )
    monkeypatch.setattr(main, "settings", configured)
    monkeypatch.setattr(main, "workflow_starter", create_workflow_starter(configured))
    runner = main.workflow_starter
    running_tasks = []

    async def background_loop(stop):
        assert runner.ready()
        running_tasks.append(asyncio.current_task())
        await stop.wait()

    for name in (
        "outbox_dispatcher_loop",
        "provider_cancel_dispatcher_loop",
        "storage_cleanup_dispatcher_loop",
        "control_plane_reconciler_loop",
    ):
        monkeypatch.setattr(main, name, background_loop)

    async def startup():
        assert not runner.ready()
        async with main.lifespan(main.app):
            assert runner.ready()
            await asyncio.sleep(0)
            assert len(running_tasks) == int(dispatcher) * 3 + int(reconciler)
        assert not runner.ready()
        assert all(task.done() for task in running_tasks)

    asyncio.run(startup())
