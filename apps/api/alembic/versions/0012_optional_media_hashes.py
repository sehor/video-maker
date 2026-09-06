"""Make legacy media hashes optional; new media is no longer hashed."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_optional_media_hashes"
down_revision: str | None = "0011_control_plane_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("project_assets", "generation_outputs"):
        op.alter_column(table, "sha256", existing_type=sa.String(64), nullable=True)


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("project_assets", "generation_outputs"):
        if connection.scalar(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE sha256 IS NULL)")
        ):
            raise RuntimeError("Cannot restore required media hashes while unhashed media exists")
    for table in ("project_assets", "generation_outputs"):
        op.alter_column(table, "sha256", existing_type=sa.String(64), nullable=False)
