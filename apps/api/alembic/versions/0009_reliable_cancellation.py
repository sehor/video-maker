"""add reliable provider cancellation outbox association

Revision ID: 0009_reliable_cancellation
Revises: 0008_attempt_provenance
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_reliable_cancellation"
down_revision: str | None = "0008_attempt_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.add_column(sa.Column("attempt_id", sa.Uuid()))
        batch_op.create_foreign_key(
            "fk_outbox_events_attempt_job",
            "generation_attempts",
            ["attempt_id", "job_id"],
            ["id", "job_id"],
            ondelete="CASCADE",
        )
        batch_op.create_check_constraint(
            "ck_outbox_events_cancel_attempt",
            "event_type != 'provider.cancel.requested' OR attempt_id IS NOT NULL",
        )
        batch_op.create_index("ix_outbox_events_attempt_id", ["attempt_id"])


def downgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_index("ix_outbox_events_attempt_id")
        batch_op.drop_constraint("ck_outbox_events_cancel_attempt", type_="check")
        batch_op.drop_constraint("fk_outbox_events_attempt_job", type_="foreignkey")
        batch_op.drop_column("attempt_id")
