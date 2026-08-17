"""stage two retries, batches, cancellation and audit

Revision ID: 0004_stage_two_reliability
Revises: 0003_outbox
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_stage_two_reliability"
down_revision: str | None = "0003_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "batches",
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
        sa.Column("ledger_unit", sa.String(24), nullable=False),
        sa.Column("reserved_ms", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("reserved_ms > 0", name="ck_batches_reserved_ms"),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_batch_owner_idempotency"
        ),
    )
    op.create_index("ix_batches_owner_id", "batches", ["owner_id"])
    op.create_index("ix_batches_project_id", "batches", ["project_id"])

    op.add_column("jobs", sa.Column("batch_id", sa.Uuid()))
    op.add_column(
        "jobs", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3")
    )
    op.add_column("jobs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
    op.create_foreign_key(
        "fk_jobs_batch_id", "jobs", "batches", ["batch_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index("ix_jobs_batch_id", "jobs", ["batch_id"])

    op.add_column("attempts", sa.Column("failure_code", sa.String(64)))
    op.add_column("attempts", sa.Column("failure_message", sa.String(500)))

    op.drop_constraint("uq_workflow_runs_job_id", "workflow_runs", type_="unique")
    op.add_column("workflow_runs", sa.Column("attempt_id", sa.Uuid()))
    op.execute(
        "UPDATE workflow_runs SET attempt_id = "
        "(SELECT id FROM attempts WHERE attempts.job_id = workflow_runs.job_id "
        "ORDER BY number LIMIT 1)"
    )
    op.alter_column("workflow_runs", "attempt_id", nullable=False)
    op.create_foreign_key(
        "fk_workflow_runs_attempt_id",
        "workflow_runs",
        "attempts",
        ["attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_workflow_runs_attempt_id", "workflow_runs", ["attempt_id"], unique=True)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "actor_id", sa.Uuid(), sa.ForeignKey("app_users.id", ondelete="SET NULL")
        ),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_index("ix_workflow_runs_attempt_id", table_name="workflow_runs")
    op.drop_constraint("fk_workflow_runs_attempt_id", "workflow_runs", type_="foreignkey")
    op.drop_column("workflow_runs", "attempt_id")
    op.create_unique_constraint("uq_workflow_runs_job_id", "workflow_runs", ["job_id"])
    op.drop_column("attempts", "failure_message")
    op.drop_column("attempts", "failure_code")
    op.drop_index("ix_jobs_batch_id", table_name="jobs")
    op.drop_constraint("fk_jobs_batch_id", "jobs", type_="foreignkey")
    op.drop_column("jobs", "cancel_requested_at")
    op.drop_column("jobs", "max_attempts")
    op.drop_column("jobs", "batch_id")
    op.drop_table("batches")
