"""Repository-wide collection and external-operation policy."""

import json
import os
import re
import socket
import subprocess
from pathlib import Path

import pytest

PHASE_TIMES = pytest.StashKey[list[dict]]()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    item.config.stash.setdefault(PHASE_TIMES, []).append({
        "nodeid": report.nodeid,
        "phase": report.when,
        "seconds": report.duration,
        "outcome": report.outcome,
        "database": dict(report.user_properties),
    })


def pytest_addoption(parser):
    parser.addoption("--run-db", action="store_true", help="Explicit database integration tests")
    parser.addoption("--run-live", action="store_true", help="Explicit remote verification")


def pytest_collection_modifyitems(config, items):
    selected, excluded = [], []
    for item in items:
        inapplicable = (item.get_closest_marker("posix") and os.name == "nt") or (
            item.get_closest_marker("windows") and os.name != "nt"
        )
        live = item.get_closest_marker("live")
        database = item.get_closest_marker("database")
        if (inapplicable or (live and not config.getoption("--run-live"))
                or (database and not config.getoption("--run-db"))):
            excluded.append(item)
        else:
            selected.append(item)
    if excluded:
        config.hook.pytest_deselected(items=excluded)
    items[:] = selected


@pytest.fixture(autouse=True)
def external_operations(monkeypatch, request):
    """Mock external adapters; accidental container commands fail before process creation."""
    original_popen = subprocess.Popen
    original_connect = socket.socket.connect

    def guarded_popen(args, *positional, **kwargs):
        command = args if isinstance(args, str) else " ".join(map(str, args))
        if re.search(r"(?i)(?<![\w-])(?:wsl(?:\.exe)?|docker(?:\.exe)?)(?![\w-])", command):
            raise AssertionError("External container operations must be simulated in tests")
        if re.search(r"(?i)(?<![\w-])(?:ffmpeg|ffprobe)(?:\.exe)?(?![\w-])", command):
            raise AssertionError("Media processes must not run in automated tests")
        return original_popen(args, *positional, **kwargs)

    def guarded_connect(sock, address):
        if (not request.node.get_closest_marker("live") and isinstance(address, tuple)
                and address[0] not in {"127.0.0.1", "localhost", "::1"}):
            raise AssertionError("External network access must use a fake transport")
        return original_connect(sock, address)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def pytest_sessionfinish(session, exitstatus):
    runtime = os.environ.get("TEST_RUNTIME_ROOT")
    if runtime:
        target = Path(runtime).parent / "python-phases.json"
        target.write_text(json.dumps(session.config.stash.get(PHASE_TIMES, []), indent=2),
                          encoding="utf-8")
    # Explicit skips/xfails are not proof. Platform/live exclusions happen at collection.
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter and any(reporter.stats.get(key) for key in ("skipped", "xfailed", "xpassed")):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
