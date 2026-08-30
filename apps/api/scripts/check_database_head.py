from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))

    expected_heads = tuple(ScriptDirectory.from_config(config).get_heads())
    if len(expected_heads) != 1:
        print(f"Expected one Alembic head, found: {expected_heads}")
        return 1

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required to check the database migration head")
        return 1

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            current_heads = tuple(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()

    if current_heads != expected_heads:
        print(f"Database heads {current_heads} do not match Alembic heads {expected_heads}")
        return 1

    print(f"Database is at the unique Alembic head: {expected_heads[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
