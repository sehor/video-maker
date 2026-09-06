import asyncio
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import jwt
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.hatchet_workflows import create_hatchet, create_hatchet_workflows


@pytest.fixture(autouse=True)
def clean_database():
    """Transport and native bootstrap tests have no database dependency."""
    yield


def token(server="https://console.example.test", host="grpc.example.test:443"):
    return jwt.encode(
        {"sub": "00000000-0000-0000-0000-000000000001",
         "server_url": server, "grpc_broadcast_address": host},
        "offline-hatchet-test-secret-at-least-32", algorithm="HS256",
    )


def settings(**overrides):
    return Settings(_env_file=None, **{
        "environment": "development", "workflow_backend": "hatchet",
        "hatchet_client_token": token(), "hatchet_client_token_file": None,
        "hatchet_client_host_port": None, "hatchet_server_url": None,
        "hatchet_client_tls_strategy": "tls", "hatchet_client_tls_root_ca_file": None,
        "hatchet_client_tls_server_name": "", "hatchet_client_namespace": "",
        **overrides,
    })


def test_cloud_uses_token_addresses_tls_and_namespace_without_network(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("network")))
    client = create_hatchet(settings(hatchet_client_namespace="Windev_Test"))
    assert client.config.host_port == "grpc.example.test:443"
    assert client.config.server_url == "https://console.example.test"
    assert client.config.tls_config.strategy == "tls"
    assert client.config.tls_config.server_name == "grpc.example.test"
    assert client.config.namespace == "windev_test_"


def test_explicit_addresses_override_token_and_other_sdk_env_cannot_disable_tls(monkeypatch):
    monkeypatch.setenv("HATCHET_CLIENT_SERVER_URL", "http://wrong.invalid")
    monkeypatch.setenv("HATCHET_CLIENT_TLS_STRATEGY", "none")
    config = create_hatchet(settings(
        hatchet_client_host_port="other.example.test:443",
        hatchet_server_url="https://other.example.test",
    )).config
    assert config.host_port == "other.example.test:443"
    assert config.server_url == "https://other.example.test"
    assert config.tls_config.strategy == "tls"


def test_sdk_selects_secure_channel_with_certificate_verification(monkeypatch):
    from hatchet_sdk import connection

    secure = Mock(return_value=object())
    insecure = Mock(side_effect=AssertionError("TLS downgrade"))
    monkeypatch.setattr(connection.grpc, "secure_channel", secure)
    monkeypatch.setattr(connection.grpc, "insecure_channel", insecure)
    config = create_hatchet(settings()).config
    connection.new_conn(config, aio=False)
    assert secure.call_args.kwargs["target"] == "grpc.example.test:443"
    assert secure.call_args.kwargs["credentials"] is not None
    assert ("grpc.ssl_target_name_override", "grpc.example.test") in (
        secure.call_args.kwargs["options"]
    )


@pytest.mark.parametrize("strategy", ["", "TLS", "typo", "mtls"])
def test_unknown_tls_strategy_fails_closed(strategy):
    with pytest.raises(ValidationError, match="hatchet_client_tls_strategy"):
        settings(hatchet_client_tls_strategy=strategy)


@pytest.mark.parametrize("host", ["https://grpc.example.test:443", "grpc.example.test",
                                    "user:secret@grpc.example.test:443", "host:0", "host:bad"])
def test_bad_grpc_addresses_fail_without_exposing_input(host):
    with pytest.raises(ValidationError) as error:
        settings(hatchet_client_host_port=host)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("server", ["http://console.example.test", "https://u:secret@host",
                                      "https://host/api", "https://host?token=secret"])
def test_bad_rest_addresses_fail_without_exposing_input(server):
    with pytest.raises(ValidationError) as error:
        settings(hatchet_server_url=server)
    assert "secret" not in str(error.value)


