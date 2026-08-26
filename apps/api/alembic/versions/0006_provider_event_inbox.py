"""add provider event inbox and cancellation-requested state

Revision ID: 0006_provider_event_inbox
Revises: 0005_transactional_outbox
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_provider_event_inbox"
down_revision: str | None = "0005_transactional_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_event_inbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider_code", sa.String(50), nullable=False),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("provider_job_id", sa.String(255), nullable=False),
        sa.Column("provider_status", sa.String(24), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("generation_attempts.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("generation_jobs.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("lock_token", sa.String(36)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "provider_code",
            "external_event_id",
            name="uq_provider_event_inbox_external_event",
        ),
        sa.CheckConstraint(
            "status != 'PROCESSING' OR (locked_at IS NOT NULL AND lock_token IS NOT NULL)",
            name="ck_provider_event_inbox_processing_lease",
        ),
    )
    op.create_index(
        "ix_provider_event_inbox_status_received",
        "provider_event_inbox",
        ["status", "received_at"],
    )
    op.create_index(
        "ix_provider_event_inbox_attempt_id", "provider_event_inbox", ["attempt_id"]
    )
    op.create_index("ix_provider_event_inbox_job_id", "provider_event_inbox", ["job_id"])


def downgrade() -> None:
    op.drop_table("provider_event_inbox")
