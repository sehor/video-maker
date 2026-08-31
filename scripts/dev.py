"""Native development commands. No services, dependencies or databases are installed here."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps/api"
WEB = ROOT / "apps/web"


class PreflightError(Exception):
    pass


def load_environment(path: Path, inherited: dict[str, str]) -> dict[str, str]:
    if not path.is_file():
        raise PreflightError("Environment file missing; copy .env.example to .env and configure it")
    # No interpolation: passwords containing $ must remain literal. Shell variables take priority.
    values = dotenv_values(path, interpolate=False, encoding="utf-8-sig")
    env = {**{key: value for key, value in values.items() if value is not None}, **inherited}
    if env.get("ENVIRONMENT", "development") != "development":
        raise PreflightError("These commands require ENVIRONMENT=development")
    storage = Path(env.get("STORAGE_ROOT", "./data/storage"))
    env["STORAGE_ROOT"] = str((ROOT / storage).resolve())
    env.setdefault("UV_CACHE_DIR", str(API / ".uv-cache"))
    env.setdefault("UV_NO_PYTHON_DOWNLOADS", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("E2E_BASE_URL", "http://localhost:3000")
    return env


def database_url(value: str, key: str, driver: str) -> URL:
    try:
        url = make_url(value)
        if url.drivername != driver:
            raise ValueError
        if not url.username or url.password is None or not url.database:
            raise ValueError
        if url.host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError
        if not 1 <= (url.port or 5432) <= 65535:
            raise ValueError
        # libpq query parameters can override the visible authority/database/credentials.
        if set(url.query) - {"sslmode"}:
            raise ValueError
    except (ValueError, TypeError, SQLAlchemyError):
        raise PreflightError(
            f"{key}: expected {driver}://USER:PASSWORD@localhost:5432/DB; "
            "an empty password is USER:@, special characters need percent-encoding. "
            "Only the optional sslmode query parameter is supported."
        ) from None
    return url


def validate_database_pair(env: dict[str, str]) -> URL:
    api = database_url(env.get("DATABASE_URL", ""), "DATABASE_URL", "postgresql+psycopg")
    auth = database_url(
        env.get("BETTER_AUTH_DATABASE_URL", ""), "BETTER_AUTH_DATABASE_URL", "postgresql"
    )
    if api.set(drivername="postgresql", port=api.port or 5432) != auth.set(port=auth.port or 5432):
        raise PreflightError("API and Better Auth URLs must have the same target and credentials")
    for key, actual in (
        ("POSTGRES_USER", api.username), ("POSTGRES_PASSWORD", api.password),
        ("POSTGRES_DB", api.database),
    ):
        if key in env and env[key] != actual:
            raise PreflightError(f"{key} disagrees with the database URLs; update the full URLs")
    return api


def validate_test_database(env: dict[str, str], development: URL) -> URL:
    url = database_url(
        env.get("TEST_DATABASE_URL", ""), "TEST_DATABASE_URL", "postgresql+psycopg"
    )
    if not url.database.endswith("_test") or url.database == development.database:
        raise PreflightError("Tests require a separate, existing database ending in _test")
    return url


def run(args: list[str], env: dict[str, str], cwd: Path = ROOT, *, capture=False) -> str:
    executable = shutil.which(args[0], path=env.get("PATH"))
    if executable is None:
        raise PreflightError(f"Missing executable: {args[0]}; install the documented prerequisite")
    # No shell, no credential-bearing command arguments. The foreground child owns its lifetime.
    result = subprocess.run(
        [executable, *args[1:]], cwd=cwd, env=env, check=False,
        capture_output=capture, text=True, encoding="utf-8", errors="replace",
        timeout=20 if capture else None,
    )
    if result.returncode:
        raise PreflightError(f"{args[0]} command failed ({result.returncode})")
    return result.stdout.strip() if capture else ""


def uv(args: list[str], env: dict[str, str]) -> None:
    run(["uv", "run", "--no-sync", *args], env, API)


def pnpm(args: list[str], env: dict[str, str]) -> None:
    run(["pnpm", *args], env)


def inspect_database(url: URL) -> tuple[tuple[str, ...], tuple[str, ...], set[str]]:
    print(f"PostgreSQL target: {url.host}:{url.port or 5432}/{url.database}", flush=True)
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "alembic"))
    expected = tuple(ScriptDirectory.from_config(config).get_heads())
    engine = create_engine(url, connect_args={
        "connect_timeout": 3,
        "options": "-c default_transaction_read_only=on -c statement_timeout=5000",
        "passfile": os.devnull,
    })
    try:
        with engine.connect() as connection:
            print(f"PostgreSQL version: {connection.scalar(text('SHOW server_version'))}")
            current = tuple(MigrationContext.configure(connection).get_current_heads())
            tables = set(inspect(connection).get_table_names())
    except SQLAlchemyError as exc:
        # Do not guess credentials are wrong for network, schema or driver failures.
        code = getattr(getattr(exc, "orig", None), "sqlstate", None)
        raise PreflightError(
            f"PostgreSQL connection/query failed ({type(exc).__name__}, SQLSTATE={code}); "
            "check instance, port and database after checking the URL; credentials may also matter"
        ) from None
    finally:
        engine.dispose()
    print(f"Alembic current={current or 'unmigrated'}, expected={expected}", flush=True)
    return current, expected, tables


def migration_guard(current: tuple[str, ...], tables: set[str]) -> None:
    auth_tables = {"user", "session", "account", "verification", "jwks"}
    if not current and tables - auth_tables - {"alembic_version"}:
        raise PreflightError(
            "Unversioned database contains tables; inspect/backup before migrating"
        )
    if current == ("0001_stage_one",):
        raise PreflightError("Stage-one upgrade drops legacy tables; backup and migrate manually")


def auth_check(env: dict[str, str], *, allow_unmigrated=False) -> None:
    args = ["node", str(WEB / "scripts/check-database.mjs")]
    if allow_unmigrated:
        args.append("--allow-unmigrated")
    run(args, env)


def check_tools(env: dict[str, str]) -> None:
    print(f"Python {sys.version.split()[0]}")
    for tool, args in (
        ("uv", ["--version"]), ("node", ["--version"]), ("pnpm", ["--version"]),
        (env.get("FFMPEG_BINARY", "ffmpeg"), ["-version"]),
        (env.get("FFPROBE_BINARY", "ffprobe"), ["-version"]),
    ):
        version = run([tool, *args], env, capture=True).splitlines()[0]
        print(f"{Path(tool).name}: {version}")
        if tool == "node" and int(version.lstrip("v").split(".")[0]) < 22:
            raise PreflightError("Node 22+ is required")
        if tool == "pnpm" and version != "9.12.3":
            raise PreflightError("Use the repository's pnpm 9.12.3")
    if not (WEB / "node_modules").is_dir():
        raise PreflightError("Web dependencies missing; run pnpm install --frozen-lockfile")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("task", choices=[
        "check", "migrate", "api", "web", "worker", "test", "test-api", "test-web",
        "e2e", "lint", "typecheck", "build", "generate-client",
    ])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        env = load_environment(args.env_file.resolve(), dict(os.environ))
        for key in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "PGPASSFILE"):
            env.pop(key, None)
            os.environ.pop(key, None)
        env["PGPASSFILE"] = os.devnull
        url = validate_database_pair(env)
        # Validate Settings without importing the API or initializing background workers.
        sys.path.insert(0, str(API))
        from app.config import Settings

        Settings(_env_file=None, **{key.lower(): value for key, value in env.items()})
        if args.task in {"check", "migrate"}:
            check_tools(env)
            current, expected, tables = inspect_database(url)
            auth_check(env, allow_unmigrated=args.task == "migrate")
            if args.task == "migrate":
                migration_guard(current, tables)
                uv(["alembic", "upgrade", "head"], env)
                pnpm(["--filter", "@video-factory/web", "auth:migrate"], env)
                current, expected, _ = inspect_database(url)
                auth_check(env)
            if len(expected) != 1 or current != expected:
                raise PreflightError("Database is not at Alembic head; run scripts/dev.ps1 migrate")
            print("Native development checks passed; no services were installed or restarted.")
        elif args.task == "api":
            uv(["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000",
                "--reload", *args.args], env)
        elif args.task == "web":
            pnpm(["--filter", "@video-factory/web", "dev", "--host", "127.0.0.1",
                  "--port", "3000", *args.args], env)
        elif args.task == "worker":
            if env.get("WORKFLOW_BACKEND", "local") != "hatchet":
                raise PreflightError("worker requires WORKFLOW_BACKEND=hatchet; local needs none")
            uv(["python", "-m", "app.worker", *args.args], env)
        elif args.task in {"test", "test-api"}:
            test_url = validate_test_database(env, url)
            inspect_database(test_url)  # Never create or drop a database automatically.
            runtime_parent = API / ".test-tmp-native"
            runtime_parent.mkdir(exist_ok=True)
            runtime = Path(tempfile.mkdtemp(prefix="run-", dir=runtime_parent))
            env["TEST_RUNTIME_ROOT"] = str(runtime / "data")
            # A fresh directory avoids stale Windows ACLs and pytest deleting a shared temp root.
            uv(["pytest", "-q", "--tb=short", "--basetemp", str(runtime / "pytest"),
                "-o", f"cache_dir={runtime / 'cache'}", *args.args], env)
            if args.task == "test":
                pnpm(["test"], env)
        elif args.task == "test-web":
            pnpm(["test", *args.args], env)
        elif args.task == "e2e":
            target = urlsplit(env["E2E_BASE_URL"])
            if target.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise PreflightError("Native E2E requires a local E2E_BASE_URL and running API/Web")
            pnpm(["test:e2e", *args.args], env)
        elif args.task == "lint":
            uv(["ruff", "check", "app", "tests", "../../scripts/dev.py"], env)
            pnpm(["lint"], env)
        elif args.task == "generate-client":
            uv(["python", "scripts/export_openapi.py"], env)
            pnpm(["generate:client"], env)
        else:
            pnpm([args.task, *args.args], env)
    except (PreflightError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        # ValidationError may contain arbitrary settings; only our own errors are safe to display.
        print(f"ERROR: {exc if isinstance(exc, PreflightError) else type(exc).__name__}",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