def test_remote_token_cannot_select_plaintext_and_error_does_not_leak_token():
    configured = settings(hatchet_client_tls_strategy="none")
    with pytest.raises(RuntimeError, match="TLS settings") as error:
        create_hatchet(configured)
    assert configured.get_hatchet_token() not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_local_plaintext_requires_explicit_selection_and_production_rejects_it():
    client = create_hatchet(settings(
        hatchet_client_token=token("http://hatchet:8888", "hatchet:7077"),
        hatchet_client_tls_strategy="none",
    ))
    assert client.config.tls_config.strategy == "none"
    with pytest.raises(ValidationError, match="Production Hatchet requires"):
        settings(environment="production", hatchet_client_tls_strategy="none")


def test_ca_file_and_server_name_are_forwarded_and_conflicting_options_rejected(tmp_path):
    ca = tmp_path / "root ca.pem"
    ca.write_text("offline certificate fixture")
    configured = settings(
        hatchet_client_tls_root_ca_file=ca, hatchet_client_tls_server_name="tls.example.test"
    )
    tls = create_hatchet(configured).config.tls_config
    assert tls.root_ca_file == str(ca)
    assert tls.server_name == "tls.example.test"
    with pytest.raises(ValidationError, match="unavailable"):
        settings(hatchet_client_tls_root_ca_file=tmp_path / "missing.pem")
    with pytest.raises(ValidationError, match="cannot be used"):
        settings(hatchet_client_tls_strategy="none", hatchet_client_tls_server_name="host")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native signal bootstrap")
def test_native_worker_and_sdk_listener_construct_and_handle_signals_without_network():
    from hatchet_sdk.worker.action_listener_process import WorkerActionListenerProcess

    from app.windows_hatchet_worker import WindowsHatchetWorker, WindowsSignalLoop
    from app.worker import create_generation_worker

    previous = {
        sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGBREAK)
    }
    worker = None
    loop = WindowsSignalLoop()
    try:
        worker = create_generation_worker(create_hatchet_workflows(settings()))
        assert isinstance(worker, WindowsHatchetWorker)
        calls = []
        loop.add_signal_handler(signal.SIGTERM, calls.append, "term")
        signal.raise_signal(signal.SIGTERM)
        loop.run_until_complete(asyncio.sleep(0))
        assert calls == ["term"]

        async def construct_listener():
            return WorkerActionListenerProcess(
                name=worker.name, actions=[], slot_config=worker._slot_config,
                config=worker._config, action_queue=worker._action_queue,
                event_queue=worker._event_queue, handle_kill=False, debug=False,
                labels=[], worker_id_queue=worker._worker_id_queue,
                stop_event=worker._stop_listener_event,
            )

        listener = loop.run_until_complete(construct_listener())
        assert listener.listener is None  # Constructor did not connect to Cloud.
    finally:
        loop.close()
        if worker is not None:
            worker._close_queues()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native startup failure")
def test_native_startup_failure_exits_with_error_instead_of_hanging():
    script = """
from hatchet_sdk.worker.worker import Worker
from app.worker import create_generation_worker
from app.hatchet_workflows import create_hatchet_workflows
from tests.test_hatchet_tls import settings

async def fail_start(self):
    raise ValueError('startup-test-private-detail')

Worker._aio_start = fail_start
Worker.register_workflows = lambda self, _: self._action_registry.update({'offline': None})
create_generation_worker(create_hatchet_workflows(settings())).start()
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode != 0
    assert "Windows Hatchet worker startup failed" in result.stderr
    assert "startup-test-private-detail" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows spawned listener bootstrap")
def test_listener_bootstrap_installs_signal_support_in_child_process():
    original_policy = asyncio.get_event_loop_policy()
    script = """
import asyncio
import signal
from app import windows_hatchet_worker as native

def probe(*args):
    async def check():
        loop = asyncio.get_running_loop()
        assert isinstance(loop, native.WindowsSignalLoop)
        loop.add_signal_handler(signal.SIGTERM, lambda: None)
        assert loop.remove_signal_handler(signal.SIGTERM)
    asyncio.run(check())

native.worker_action_listener_process = probe
native.windows_listener_process()
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    assert asyncio.get_event_loop_policy() is original_policy
