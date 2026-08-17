"""stage two quote and seconds ledger

Revision ID: 0002_stage_two_ledger
Revises: 0001_stage_one
Create Date: 2026-08-17
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_stage_two_ledger"
down_revision: str | None = "0001_stage_one"
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
        "quality_tiers",
        sa.Column("code", sa.String(24), primary_key=True),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("ledger_unit", sa.String(24), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        *timestamps(),
    )
    op.create_table(
        "price_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tier_code",
            sa.String(24),
            sa.ForeignKey("quality_tiers.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("resolution", sa.String(16), nullable=False),
        sa.Column("charge_numerator", sa.Integer(), nullable=False),
        sa.Column("charge_denominator", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("effective_until", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.UniqueConstraint(
            "tier_code",
            "version",
            "resolution",
            name="uq_price_version_tier_version_resolution",
        ),
        sa.CheckConstraint("charge_numerator > 0", name="ck_price_charge_numerator"),
        sa.CheckConstraint("charge_denominator > 0", name="ck_price_charge_denominator"),
    )
    op.create_index("ix_price_versions_tier_code", "price_versions", ["tier_code"])
    tier_table = sa.table(
        "quality_tiers",
        sa.column("code", sa.String),
        sa.column("display_name", sa.String),
        sa.column("ledger_unit", sa.String),
        sa.column("enabled", sa.Boolean),
    )
    op.bulk_insert(
        tier_table,
        [
            {"code": "FAST", "display_name": "快速", "ledger_unit": "FAST_MS", "enabled": True},
            {
                "code": "STUDIO",
                "display_name": "工作室",
                "ledger_unit": "STUDIO_MS",
                "enabled": True,
            },
            {
                "code": "CINEMA",
                "display_name": "电影",
                "ledger_unit": "CINEMA_MS",
                "enabled": False,
            },
        ],
    )
    price_table = sa.table(
        "price_versions",
        sa.column("id", sa.Uuid),
        sa.column("tier_code", sa.String),
        sa.column("version", sa.Integer),
        sa.column("resolution", sa.String),
        sa.column("charge_numerator", sa.Integer),
        sa.column("charge_denominator", sa.Integer),
        sa.column("enabled", sa.Boolean),
    )
    op.bulk_insert(
        price_table,
        [
            {
                "id": uuid.uuid4(),
                "tier_code": tier,
                "version": 1,
                "resolution": resolution,
                "charge_numerator": 1,
                "charge_denominator": 1,
                "enabled": enabled,
            }
            for tier, resolution, enabled in (
                ("FAST", "720p", True),
                ("STUDIO", "720p", True),
                ("STUDIO", "1080p", False),
                ("CINEMA", "720p", False),
            )
        ],
    )
    op.create_table(
        "quotes",
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
        sa.Column(
            "price_version_id",
            sa.Uuid(),
            sa.ForeignKey("price_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tier_code", sa.String(24), nullable=False),
        sa.Column("ledger_unit", sa.String(24), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("variant_count", sa.Integer(), nullable=False),
        sa.Column("resolution", sa.String(16), nullable=False),
        sa.Column("aspect_ratio", sa.String(8), nullable=False),
        sa.Column("reserved_ms", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.CheckConstraint("duration_ms > 0", name="ck_quotes_duration_ms"),
        sa.CheckConstraint("variant_count > 0", name="ck_quotes_variant_count"),
        sa.CheckConstraint("reserved_ms > 0", name="ck_quotes_reserved_ms"),
    )
    for column in ("owner_id", "project_id", "shot_id"):
        op.create_index(f"ix_quotes_{column}", "quotes", [column])
    op.create_table(
        "ledger_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("app_users.id", ondelete="CASCADE")
        ),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("unit", sa.String(24), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "scope_key", "code", "unit", name="uq_ledger_account_scope_code_unit"
        ),
    )
    op.create_index("ix_ledger_accounts_owner_id", "ledger_accounts", ["owner_id"])
    op.create_table(
        "ledger_transactions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("reference_type", sa.String(32), nullable=False),
        sa.Column("reference_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "wallet_balances",
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("ledger_accounts.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("balance_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "ledger_postings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "transaction_id",
            sa.Uuid(),
            sa.ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("ledger_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("amount_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("transaction_id", "account_id", name="uq_posting_tx_account"),
        sa.CheckConstraint("amount_ms <> 0", name="ck_posting_nonzero"),
    )
    op.create_index("ix_ledger_postings_transaction_id", "ledger_postings", ["transaction_id"])
    op.create_index("ix_ledger_postings_account_id", "ledger_postings", ["account_id"])
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("quote_id", sa.Uuid()))
        batch.add_column(sa.Column("quote_snapshot", sa.JSON()))
        batch.add_column(sa.Column("ledger_unit", sa.String(24)))
        batch.add_column(sa.Column("reserved_ms", sa.BigInteger()))
        batch.add_column(sa.Column("settlement_status", sa.String(16)))
        batch.create_foreign_key(
            "fk_jobs_quote_id", "quotes", ["quote_id"], ["id"], ondelete="RESTRICT"
        )
        batch.create_unique_constraint("uq_jobs_quote_id", ["quote_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION reject_ledger_posting_mutation() RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'ledger postings are immutable';
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER ledger_postings_immutable
            BEFORE UPDATE OR DELETE ON ledger_postings
            FOR EACH ROW EXECUTE FUNCTION reject_ledger_posting_mutation();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER ledger_postings_immutable ON ledger_postings")
        op.execute("DROP FUNCTION reject_ledger_posting_mutation()")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("uq_jobs_quote_id", type_="unique")
        batch.drop_constraint("fk_jobs_quote_id", type_="foreignkey")
        for column in (
            "settlement_status",
            "reserved_ms",
            "ledger_unit",
            "quote_snapshot",
            "quote_id",
        ):
            batch.drop_column(column)
    for table in (
        "ledger_postings",
        "wallet_balances",
        "ledger_transactions",
        "ledger_accounts",
        "quotes",
        "price_versions",
        "quality_tiers",
    ):
        op.drop_table(table)
