"""add API idempotency records for reliable job writes

Revision ID: 0004_job_state_api_idempotency
Revises: 0003_quote_seconds_ledger
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_job_state_api_idempotency"
down_revision: str | None = "0003_quote_seconds_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_idempotency_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("app_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_type", sa.String(64)),
        sa.Column("result_id", sa.Uuid()),
        sa.Column("response_status", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "user_id",
            "scope",
            "idempotency_key",
            name="uq_api_idempotency_user_scope_key",
        ),
    )
    op.create_index(
        "ix_api_idempotency_records_user_id", "api_idempotency_records", ["user_id"]
    )


def downgrade() -> None:
    op.drop_table("api_idempotency_records")
