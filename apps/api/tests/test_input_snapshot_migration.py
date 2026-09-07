import json
import uuid

import pytest
from job_input_snapshot_cutover import inspect_or_enable
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_input_snapshot import snapshot_payload
from test_migrations import alembic_config

from alembic import command
from app.models import GenerationJob

pytestmark = pytest.mark.database
PREVIOUS = "0012_optional_media_hashes"
REVISION = "0013_job_input_snapshot"
MISSING = object()


def seed_owner(connection):
    owner, project, shot = [uuid.uuid4() for _ in range(3)]
    connection.execute(
        text("INSERT INTO app_users (id, auth_subject) VALUES (:id, :subject)"),
        {"id": owner, "subject": str(owner)},
    )
    connection.execute(
        text("INSERT INTO projects (id, owner_id, name) VALUES (:id, :owner, 'snapshot project')"),
        {"id": project, "owner": owner},
    )
    connection.execute(
        text(
            "INSERT INTO shots (id, project_id, title, prompt, duration_seconds, aspect_ratio) "
            "VALUES (:id, :project, 'shot', 'current edited shot, not historical input', 2, '16:9')"
        ),
        {"id": shot, "project": project},
    )
    return {"owner": owner, "project": project, "shot": shot}


def insert_job(connection, owner, *, snapshot=MISSING, status="CREATED"):
    job_id = uuid.uuid4()
    column = "" if snapshot is MISSING else ", input_snapshot_json"
    value = "" if snapshot is MISSING else ", CAST(:snapshot AS json)"
    connection.execute(
        text(
            "INSERT INTO generation_jobs "
            "(id, user_id, project_id, shot_id, tier_code, duration_ms, "
            "resolution, aspect_ratio, variant_index, quote_snapshot_json, status, mock_mode"
            f"{column}) VALUES (:id, :owner, :project, :shot, 'FAST', 2000, '720P', '16:9', 0, "
            f"'{{\"historical_price\": true}}', :status, 'success'{value})"
        ),
        {
            **owner,
            "id": job_id,
            "status": status,
            "snapshot": None if snapshot is MISSING else json.dumps(snapshot),
        },
    )
    return job_id


def test_empty_upgrade_and_non_destructive_downgrade(migration_database):
    url, engine = migration_database
    config = alembic_config(url)
    command.upgrade(config, REVISION)
    assert next(
        c
        for c in inspect(engine).get_columns("generation_jobs")
        if c["name"] == "input_snapshot_json"
    )["nullable"]
    with engine.begin() as connection:
        assert inspect_or_enable(connection) == {
            "required": False,
            "legacy_terminal_count": 0,
            "active_legacy_jobs": [],
        }
    command.downgrade(config, PREVIOUS)
    assert "input_snapshot_json" not in {
        c["name"] for c in inspect(engine).get_columns("generation_jobs")
    }
    command.upgrade(config, REVISION)


def test_existing_jobs_upgrade_without_backfill_or_balance_changes(migration_database):
    url, engine = migration_database
    config = alembic_config(url)
    command.upgrade(config, PREVIOUS)
    with engine.begin() as connection:
        owner = seed_owner(connection)
        terminal = insert_job(connection, owner, status="SUCCEEDED")
        active = insert_job(connection, owner, status="CANCEL_REQUESTED")
    command.upgrade(config, REVISION)
    with Session(engine) as session:
        jobs = session.scalars(select(GenerationJob).order_by(GenerationJob.id)).all()
        assert {j.id for j in jobs} == {terminal, active}
        assert all(j.input_snapshot_json is None for j in jobs)
        assert all(j.quote_snapshot_json == {"historical_price": True} for j in jobs)
    with engine.begin() as connection:
        report = inspect_or_enable(connection, enable=True)
        assert not report["required"]
        assert report["legacy_terminal_count"] == 1
        assert [row["id"] for row in report["active_legacy_jobs"]] == [active]
        assert connection.scalar(text("SELECT count(*) FROM ledger_transactions")) == 0
        assert (
            connection.scalar(
                text("SELECT status FROM generation_jobs WHERE id = :id"), {"id": active}
            )
            == "CANCEL_REQUESTED"
        )
        with pytest.raises(IntegrityError, match="immutable"), connection.begin_nested():
            connection.execute(
                text(
                    "UPDATE generation_jobs SET input_snapshot_json = CAST(:snapshot AS json) "
                    "WHERE id = :id"
                ),
                {"id": terminal, "snapshot": json.dumps(snapshot_payload())},
            )
    command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM generation_jobs")) == 2


