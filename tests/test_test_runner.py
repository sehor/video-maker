"""Failure propagation, isolation validation and simulated external operations."""

import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import test_support
from dev import PreflightError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_test_runner", ROOT / "scripts/test.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize("url", [
    "", "sqlite:///test.db", "postgresql+psycopg://u:p@postgres/app_test",
    "postgresql+psycopg://u:p@127.0.0.1/app",
    "postgresql+psycopg://u:p@127.0.0.1/app_test?host=remote",
    "postgresql+psycopg://u:p@127.0.0.1/app_test?options=-csearch_path=public",
])
def test_unsafe_test_database_target_fails_before_connection(monkeypatch, url):
    monkeypatch.setattr(test_support, "dotenv_values", lambda *a, **kw: {})
    monkeypatch.setattr(test_support.os, "environ", {"TEST_DATABASE_URL": url})
    with pytest.raises(PreflightError):
        test_support.test_database_url()


def test_development_database_is_protected_even_when_name_ends_in_test(monkeypatch):
    url = "postgresql+psycopg://u:p@localhost/development_test"
    monkeypatch.setattr(test_support, "dotenv_values", lambda *a, **kw: {"DATABASE_URL": url})
    monkeypatch.setattr(test_support.os, "environ", {"TEST_DATABASE_URL": url})
    with pytest.raises(PreflightError, match="differ"):
        test_support.test_database_url()


def test_test_failure_stops_later_suites_and_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    calls = []

    def failed(*args):
        raise PreflightError("injected failed test")

    monkeypatch.setattr(runner, "python_tests", failed)
    monkeypatch.setattr(runner, "run", lambda *args: calls.append(args))
    monkeypatch.setattr(runner, "e2e", lambda *args: calls.append(args))
    assert runner.main(["all"]) == 1
    assert not calls


def test_unit_entrypoint_does_not_inspect_database(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "test_database_url", lambda: pytest.fail("unexpected DB lookup"))
    calls = []
    monkeypatch.setattr(runner, "run", lambda args, *rest: calls.append(args))
    assert runner.main(["unit"]) == 0
    assert len(calls) == 1
    assert "not database and not live" in calls[0]


@pytest.mark.parametrize("command", [["wsl.exe", "--status"], ["docker", "ps"]])
def test_container_process_is_blocked_before_launch(command):
    with pytest.raises(AssertionError, match="must be simulated"):
        subprocess.Popen(command)


@pytest.mark.parametrize("binary", ["ffmpeg", "ffprobe", "ffmpeg.exe", "ffprobe.exe"])
def test_media_process_is_blocked_before_launch(binary):
    with pytest.raises(AssertionError, match="Media processes must not run"):
        subprocess.Popen([binary, "-version"])


def test_remote_socket_is_blocked_before_connection():
    with socket.socket() as connection, pytest.raises(AssertionError, match="fake transport"):
        connection.connect(("203.0.113.1", 443))


@pytest.mark.parametrize("body", [
    "pytest.skip('not executed')",
    "pytest.xfail('not verified')",
    "assert False",
])
def test_unexecuted_or_failing_test_cannot_report_success(tmp_path, body):
    (tmp_path / "conftest.py").write_text((ROOT / "conftest.py").read_text(), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_probe.py").write_text(f"import pytest\ndef test_probe():\n    {body}\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize("group", ["test", "python", "unit", "media"])
def test_daily_entrypoints_do_not_enable_database(tmp_path, monkeypatch, group):
    calls = []
    monkeypatch.setattr(runner, "run", lambda args, *rest: calls.append(args))
    runner.python_tests(group, {}, tmp_path, [])
    assert "--run-db" not in calls[0]


@pytest.mark.parametrize("group", ["db", "db-full", "all"])
def test_explicit_database_entrypoints_select_requested_scope(tmp_path, monkeypatch, group):
    calls = []
    monkeypatch.setattr(runner, "run", lambda args, *rest: calls.append(args))
    runner.python_tests(group, {}, tmp_path, [])
    assert "--run-db" in calls[0]
    selected_paths = [arg for arg in calls[0] if "::test_" in arg]
    assert selected_paths == (list(runner.DB_SMOKE_TESTS) if group == "db" else [])


@pytest.mark.parametrize("enable_database", [False, True])
def test_pytest_database_collection_requires_explicit_opt_in(tmp_path, enable_database):
    (tmp_path / "conftest.py").write_text((ROOT / "conftest.py").read_text(), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    database: integration test\n", encoding="utf-8"
    )
    (tmp_path / "test_probe.py").write_text(
        "from pathlib import Path\nimport pytest\n"
        "def test_unit(): pass\n"
        "@pytest.mark.database\n"
        "def test_database(): Path('db-ran').touch()\n", encoding="utf-8"
    )
    # This synthetic database-marked test never connects to a database.
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if enable_database:
        args.append("--run-db")
    result = subprocess.run(
        args, cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "db-ran").exists() == enable_database
