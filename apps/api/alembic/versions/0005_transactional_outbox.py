"""add transactional outbox for workflow starts

Revision ID: 0005_transactional_outbox
Revises: 0004_job_state_api_idempotency
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_transactional_outbox"
down_revision: str | None = "0004_job_state_api_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("lock_token", sa.String(36)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("workflow_id", sa.String(255)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("job_id", "event_type", name="uq_outbox_events_job_event"),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_events_idempotency_key"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_outbox_events_attempt_count"),
        sa.CheckConstraint(
            "status != 'PROCESSING' OR (locked_at IS NOT NULL AND lock_token IS NOT NULL)",
            name="ck_outbox_events_processing_lease",
        ),
        sa.CheckConstraint(
            "status != 'PUBLISHED' OR published_at IS NOT NULL",
            name="ck_outbox_events_published_at",
        ),
    )
    op.create_index("ix_outbox_events_job_id", "outbox_events", ["job_id"])
    op.create_index(
        "ix_outbox_events_dispatchable", "outbox_events", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_table("outbox_events")
