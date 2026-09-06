"""persist immutable attempt cost and provenance snapshots

Revision ID: 0008_attempt_provenance
Revises: 0007_batch_atomic_reservation
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_attempt_provenance"
down_revision: str | None = "0007_batch_atomic_reservation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SNAPSHOT_COLUMNS = (
    "workflow_version",
    "worker_version",
    "image_digest",
    "worker_commit",
    "comfyui_version",
    "comfyui_commit",
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
    "raw_metrics_json",
)


def upgrade() -> None:
    with op.batch_alter_table("generation_attempts") as batch_op:
        batch_op.add_column(sa.Column("image_digest", sa.String(80)))
        batch_op.add_column(sa.Column("worker_commit", sa.String(64)))
        batch_op.add_column(sa.Column("comfyui_version", sa.String(100)))
        batch_op.add_column(sa.Column("comfyui_commit", sa.String(64)))
        batch_op.add_column(sa.Column("workflow_hash", sa.String(64)))
        batch_op.add_column(sa.Column("model_hashes_json", sa.JSON()))
        batch_op.add_column(sa.Column("gpu_type", sa.String(100)))
        batch_op.add_column(sa.Column("queue_ms", sa.BigInteger()))
        batch_op.add_column(sa.Column("cold_start_ms", sa.BigInteger()))
        batch_op.add_column(sa.Column("runtime_ms", sa.BigInteger()))
        batch_op.add_column(sa.Column("billable_ms", sa.BigInteger()))
        batch_op.add_column(sa.Column("cost_source", sa.String(16)))
        batch_op.create_check_constraint(
            "ck_generation_attempts_timings_nonnegative",
            "(queue_ms IS NULL OR queue_ms >= 0) AND "
            "(cold_start_ms IS NULL OR cold_start_ms >= 0) AND "
            "(runtime_ms IS NULL OR runtime_ms >= 0) AND "
            "(billable_ms IS NULL OR billable_ms >= 0)",
        )
        batch_op.create_check_constraint(
            "ck_generation_attempts_cost_snapshot",
            "(cost_minor IS NULL AND cost_currency IS NULL AND cost_source IS NULL) OR "
            "(cost_minor >= 0 AND cost_currency IS NOT NULL AND "
            "cost_source IN ('ACTUAL', 'ESTIMATE', 'SIMULATED'))",
        )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        json_columns = {"model_hashes_json", "raw_metrics_json"}
        comparisons = " OR\n".join(
            (
                f"(OLD.{column} IS NOT NULL AND OLD.{column}::text "
                f"IS DISTINCT FROM NEW.{column}::text)"
                if column in json_columns
                else f"(OLD.{column} IS NOT NULL AND "
                f"OLD.{column} IS DISTINCT FROM NEW.{column})"
            )
            for column in SNAPSHOT_COLUMNS
        )
        op.execute(
            f"""
            CREATE FUNCTION reject_attempt_snapshot_mutation() RETURNS trigger AS $$
            BEGIN
              IF {comparisons} THEN
                RAISE EXCEPTION 'attempt provenance snapshot is immutable';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER generation_attempt_snapshot_immutable
            BEFORE UPDATE ON generation_attempts
            FOR EACH ROW EXECUTE FUNCTION reject_attempt_snapshot_mutation();
            """
        )
    elif dialect == "sqlite":
        comparisons = " OR\n".join(
            f"(OLD.{column} IS NOT NULL AND OLD.{column} IS NOT NEW.{column})"
            for column in SNAPSHOT_COLUMNS
        )
        op.execute(
            f"""
            CREATE TRIGGER generation_attempt_snapshot_immutable
            BEFORE UPDATE OF {", ".join(SNAPSHOT_COLUMNS)} ON generation_attempts
            WHEN {comparisons}
            BEGIN
              SELECT RAISE(ABORT, 'attempt provenance snapshot is immutable');
            END;
            """
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            DROP TRIGGER generation_attempt_snapshot_immutable ON generation_attempts;
            DROP FUNCTION reject_attempt_snapshot_mutation();
            """
        )
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER generation_attempt_snapshot_immutable")

    with op.batch_alter_table("generation_attempts") as batch_op:
        batch_op.drop_constraint("ck_generation_attempts_cost_snapshot", type_="check")
        batch_op.drop_constraint(
            "ck_generation_attempts_timings_nonnegative", type_="check"
        )
        batch_op.drop_column("cost_source")
        batch_op.drop_column("billable_ms")
        batch_op.drop_column("runtime_ms")
        batch_op.drop_column("cold_start_ms")
        batch_op.drop_column("queue_ms")
        batch_op.drop_column("gpu_type")
        batch_op.drop_column("model_hashes_json")
        batch_op.drop_column("workflow_hash")
        batch_op.drop_column("comfyui_commit")
        batch_op.drop_column("comfyui_version")
        batch_op.drop_column("worker_commit")
        batch_op.drop_column("image_digest")
