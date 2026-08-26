"""add atomic batch reservations

Revision ID: 0007_batch_atomic_reservation
Revises: 0006_provider_event_inbox
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_batch_atomic_reservation"
down_revision: str | None = "0006_provider_event_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generation_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("ledger_unit", sa.String(24), nullable=False),
        sa.Column("reserved_amount_ms", sa.BigInteger(), nullable=False),
        sa.Column("reserved_tx_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_generation_batches_project_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reserved_tx_id"],
            ["ledger_transactions.id"],
            name="fk_generation_batches_reserved_tx",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "id", "user_id", "project_id", name="uq_generation_batches_identity"
        ),
        sa.UniqueConstraint(
            "id", "reserved_tx_id", name="uq_generation_batches_reservation_identity"
        ),
        sa.UniqueConstraint("reserved_tx_id", name="uq_generation_batches_reserved_tx_id"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED_FINAL')",
            name="ck_generation_batches_status",
        ),
        sa.CheckConstraint(
            "reserved_amount_ms > 0", name="ck_generation_batches_reserved_amount_ms"
        ),
    )
    op.create_index("ix_generation_batches_user_id", "generation_batches", ["user_id"])
    op.create_index("ix_generation_batches_project_id", "generation_batches", ["project_id"])

    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.drop_constraint("uq_generation_jobs_reserved_tx_id", type_="unique")
        batch_op.create_foreign_key(
            "fk_generation_jobs_batch_identity",
            "generation_batches",
            ["batch_id", "user_id", "project_id"],
            ["id", "user_id", "project_id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_generation_jobs_batch_reservation",
            "generation_batches",
            ["batch_id", "reserved_tx_id"],
            ["id", "reserved_tx_id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "uq_generation_jobs_standalone_reserved_tx_id",
        "generation_jobs",
        ["reserved_tx_id"],
        unique=True,
        postgresql_where=sa.text("batch_id IS NULL"),
        sqlite_where=sa.text("batch_id IS NULL"),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE generation_jobs SET batch_id = NULL, reserved_tx_id = NULL "
            "WHERE batch_id IS NOT NULL"
        )
    )
    op.drop_index(
        "uq_generation_jobs_standalone_reserved_tx_id", table_name="generation_jobs"
    )
    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.drop_constraint("fk_generation_jobs_batch_reservation", type_="foreignkey")
        batch_op.drop_constraint("fk_generation_jobs_batch_identity", type_="foreignkey")
        batch_op.create_unique_constraint("uq_generation_jobs_reserved_tx_id", ["reserved_tx_id"])

    op.drop_index("ix_generation_batches_project_id", table_name="generation_batches")
    op.drop_index("ix_generation_batches_user_id", table_name="generation_batches")
    op.drop_table("generation_batches")
