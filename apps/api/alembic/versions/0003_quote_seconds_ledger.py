"""add generation quotes and the double-entry seconds ledger

Revision ID: 0003_quote_seconds_ledger
Revises: 0002_core_domain_contract
Create Date: 2026-08-25
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_quote_seconds_ledger"
down_revision: str | None = "0002_core_domain_contract"
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


def price_id(tier: str, resolution: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"video-maker:price:{tier}:v1:{resolution}")


def upgrade() -> None:
    op.create_table(
        "quality_tiers",
        sa.Column("code", sa.String(24), primary_key=True),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("billing_unit", sa.String(24), nullable=False, unique=True),
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
        sa.Column("resolution", sa.String(8), nullable=False),
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
            name="uq_price_versions_tier_version_resolution",
        ),
        sa.CheckConstraint("version > 0", name="ck_price_versions_version"),
        sa.CheckConstraint("charge_numerator > 0", name="ck_price_versions_numerator"),
        sa.CheckConstraint("charge_denominator > 0", name="ck_price_versions_denominator"),
        sa.CheckConstraint(
            "resolution IN ('720P', '1080P')", name="ck_price_versions_resolution"
        ),
    )
    op.create_index("ix_price_versions_tier_code", "price_versions", ["tier_code"])

    tier_table = sa.table(
        "quality_tiers",
        sa.column("code", sa.String),
        sa.column("display_name", sa.String),
        sa.column("billing_unit", sa.String),
        sa.column("enabled", sa.Boolean),
    )
    op.bulk_insert(
        tier_table,
        [
            {"code": "FAST", "display_name": "快速", "billing_unit": "FAST_MS", "enabled": True},
            {
                "code": "STUDIO",
                "display_name": "工作室",
                "billing_unit": "STUDIO_MS",
                "enabled": True,
            },
            {
                "code": "CINEMA",
                "display_name": "电影",
                "billing_unit": "CINEMA_MS",
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
                "id": price_id(tier, resolution),
                "tier_code": tier,
                "version": 1,
                "resolution": resolution,
                "charge_numerator": 1,
                "charge_denominator": 1,
                "enabled": enabled,
            }
            for tier, resolution, enabled in (
                ("FAST", "720P", True),
                ("STUDIO", "720P", True),
                ("STUDIO", "1080P", False),
                ("CINEMA", "720P", False),
            )
        ],
    )

    op.create_table(
        "generation_quotes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("shot_id", sa.Uuid(), nullable=False),
        sa.Column(
            "price_version_id",
            sa.Uuid(),
            sa.ForeignKey("price_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tier_code",
            sa.String(24),
            sa.ForeignKey("quality_tiers.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("billing_unit", sa.String(24), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("variant_count", sa.Integer(), nullable=False),
        sa.Column("resolution", sa.String(8), nullable=False),
        sa.Column("aspect_ratio", sa.String(8), nullable=False),
        sa.Column("reserved_ms", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_generation_quotes_project_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shot_id", "project_id"],
            ["shots.id", "shots.project_id"],
            name="fk_generation_quotes_shot_project",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "user_id", "shot_id", name="uq_generation_quotes_identity"),
        sa.CheckConstraint("duration_ms > 0", name="ck_generation_quotes_duration_ms"),
        sa.CheckConstraint("variant_count = 1", name="ck_generation_quotes_single_variant"),
        sa.CheckConstraint("reserved_ms > 0", name="ck_generation_quotes_reserved_ms"),
        sa.CheckConstraint(
            "resolution IN ('720P', '1080P')", name="ck_generation_quotes_resolution"
        ),
        sa.CheckConstraint(
            "aspect_ratio IN ('16:9', '9:16')", name="ck_generation_quotes_aspect"
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'USED', 'EXPIRED')", name="ck_generation_quotes_status"
        ),
    )
    for column in ("user_id", "project_id", "shot_id"):
        op.create_index(f"ix_generation_quotes_{column}", "generation_quotes", [column])

    op.create_table(
        "wallet_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("owner_type", sa.String(16), nullable=False),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("app_users.id", ondelete="CASCADE")
        ),
        sa.Column("account_type", sa.String(32), nullable=False),
        sa.Column("unit", sa.String(24), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "scope_key", "account_type", "unit", name="uq_wallet_accounts_scope_type_unit"
        ),
        sa.UniqueConstraint("id", "unit", name="uq_wallet_accounts_id_unit"),
        sa.CheckConstraint(
            "owner_type IN ('USER', 'PLATFORM')", name="ck_wallet_accounts_owner_type"
        ),
        sa.CheckConstraint(
            "account_type IN ('USER_AVAILABLE', 'USER_RESERVED', "
            "'PLATFORM_ISSUED', 'PLATFORM_CONSUMED', 'PLATFORM_EXPIRED')",
            name="ck_wallet_accounts_account_type",
        ),
    )
    op.create_index("ix_wallet_accounts_owner_id", "wallet_accounts", ["owner_id"])
    op.create_table(
        "ledger_transactions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tx_type", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("reference_type", sa.String(32), nullable=False),
        sa.Column("reference_id", sa.String(255), nullable=False),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("id", "unit", name="uq_ledger_transactions_id_unit"),
        sa.UniqueConstraint(
            "tx_type",
            "reference_type",
            "reference_id",
            name="uq_ledger_transactions_business_action",
        ),
    )
    op.create_table(
        "wallet_balances",
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("wallet_accounts.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("balance_ms", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "ledger_postings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("amount_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id", "unit"],
            ["ledger_transactions.id", "ledger_transactions.unit"],
            name="fk_ledger_postings_transaction_unit",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "unit"],
            ["wallet_accounts.id", "wallet_accounts.unit"],
            name="fk_ledger_postings_account_unit",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "transaction_id", "account_id", name="uq_ledger_postings_tx_account"
        ),
        sa.CheckConstraint("amount_ms <> 0", name="ck_ledger_postings_nonzero"),
    )
    op.create_index(
        "ix_ledger_postings_transaction_id", "ledger_postings", ["transaction_id"]
    )
    op.create_index("ix_ledger_postings_account_id", "ledger_postings", ["account_id"])

    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.add_column(sa.Column("quote_id", sa.Uuid()))
        batch_op.add_column(sa.Column("ledger_unit", sa.String(24)))
        batch_op.add_column(sa.Column("reserved_amount_ms", sa.BigInteger()))
        batch_op.add_column(sa.Column("settlement_status", sa.String(16)))
        batch_op.create_foreign_key(
            "fk_generation_jobs_quote_identity",
            "generation_quotes",
            ["quote_id", "user_id", "shot_id"],
            ["id", "user_id", "shot_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_generation_jobs_reserved_tx",
            "ledger_transactions",
            ["reserved_tx_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint("uq_generation_jobs_quote_id", ["quote_id"])
        batch_op.create_unique_constraint("uq_generation_jobs_reserved_tx_id", ["reserved_tx_id"])
        batch_op.create_check_constraint(
            "ck_generation_jobs_settlement_status",
            "settlement_status IS NULL OR settlement_status IN ('RESERVED', 'SETTLED', 'RELEASED')",
        )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
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

            CREATE FUNCTION verify_ledger_transaction_balanced() RETURNS trigger AS $$
            DECLARE target_transaction uuid;
            BEGIN
              target_transaction := CASE
                WHEN TG_OP = 'DELETE' THEN OLD.transaction_id
                ELSE NEW.transaction_id
              END;
              IF (SELECT COALESCE(SUM(amount_ms), 0)
                  FROM ledger_postings
                  WHERE transaction_id = target_transaction) <> 0 THEN
                RAISE EXCEPTION 'ledger transaction % is not balanced', target_transaction;
              END IF;
              RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;

            CREATE CONSTRAINT TRIGGER ledger_transactions_balanced
            AFTER INSERT OR UPDATE OR DELETE ON ledger_postings
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION verify_ledger_transaction_balanced();

            CREATE FUNCTION reject_generation_quote_term_mutation() RETURNS trigger AS $$
            BEGIN
              IF OLD.user_id IS DISTINCT FROM NEW.user_id
                 OR OLD.project_id IS DISTINCT FROM NEW.project_id
                 OR OLD.shot_id IS DISTINCT FROM NEW.shot_id
                 OR OLD.price_version_id IS DISTINCT FROM NEW.price_version_id
                 OR OLD.tier_code IS DISTINCT FROM NEW.tier_code
                 OR OLD.billing_unit IS DISTINCT FROM NEW.billing_unit
                 OR OLD.duration_ms IS DISTINCT FROM NEW.duration_ms
                 OR OLD.variant_count IS DISTINCT FROM NEW.variant_count
                 OR OLD.resolution IS DISTINCT FROM NEW.resolution
                 OR OLD.aspect_ratio IS DISTINCT FROM NEW.aspect_ratio
                 OR OLD.reserved_ms IS DISTINCT FROM NEW.reserved_ms
                 OR OLD.expires_at IS DISTINCT FROM NEW.expires_at THEN
                RAISE EXCEPTION 'generation quote terms are immutable';
              END IF;
              IF OLD.status IS DISTINCT FROM NEW.status
                 AND NOT (OLD.status = 'OPEN' AND NEW.status IN ('USED', 'EXPIRED')) THEN
                RAISE EXCEPTION 'generation quote status cannot move backwards';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER generation_quote_terms_immutable
            BEFORE UPDATE ON generation_quotes
            FOR EACH ROW EXECUTE FUNCTION reject_generation_quote_term_mutation();
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER ledger_postings_immutable_update
            BEFORE UPDATE ON ledger_postings
            BEGIN
              SELECT RAISE(ABORT, 'ledger postings are immutable');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER ledger_postings_immutable_delete
            BEFORE DELETE ON ledger_postings
            BEGIN
              SELECT RAISE(ABORT, 'ledger postings are immutable');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER generation_quote_terms_immutable
            BEFORE UPDATE OF user_id, project_id, shot_id, price_version_id, tier_code,
              billing_unit, duration_ms, variant_count, resolution, aspect_ratio,
              reserved_ms, expires_at ON generation_quotes
            BEGIN
              SELECT RAISE(ABORT, 'generation quote terms are immutable');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER generation_quote_status_monotonic
            BEFORE UPDATE OF status ON generation_quotes
            WHEN NOT (
              OLD.status = NEW.status
              OR (OLD.status = 'OPEN' AND NEW.status IN ('USED', 'EXPIRED'))
            )
            BEGIN
              SELECT RAISE(ABORT, 'generation quote status cannot move backwards');
            END;
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            DROP TRIGGER generation_quote_terms_immutable ON generation_quotes;
            DROP FUNCTION reject_generation_quote_term_mutation();
            DROP TRIGGER ledger_transactions_balanced ON ledger_postings;
            DROP FUNCTION verify_ledger_transaction_balanced();
            DROP TRIGGER ledger_postings_immutable ON ledger_postings;
            DROP FUNCTION reject_ledger_posting_mutation();
            """
        )

    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.drop_constraint("ck_generation_jobs_settlement_status", type_="check")
        batch_op.drop_constraint("uq_generation_jobs_reserved_tx_id", type_="unique")
        batch_op.drop_constraint("uq_generation_jobs_quote_id", type_="unique")
        batch_op.drop_constraint("fk_generation_jobs_reserved_tx", type_="foreignkey")
        batch_op.drop_constraint("fk_generation_jobs_quote_identity", type_="foreignkey")
        batch_op.drop_column("settlement_status")
        batch_op.drop_column("reserved_amount_ms")
        batch_op.drop_column("ledger_unit")
        batch_op.drop_column("quote_id")

    for table in (
        "ledger_postings",
        "wallet_balances",
        "ledger_transactions",
        "wallet_accounts",
        "generation_quotes",
        "price_versions",
        "quality_tiers",
    ):
        op.drop_table(table)
