import uuid
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.config import get_settings


def alembic_config(database_url: str) -> Config:
    settings = get_settings()
    settings.database_url = database_url
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def only_alembic_head(config: Config) -> str:
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"expected one Alembic head, found {heads}"
    return heads[0]


def test_empty_database_upgrades_to_head(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'empty.db'}"
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    assert {
        "project_assets",
        "shot_references",
        "generation_jobs",
        "generation_batches",
        "generation_attempts",
        "generation_outputs",
        "job_events",
        "quality_tiers",
        "price_versions",
        "generation_quotes",
        "wallet_accounts",
        "wallet_balances",
        "ledger_transactions",
        "ledger_postings",
        "api_idempotency_records",
        "outbox_events",
        "provider_event_inbox",
    } <= tables
    assert {"assets", "jobs", "attempts", "outputs"}.isdisjoint(tables)
    with engine.connect() as connection:
        triggers = set(
            connection.scalars(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        )
    assert {
        "ledger_postings_immutable_update",
        "ledger_postings_immutable_delete",
        "generation_quote_terms_immutable",
        "generation_quote_status_monotonic",
        "generation_attempt_snapshot_immutable",
    } <= triggers
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            only_alembic_head(config)
        )

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
    assert "fk_generation_jobs_batch_identity" in {
        constraint["name"] for constraint in inspector.get_foreign_keys("generation_jobs")
    }
    assert "fk_generation_jobs_batch_reservation" in {
        constraint["name"] for constraint in inspector.get_foreign_keys("generation_jobs")
    }
    assert "uq_generation_jobs_final_output_id" in {
        constraint["name"] for constraint in inspector.get_unique_constraints("generation_jobs")
    }
    assert {"quote_id", "ledger_unit", "reserved_amount_ms", "settlement_status"} <= {
        column["name"] for column in inspector.get_columns("generation_jobs")
    }
    assert {
        "image_digest",
        "worker_version",
        "worker_commit",
        "comfyui_version",
        "comfyui_commit",
        "workflow_version",
        "workflow_hash",
        "model_hashes_json",
        "gpu_type",
        "queue_ms",
        "cold_start_ms",
        "runtime_ms",
        "billable_ms",
        "cost_minor",
        "cost_currency",
        "cost_source",
    } <= {column["name"] for column in inspector.get_columns("generation_attempts")}
    assert {
        "user_id",
        "project_id",
        "status",
        "ledger_unit",
        "reserved_amount_ms",
        "reserved_tx_id",
    } <= {column["name"] for column in inspector.get_columns("generation_batches")}
    assert {
        "uq_generation_batches_identity",
        "uq_generation_batches_reservation_identity",
        "uq_generation_batches_reserved_tx_id",
    } <= {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("generation_batches")
    }
    assert "uq_generation_jobs_standalone_reserved_tx_id" in {
        index["name"] for index in inspector.get_indexes("generation_jobs")
    }
    assert {
        "job_id",
        "attempt_id",
        "idempotency_key",
        "status",
        "attempt_count",
        "locked_at",
        "lock_token",
        "published_at",
    } <= {column["name"] for column in inspector.get_columns("outbox_events")}
    assert {
        "uq_outbox_events_job_event",
        "uq_outbox_events_idempotency_key",
    } <= {
        constraint["name"] for constraint in inspector.get_unique_constraints("outbox_events")
    }
    assert "fk_outbox_events_attempt_job" in {
        constraint["name"] for constraint in inspector.get_foreign_keys("outbox_events")
    }
    assert "ix_outbox_events_attempt_id" in {
        index["name"] for index in inspector.get_indexes("outbox_events")
    }
    assert "ck_outbox_events_cancel_attempt" in {
        constraint["name"]
        for constraint in inspector.get_check_constraints("outbox_events")
    }
    assert {
        "provider_code",
        "external_event_id",
        "provider_job_id",
        "payload_hash",
        "status",
        "attempt_id",
        "job_id",
        "locked_at",
        "lock_token",
        "processed_at",
    } <= {column["name"] for column in inspector.get_columns("provider_event_inbox")}
    assert "uq_provider_event_inbox_external_event" in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("provider_event_inbox")
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM quality_tiers")) == 3
        assert connection.scalar(text("SELECT count(*) FROM price_versions")) == 4
