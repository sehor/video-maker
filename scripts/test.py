"""Unified native test runner. No automatic dependency installation or service startup."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.request import urlopen

from dev import PreflightError, run
from test_support import API, ROOT, isolated_schema, migrate, test_database_url


def environment(runtime: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("HATCHET_", "RUNPOD_", "PG"))}
    env.update({
        "ENVIRONMENT": "development", "WORKFLOW_BACKEND": "local",
        "GENERATION_ROUTE_VERSION": "mock_video_v1", "RUNPOD_PROVIDER_ENABLED": "false",
        "TEST_RUNTIME_ROOT": str(runtime / "data"), "STORAGE_ROOT": str(runtime / "storage"),
        "UV_CACHE_DIR": str(API / ".uv-cache"), "UV_NO_PYTHON_DOWNLOADS": "1",
        "PYTHONUNBUFFERED": "1", "PGPASSFILE": os.devnull,
        "OUTBOX_DISPATCHER_ENABLED": "false", "RECONCILER_ENABLED": "false",
        "STORAGE_CLAIM_SECRET": "test-storage-claim-secret-at-least-32-bytes",
        "PROVIDER_CALLBACK_CLAIM_SECRET": "test-provider-callback-secret-at-least-32-bytes",
    })
    return env


DB_SMOKE_TESTS = (
    "apps/api/tests/test_projects_permissions.py::test_project_and_shot_crud",
    "apps/api/tests/test_projects_permissions.py::test_other_user_cannot_access_project_or_shot",
    "apps/api/tests/test_uploads.py::test_upload_and_private_download",
    "apps/api/tests/test_mock_jobs.py::test_mock_success_produces_playable_mp4",
)


def python_tests(group: str, env: dict[str, str], runtime: Path, extra: list[str]) -> None:
    selection = {"unit": "not database and not live",
                 "db": "database and not live", "db-full": "database and not live",
                 "media": "media and not live"}
    args = [sys.executable, "-m", "pytest", "-q", "--basetemp", str(runtime / "pytest"),
            "-o", f"cache_dir={runtime / 'cache'}", f"--junitxml={runtime / 'python.xml'}"]
    if group in {"db", "db-full", "all"}:
        args += ["--run-db"]
    if group == "db":
        args += list(DB_SMOKE_TESTS)
    if group in selection:
        args += ["-m", selection[group]]
    run([*args, *extra], env)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def start(stack: ExitStack, args: list[str], env: dict[str, str], log: Path):
    output = stack.enter_context(log.open("w", encoding="utf-8"))
    process = subprocess.Popen(
        args, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    stack.callback(stop, process)
    return process


def wait_ready(url: str, processes: list[subprocess.Popen], timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise PreflightError("Test server exited; inspect the run's api.log/web.log")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise PreflightError("Test server readiness deadline exceeded; inspect run logs")


def e2e(env: dict[str, str], runtime: Path, extra: list[str]) -> None:
    # Fresh database schema and ports protect existing development data and processes.
    with isolated_schema(test_database_url()) as url, ExitStack() as stack:
        api_port, web_port = free_port(), free_port()
        while web_port == api_port:
            web_port = free_port()
        api_base, web_base = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{web_port}"
        env = {**env, "DATABASE_URL": url.render_as_string(hide_password=False),
               "BETTER_AUTH_DATABASE_URL": url.set(drivername="postgresql").render_as_string(
                   hide_password=False),
               "BETTER_AUTH_SECRET": "e2e-only-auth-secret-at-least-32-characters",
               "BETTER_AUTH_URL": web_base, "AUTH_ISSUER": web_base,
               "AUTH_JWKS_URL": f"{web_base}/api/auth/jwks", "AUTH_AUDIENCE": "video-factory-api",
               "CORS_ORIGINS": web_base, "NUXT_PUBLIC_API_BASE": api_base,
               "NUXT_PUBLIC_AUTH_BASE_URL": web_base, "E2E_BASE_URL": web_base,
               "E2E_MANAGED": "1", "NITRO_HOST": "127.0.0.1", "NITRO_PORT": str(web_port),
               "OUTBOX_DISPATCHER_ENABLED": "true", "PYTHONPATH": str(API),
               "GENERATION_ROUTE_VERSION": "runpod_simulated_v1", "RUNPOD_SIMULATOR_ENABLED": "true"}
        # Migrations use the same isolated URL as both servers.
        sys.path.insert(0, str(API))
        migrate(url)
        run(["pnpm", "--filter", "@video-factory/web", "auth:migrate"], env)
        run(["pnpm", "--filter", "@video-factory/web", "build"], env)
        api = start(stack, [sys.executable, "-m", "uvicorn", "app.main:app", "--host",
                           "127.0.0.1", "--port", str(api_port)], env, runtime / "api.log")
        node = shutil.which("node")
        if node is None:
            raise PreflightError("Node.js is required")
        web = start(stack, [node, str(ROOT / "apps/web/.output/server/index.mjs")],
                    env, runtime / "web.log")
        wait_ready(f"{api_base}/readyz", [api, web])
        wait_ready(f"{web_base}/login", [api, web])
        run(["pnpm", "--filter", "@video-factory/web", "test:e2e:browser", *extra], env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=["test", "all", "python", "unit", "db", "db-full", "media", "web", "e2e"],
                        default="test", nargs="?")
    args, extra = parser.parse_known_args(argv)
    parent = ROOT / ".test-runs"
    parent.mkdir(exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix=f"{args.group}-", dir=parent))
    env = environment(runtime)
    print(f"Test artifacts: {runtime}", flush=True)
    try:
        if args.group in {"test", "all", "python", "unit", "db", "db-full", "media"}:
            python_tests(args.group, env, runtime, extra)
        if args.group in {"test", "all", "web"}:
            run(["pnpm", "--filter", "@video-factory/web", "test"], env)
        if args.group in {"all", "e2e"}:
            e2e(env, runtime, extra if args.group == "e2e" else [])
    except (PreflightError, OSError, ValueError) as exc:
        print(f"FAILED: {exc if isinstance(exc, PreflightError) else type(exc).__name__}",
              file=sys.stderr)
        return 1
    print(f"PASS: {args.group} (only the selected suites were executed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
