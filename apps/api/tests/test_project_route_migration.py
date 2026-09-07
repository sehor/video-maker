import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.routing import MOCK_ROUTE, SIMULATED_RUNPOD_ROUTE
from tests.test_input_snapshot_migration import insert_job, seed_owner
from tests.test_migrations import alembic_config

pytestmark = pytest.mark.database


def test_project_route_migration_classifies_history_without_rewriting_jobs(migration_database):
    url, engine = migration_database
    config = alembic_config(url)
    command.upgrade(config, "0013_job_input_snapshot")
    with engine.begin() as db:
        same, mixed, unknown, empty = [seed_owner(db) for _ in range(4)]
        jobs = []
        for owner, candidate in [
            (same, MOCK_ROUTE.candidate_id),
            (mixed, MOCK_ROUTE.candidate_id),
            (mixed, SIMULATED_RUNPOD_ROUTE.candidate_id),
            (unknown, uuid.uuid4()),
        ]:
            job = insert_job(db, owner)
            db.execute(
                text(
                    "UPDATE generation_jobs SET selected_route_candidate_id=:candidate WHERE id=:id"
                ),
                {"candidate": candidate, "id": job},
            )
            jobs.append((job, candidate))
    command.upgrade(config, "0014_project_route_binding")
    with engine.begin() as db:
        for owner, state in [
            (same, "PROVISIONAL"),
            (mixed, "REVIEW"),
            (unknown, "REVIEW"),
            (empty, "UNBOUND"),
        ]:
            assert (
                db.scalar(
                    text("SELECT route_binding_status FROM projects WHERE id=:id"),
                    {"id": owner["project"]},
                )
                == state
            )
        for job, candidate in jobs:
            assert (
                db.scalar(
                    text("SELECT selected_route_candidate_id FROM generation_jobs WHERE id=:id"),
                    {"id": job},
                )
                == candidate
            )
        with pytest.raises(IntegrityError, match="immutable"), db.begin_nested():
            db.execute(text("UPDATE generation_route_versions SET definition_json='{}'"))
        with pytest.raises(IntegrityError, match="immutable"), db.begin_nested():
            db.execute(
                text("UPDATE projects SET route_candidate_id=:candidate WHERE id=:id"),
                {"candidate": SIMULATED_RUNPOD_ROUTE.candidate_id, "id": same["project"]},
            )
    with pytest.raises(RuntimeError, match="Cannot discard"):
        command.downgrade(config, "0013_job_input_snapshot")


def test_empty_route_migration_can_roll_back(migration_database):
    url, engine = migration_database
    config = alembic_config(url)
    command.upgrade(config, "0014_project_route_binding")
    command.downgrade(config, "0013_job_input_snapshot")
    command.upgrade(config, "0014_project_route_binding")
    with engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM generation_route_versions")) == 2
