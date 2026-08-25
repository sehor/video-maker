import uuid
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from alembic import command
from alembic.config import Config
from app.config import get_settings


def alembic_config(database_url: str) -> Config:
    settings = get_settings()
    settings.database_url = database_url
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_empty_database_upgrades_to_head(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'empty.db'}"
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    tables = set(inspect(create_engine(database_url)).get_table_names())
    assert {
        "project_assets",
        "shot_references",
        "generation_jobs",
        "generation_attempts",
        "generation_outputs",
        "job_events",
    } <= tables
    assert {"assets", "jobs", "attempts", "outputs"}.isdisjoint(tables)

    command.downgrade(config, "0001_stage_one")
    downgraded_tables = set(inspect(create_engine(database_url)).get_table_names())
    assert {"assets", "jobs", "attempts", "outputs"} <= downgraded_tables
    command.upgrade(config, "head")


def test_stage_one_database_upgrades_destructively_and_keeps_projects_and_shots(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'stage-one.db'}"
    config = alembic_config(database_url)
    command.upgrade(config, "0001_stage_one")
    engine = create_engine(database_url)
    user_id = uuid.uuid4().hex
    project_id = uuid.uuid4().hex
    shot_id = uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO app_users (id, auth_subject) VALUES (:id, 'stage-one-owner')"),
            {"id": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO projects (id, owner_id, name) "
                "VALUES (:id, :owner_id, 'stage-one-project')"
            ),
            {"id": project_id, "owner_id": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO shots "
                "(id, project_id, title, prompt, duration_seconds, aspect_ratio) "
                "VALUES (:id, :project_id, 'stage-one-shot', 'prompt', 2, '16:9')"
            ),
            {"id": shot_id, "project_id": project_id},
        )

    command.upgrade(config, "head")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"assets", "jobs", "attempts", "outputs"}.isdisjoint(tables)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM projects")) == 1
        assert connection.scalar(text("SELECT count(*) FROM shots")) == 1
    assert "fk_generation_jobs_final_output" in {
        constraint["name"] for constraint in inspector.get_foreign_keys("generation_jobs")
    }
    assert "uq_generation_jobs_final_output_id" in {
        constraint["name"] for constraint in inspector.get_unique_constraints("generation_jobs")
    }
