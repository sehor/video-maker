"""Read-only local source inventory by default; --register records owned legacy artifacts."""

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dev import ROOT, database_url, load_environment

sys.path.insert(0, str(ROOT / "apps/api"))
from app.artifact_lifecycle import inventory_local_sources  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args()
    values = load_environment(ROOT / ".env", dict(os.environ))
    target = database_url(
        values.get("DATABASE_URL", ""), "DATABASE_URL", "postgresql+psycopg"
    )
    root = Path(values.get("STORAGE_ROOT", "./data/storage"))
    if not root.is_absolute():
        root = ROOT / root
    engine = create_engine(target)
    try:
        print(
            json.dumps(
                inventory_local_sources(
                    root, sessionmaker(engine), apply=args.register
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