def test_snapshot_validation_and_immutability_at_database_boundary(migration_database):
    url, engine = migration_database
    config = alembic_config(url)
    command.upgrade(config, REVISION)
    snapshot = snapshot_payload()
    with engine.begin() as connection:
        owner = seed_owner(connection)
        job = insert_job(connection, owner, snapshot=snapshot)
        invalid = [
            None,
            {},
            [],
            {**snapshot, "version": 2},
            {**snapshot, "version": True},
            {**snapshot, "version": 1.0},
            {**snapshot, "duration_ms": 2000.0},
            {**snapshot, "duration_ms": 3000},
            {**snapshot, "mode": "timeout"},
            {**snapshot, "references": None},
            {**snapshot, "prompt": None},
            {**snapshot, "references": snapshot["references"] * 2},
            {**snapshot, "extra": 1},
        ]
        for field in snapshot:
            missing_field = snapshot.copy()
            del missing_field[field]
            invalid.append(missing_field)
        for key, value in [
            ("object_key", "../escape"),
            ("object_key", "a\\b"),
            ("reference_role", "STYLE"),
            ("asset_id", "not-a-uuid"),
        ]:
            invalid.append({**snapshot, "references": [{**snapshot["references"][0], key: value}]})
        for value in invalid:
            with pytest.raises(IntegrityError), connection.begin_nested():
                insert_job(connection, owner, snapshot=value)
        for value in [None, {**snapshot, "prompt": "changed"}]:
            with pytest.raises(IntegrityError, match="immutable"), connection.begin_nested():
                connection.execute(
                    text(
                        "UPDATE generation_jobs SET input_snapshot_json = CAST(:snapshot AS json) "
                        "WHERE id = :id"
                    ),
                    {"snapshot": json.dumps(value), "id": job},
                )
        with pytest.raises(IntegrityError, match="immutable"), connection.begin_nested():
            connection.execute(
                text("UPDATE generation_jobs SET input_snapshot_json = NULL WHERE id = :id"),
                {"id": job},
            )
        with pytest.raises(IntegrityError, match="invalid job input"), connection.begin_nested():
            connection.execute(
                text("UPDATE generation_jobs SET duration_ms = 3000 WHERE id = :id"), {"id": job}
            )
        connection.execute(
            text("UPDATE generation_jobs SET status = 'RUNNING' WHERE id = :id"), {"id": job}
        )
    with Session(engine) as session:
        assert session.get(GenerationJob, job).input_snapshot_json == snapshot
        # Existing application constructors omit the new field; Python None must
        # also persist as SQL NULL, not the invalid JSON literal null.
        for optional_input in ({}, {"input_snapshot_json": None}):
            legacy = GenerationJob(
                user_id=owner["owner"],
                project_id=owner["project"],
                shot_id=owner["shot"],
                duration_ms=2000,
                aspect_ratio="16:9",
                **optional_input,
            )
            session.add(legacy)
            session.commit()
            assert legacy.input_snapshot_json is None
            assert session.scalar(
                text("SELECT input_snapshot_json IS NULL FROM generation_jobs WHERE id = :id"),
                {"id": legacy.id},
            )
    with pytest.raises(RuntimeError, match="Cannot drop input snapshots"):
        command.downgrade(config, PREVIOUS)


def test_cutover_requires_snapshots_but_keeps_legacy_terminal_readable(migration_database):
    url, engine = migration_database
    config = alembic_config(url)
    command.upgrade(config, REVISION)
    with engine.begin() as connection:
        owner = seed_owner(connection)
        legacy = insert_job(connection, owner, status="FAILED_FINAL")
        nested = connection.begin_nested()
        assert inspect_or_enable(connection, enable=True)["required"]
        nested.rollback()
        assert not inspect_or_enable(connection)["required"]
        assert inspect_or_enable(connection, enable=True)["required"]
    with engine.begin() as connection:
        assert inspect_or_enable(connection, enable=True)["required"]
        for status in ("CREATED", "SUCCEEDED"):
            with pytest.raises(IntegrityError, match="required"), connection.begin_nested():
                insert_job(connection, owner, status=status)
        with pytest.raises(IntegrityError, match="required"), connection.begin_nested():
            connection.execute(
                text("UPDATE generation_jobs SET status = 'QUEUED' WHERE id = :id"), {"id": legacy}
            )
        connection.execute(
            text(
                "UPDATE generation_jobs SET error_message = 'legacy remains readable' "
                "WHERE id = :id"
            ),
            {"id": legacy},
        )
    # Activation alone blocks destructive rollback, even before the first snapshot.
    with pytest.raises(RuntimeError, match="Cannot drop input snapshots"):
        command.downgrade(config, PREVIOUS)
    with Session(engine) as session:
        assert session.get(GenerationJob, legacy).input_snapshot_json is None
        job = GenerationJob(
            **{
                "user_id": owner["owner"],
                "project_id": owner["project"],
                "shot_id": owner["shot"],
                "duration_ms": 2000,
                "aspect_ratio": "16:9",
                "input_snapshot_json": snapshot_payload(),
            }
        )
        session.add(job)
        session.commit()
        assert job.input_snapshot_json["version"] == 1


def test_cutover_serializes_with_uncommitted_legacy_insert(migration_database):
    url, engine = migration_database
    command.upgrade(alembic_config(url), REVISION)
    with engine.begin() as connection:
        owner = seed_owner(connection)
    with engine.begin() as writer:
        job = insert_job(writer, owner)
        with engine.begin() as cutover:
            cutover.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(DBAPIError) as raised, cutover.begin_nested():
                inspect_or_enable(cutover, enable=True)
            assert raised.value.orig.sqlstate == "55P03"
    with engine.begin() as connection:
        report = inspect_or_enable(connection, enable=True)
        assert not report["required"]
        assert [row["id"] for row in report["active_legacy_jobs"]] == [job]
