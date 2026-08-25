"""replace stage-one media tables with the stage-two core domain contract

Revision ID: 0002_core_domain_contract
Revises: 0001_stage_one
Create Date: 2026-08-25

This migration is intentionally destructive for stage-one Asset/Job data. Projects and
shots are retained, while the temporary stage-one media tables are replaced instead of
rewriting the historical 0001 migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_core_domain_contract"
down_revision: str | None = "0001_stage_one"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    for table in ("job_events", "outputs", "attempts", "jobs", "assets"):
        op.drop_table(table)

    with op.batch_alter_table("projects") as batch_op:
        batch_op.create_unique_constraint("uq_projects_id_owner_id", ["id", "owner_id"])
    with op.batch_alter_table("shots") as batch_op:
        batch_op.create_unique_constraint("uq_shots_id_project_id", ["id", "project_id"])

    op.create_table(
        "project_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(255), nullable=False, unique=True),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_project_assets_project_owner",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "project_id", name="uq_project_assets_id_project_id"),
        *timestamps(),
    )
    op.create_index("ix_project_assets_project_id", "project_assets", ["project_id"])
    op.create_index("ix_project_assets_owner_id", "project_assets", ["owner_id"])

    op.create_table(
        "shot_references",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("shot_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("reference_role", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["shot_id", "project_id"],
            ["shots.id", "shots.project_id"],
            name="fk_shot_references_shot_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "project_id"],
            ["project_assets.id", "project_assets.project_id"],
            name="fk_shot_references_asset_project",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("shot_id", "asset_id", "reference_role", name="uq_shot_reference_role"),
        *timestamps(),
    )
    for column in ("project_id", "shot_id", "asset_id"):
        op.create_index(f"ix_shot_references_{column}", "shot_references", [column])

    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("shot_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid()),
        sa.Column("tier_code", sa.String(32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("resolution", sa.String(8), nullable=False),
        sa.Column("aspect_ratio", sa.String(8), nullable=False),
        sa.Column("variant_index", sa.Integer(), nullable=False),
        sa.Column("quote_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reserved_tx_id", sa.Uuid()),
        sa.Column("selected_route_candidate_id", sa.Uuid()),
        sa.Column("final_output_id", sa.Uuid()),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("mock_mode", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_generation_jobs_project_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shot_id", "project_id"],
            ["shots.id", "shots.project_id"],
            name="fk_generation_jobs_shot_project",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("duration_ms > 0", name="ck_generation_jobs_duration_ms"),
        sa.CheckConstraint("resolution IN ('720P', '1080P')", name="ck_generation_jobs_resolution"),
        sa.CheckConstraint("aspect_ratio IN ('16:9', '9:16')", name="ck_generation_jobs_aspect"),
        sa.CheckConstraint("variant_index >= 0", name="ck_generation_jobs_variant_index"),
        sa.UniqueConstraint("final_output_id", name="uq_generation_jobs_final_output_id"),
        *timestamps(),
    )
    for column in ("user_id", "project_id", "shot_id", "batch_id"):
        op.create_index(f"ix_generation_jobs_{column}", "generation_jobs", [column])

    op.create_table(
        "generation_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider_endpoint_id", sa.Uuid()),
        sa.Column("provider_code", sa.String(50), nullable=False),
        sa.Column("provider_job_id", sa.String(255)),
        sa.Column("workflow_version", sa.String(100), nullable=False),
        sa.Column("worker_version", sa.String(100)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cost_minor", sa.BigInteger()),
        sa.Column("cost_currency", sa.String(3)),
        sa.Column("raw_metrics_json", sa.JSON()),
        sa.CheckConstraint("attempt_no >= 1", name="ck_generation_attempts_attempt_no"),
        sa.UniqueConstraint("job_id", "attempt_no", name="uq_generation_attempt_job_no"),
        sa.UniqueConstraint("id", "job_id", name="uq_generation_attempts_id_job_id"),
        sa.UniqueConstraint(
            "provider_endpoint_id",
            "provider_job_id",
            name="uq_generation_attempt_provider_job",
        ),
        *timestamps(),
    )
    op.create_index("ix_generation_attempts_job_id", "generation_attempts", ["job_id"])
    op.create_index(
        "ix_generation_attempts_provider_endpoint_id",
        "generation_attempts",
        ["provider_endpoint_id"],
    )

    op.create_table(
        "generation_outputs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(255), nullable=False, unique=True),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("fps", sa.Numeric(8, 3, asdecimal=False)),
        sa.Column("codec", sa.String(50)),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("validation_status", sa.String(24), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id", "job_id"],
            ["generation_attempts.id", "generation_attempts.job_id"],
            name="fk_generation_outputs_attempt_job",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "job_id", name="uq_generation_outputs_id_job_id"),
        *timestamps(),
    )
    op.create_index("ix_generation_outputs_job_id", "generation_outputs", ["job_id"])
    op.create_index("ix_generation_outputs_attempt_id", "generation_outputs", ["attempt_id"])

    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.create_foreign_key(
            "fk_generation_jobs_final_output",
            "generation_outputs",
            ["final_output_id", "id"],
            ["id", "job_id"],
        )

    op.create_table(
        "job_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_id", sa.Uuid()),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(24)),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("dedup_key", sa.String(255), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id", "job_id"],
            ["generation_attempts.id", "generation_attempts.job_id"],
            name="fk_job_events_attempt_job",
            ondelete="RESTRICT",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])
    op.create_index("ix_job_events_attempt_id", "job_events", ["attempt_id"])


def downgrade() -> None:
    op.drop_table("job_events")
    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.drop_constraint("fk_generation_jobs_final_output", type_="foreignkey")
    for table in (
        "generation_outputs",
        "generation_attempts",
        "generation_jobs",
        "shot_references",
        "project_assets",
    ):
        op.drop_table(table)

    with op.batch_alter_table("shots") as batch_op:
        batch_op.drop_constraint("uq_shots_id_project_id", type_="unique")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("uq_projects_id_owner_id", type_="unique")

    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("storage_key", sa.String(255), nullable=False, unique=True),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_assets_project_id", "assets", ["project_id"])
    op.create_index("ix_assets_owner_id", "assets", ["owner_id"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "shot_id", sa.Uuid(), sa.ForeignKey("shots.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("mock_mode", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(500)),
        *timestamps(),
    )
    for column in ("owner_id", "project_id", "shot_id"):
        op.create_index(f"ix_jobs_{column}", "jobs", [column])
    op.create_table(
        "attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_job_id", sa.String(255)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.UniqueConstraint("job_id", "number", name="uq_attempt_job_number"),
        *timestamps(),
    )
    op.create_index("ix_attempts_job_id", "attempts", ["job_id"])
    op.create_table(
        "outputs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(255), nullable=False, unique=True),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_outputs_job_id", "outputs", ["job_id"])
    op.create_table(
        "job_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(24)),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("dedup_key", sa.String(255), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])
