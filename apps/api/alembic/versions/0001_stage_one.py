"""stage one business schema

Revision ID: 0001_stage_one
Revises:
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_stage_one"
down_revision: str | None = None
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
    op.create_table(
        "app_users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("auth_subject", sa.String(255), nullable=False, unique=True),
        *timestamps(),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text()),
        *timestamps(),
    )
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])
    op.create_table(
        "shots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("aspect_ratio", sa.String(8), nullable=False),
        sa.CheckConstraint("duration_seconds BETWEEN 1 AND 10", name="ck_shots_duration"),
        sa.CheckConstraint("aspect_ratio IN ('16:9', '9:16')", name="ck_shots_aspect"),
        *timestamps(),
    )
    op.create_index("ix_shots_project_id", "shots", ["project_id"])
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


def downgrade() -> None:
    for table in (
        "job_events",
        "outputs",
        "attempts",
        "jobs",
        "assets",
        "shots",
        "projects",
        "app_users",
    ):
        op.drop_table(table)
