"""Inspect legacy jobs; --enable atomically enables required snapshots after draining."""

import argparse
import json
import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from dev import ROOT, database_url, load_environment

TERMINAL_STATUSES = (
    "('SUCCEEDED','FAILED_FINAL','CANCELLED','EXPIRED','REJECTED_POLICY')"
)


def inspect_or_enable(connection: Connection, *, enable: bool = False) -> dict:
    # This must be the first table access in the cutover transaction. It waits for
    # older inserts and prevents inserts between the drain check and trigger DDL.
    if enable:
        connection.execute(text("LOCK TABLE generation_jobs IN ACCESS EXCLUSIVE MODE"))
    state = connection.scalar(
        text(
            "SELECT tgenabled FROM pg_trigger WHERE tgrelid = 'generation_jobs'::regclass "
            "AND tgname = 'generation_job_input_required' AND NOT tgisinternal"
        )
    )
    if state is None:
        raise RuntimeError("Apply migration 0013_job_input_snapshot before cutover")
    active = [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT id, project_id, shot_id, status FROM generation_jobs "
                f"WHERE input_snapshot_json IS NULL AND status NOT IN {TERMINAL_STATUSES} ORDER BY id"
            )
        ).mappings()
    ]
    legacy_terminal_count = connection.scalar(
        text(
            "SELECT count(*) FROM generation_jobs "
            f"WHERE input_snapshot_json IS NULL AND status IN {TERMINAL_STATUSES}"
        )
    )
    if enable and not active:
        connection.execute(
            text(
                "ALTER TABLE generation_jobs ENABLE ALWAYS TRIGGER generation_job_input_required"
            )
        )
        state = "A"
    return {
        "required": state in {"O", "A"},
        "legacy_terminal_count": legacy_terminal_count,
        "active_legacy_jobs": active,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enable", action="store_true", help="Enable after BL-02B and drain"
    )
    args = parser.parse_args()
    values = load_environment(ROOT / ".env", dict(os.environ))
    target = database_url(
        values.get("DATABASE_URL", ""), "DATABASE_URL", "postgresql+psycopg"
    )
    engine = create_engine(target)
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            report = inspect_or_enable(connection, enable=args.enable)
        print(json.dumps(report, default=str, ensure_ascii=False, indent=2))
        return 1 if args.enable and report["active_legacy_jobs"] else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
