"""Test infrastructure: existing native PostgreSQL, disposable schemas, no service startup."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from dev import PreflightError, database_url

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps/api"


def test_database_url() -> URL:
    # Read only test/development URLs; never inherit provider credentials from .env.
    file_values = dotenv_values(ROOT / ".env", interpolate=False)
    values = {**file_values, **os.environ}
    url = database_url(values.get("TEST_DATABASE_URL", ""),
                       "TEST_DATABASE_URL", "postgresql+psycopg")
    if not url.database.endswith("_test"):
        raise PreflightError("TEST_DATABASE_URL must name an existing database ending in _test")
    for development_url in (file_values.get("DATABASE_URL"), os.environ.get("DATABASE_URL")):
        if not development_url:
            continue
        from sqlalchemy.engine import make_url

        development = make_url(development_url)
        if url.database == development.database:
            raise PreflightError("TEST_DATABASE_URL must differ from the development database")
    return url


@contextmanager
def isolated_schema(url: URL):
    """Own only a fresh schema; never drop a database or touch its public schema."""
    schema = "test_" + uuid.uuid4().hex
    admin = create_engine(url, connect_args={
        "connect_timeout": 5, "passfile": os.devnull,
        "hostaddr": "::1" if url.host == "::1" else "127.0.0.1",
        "options": "-cstatement_timeout=30000",
    })
    created = False
    try:
        with admin.begin() as connection:
            version = connection.scalar(text("SELECT version()"))
            if os.name == "nt" and not any(word in version for word in ("Visual C++", "mingw")):
                raise PreflightError("Tests require Windows PostgreSQL, not a forwarded server")
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        # Keep real commits/locks/constraints without waiting for disk fsync on every
        # fixture write. Database power-loss durability is not an application test.
        yield url.update_query_dict({
            "options": f"-csearch_path={schema} -csynchronous_commit=off -cstatement_timeout=30000"
        })
    finally:
        try:
            if created:
                with admin.begin() as connection:
                    connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            admin.dispose()


def migrate(url: URL, revision: str = "head"):
    from alembic import command
    from alembic.config import Config
    from app.config import get_settings

    settings = get_settings()
    original = settings.database_url
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "alembic"))
    try:
        settings.database_url = url.render_as_string(hide_password=False)
        command.upgrade(config, revision)
    finally:
        settings.database_url = original
    return config
