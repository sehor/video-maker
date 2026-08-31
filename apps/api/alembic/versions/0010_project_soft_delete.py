"""add project soft deletion and storage cleanup outbox

Revision ID: 0010_project_soft_delete
Revises: 0009_reliable_cancellation
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_project_soft_delete"
down_revision: str | None = "0009_reliable_cancellation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sa.Enum(
                    "ACTIVE",
                    "DELETED",
                    name="projectstatus",
                    native_enum=False,
                    length=16,
                ),
                server_default="ACTIVE",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True)))
        batch_op.create_check_constraint(
            "ck_projects_deletion_state",
            "(status = 'ACTIVE' AND deleted_at IS NULL) OR "
            "(status = 'DELETED' AND deleted_at IS NOT NULL)",
        )

    op.create_table(
        "storage_cleanup_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "PROCESSING",
                "PUBLISHED",
                name="outboxstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("lock_token", sa.String(length=36)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_storage_cleanup_events_attempt_count"
        ),
        sa.CheckConstraint(
            "status != 'PROCESSING' OR (locked_at IS NOT NULL AND lock_token IS NOT NULL)",
            name="ck_storage_cleanup_events_processing_lease",
        ),
        sa.CheckConstraint(
            "status != 'PUBLISHED' OR published_at IS NOT NULL",
            name="ck_storage_cleanup_events_published_at",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_storage_cleanup_events_idempotency_key"
        ),
        sa.UniqueConstraint("project_id", name="uq_storage_cleanup_events_project"),
    )
    op.create_index(
        "ix_storage_cleanup_events_dispatchable",
        "storage_cleanup_events",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_storage_cleanup_events_project_id",
        "storage_cleanup_events",
        ["project_id"],
    )

    op.create_table(
        "storage_cleanup_objects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=255), nullable=False),
        sa.Column(
            "object_kind",
            sa.Enum(
                "ASSET",
                "OUTPUT",
                name="storagecleanupobjectkind",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "DELETED",
                name="storagecleanupobjectstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("cleaned_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_storage_cleanup_objects_attempt_count"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["storage_cleanup_events.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "object_key", name="uq_storage_cleanup_objects_event_key"
        ),
    )
    op.create_index(
        "ix_storage_cleanup_objects_event_id",
        "storage_cleanup_objects",
        ["event_id"],
    )
    op.create_index(
        "ix_storage_cleanup_objects_event_status",
        "storage_cleanup_objects",
        ["event_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_storage_cleanup_objects_event_status",
        table_name="storage_cleanup_objects",
    )
    op.drop_index(
        "ix_storage_cleanup_objects_event_id", table_name="storage_cleanup_objects"
    )
    op.drop_table("storage_cleanup_objects")
    op.drop_index(
        "ix_storage_cleanup_events_project_id", table_name="storage_cleanup_events"
    )
    op.drop_index(
        "ix_storage_cleanup_events_dispatchable", table_name="storage_cleanup_events"
    )
    op.drop_table("storage_cleanup_events")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("ck_projects_deletion_state", type_="check")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("status")
