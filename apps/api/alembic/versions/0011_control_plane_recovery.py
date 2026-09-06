"""add control-plane dead letters and replay audit

Revision ID: 0011_control_plane_recovery
Revises: 0010_project_soft_delete
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_control_plane_recovery"
down_revision: str | None = "0010_project_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dead_letter_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum(
                "OUTBOX",
                "STORAGE_CLEANUP",
                name="deadlettersource",
                native_enum=False,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("cycle_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "OPEN",
                "REPLAYED",
                name="deadletterstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("replayed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count > 0", name="ck_dead_letter_events_attempt_count"
        ),
        sa.CheckConstraint("cycle_count > 0", name="ck_dead_letter_events_cycle_count"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_type", "source_id", name="uq_dead_letter_events_source"
        ),
    )
    op.create_index(
        "ix_dead_letter_events_status_created",
        "dead_letter_events",
        ["status", "created_at"],
    )

    op.create_table(
        "admin_operation_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("operation_key", sa.String(length=255), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["app_users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_key", name="uq_admin_operation_audits_operation_key"
        ),
    )
    op.create_index(
        "ix_admin_operation_audits_actor_user_id",
        "admin_operation_audits",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_admin_operation_audits_target",
        "admin_operation_audits",
        ["target_type", "target_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_admin_operation_audits_target", table_name="admin_operation_audits"
    )
    op.drop_index(
        "ix_admin_operation_audits_actor_user_id",
        table_name="admin_operation_audits",
    )
    op.drop_table("admin_operation_audits")
    op.drop_index(
        "ix_dead_letter_events_status_created", table_name="dead_letter_events"
    )
    op.drop_table("dead_letter_events")
