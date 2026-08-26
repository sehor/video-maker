import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class JobStatus(str, enum.Enum):
    CREATED = "CREATED"
    RESERVED = "RESERVED"
    QUEUED = "QUEUED"
    ROUTING = "ROUTING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    POSTPROCESSING = "POSTPROCESSING"
    VALIDATING = "VALIDATING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_FINAL = "FAILED_FINAL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED_POLICY = "REJECTED_POLICY"


class AttemptStatus(str, enum.Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class QuoteStatus(str, enum.Enum):
    OPEN = "OPEN"
    USED = "USED"
    EXPIRED = "EXPIRED"


class SettlementStatus(str, enum.Enum):
    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class ApiIdempotencyStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class OutboxStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PUBLISHED = "PUBLISHED"


class ProviderEventInboxStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"


class ProjectAssetStatus(str, enum.Enum):
    READY = "READY"
    DELETED = "DELETED"


class OutputValidationStatus(str, enum.Enum):
    PENDING = "PENDING"
    VALID = "VALID"
    INVALID = "INVALID"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AppUser(Base, TimestampMixin):
    __tablename__ = "app_users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    auth_subject: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


class ApiIdempotencyRecord(Base, TimestampMixin):
    __tablename__ = "api_idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "scope",
            "idempotency_key",
            name="uq_api_idempotency_user_scope_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ApiIdempotencyStatus] = mapped_column(
        Enum(ApiIdempotencyStatus, native_enum=False, length=16),
        default=ApiIdempotencyStatus.IN_PROGRESS,
        nullable=False,
    )
    result_type: Mapped[str | None] = mapped_column(String(64))
    result_id: Mapped[uuid.UUID | None] = mapped_column()
    response_status: Mapped[int | None] = mapped_column(Integer)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("id", "owner_id", name="uq_projects_id_owner_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    shots: Mapped[list["Shot"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    assets: Mapped[list["ProjectAsset"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Shot(Base, TimestampMixin):
    __tablename__ = "shots"
    __table_args__ = (
        CheckConstraint("duration_seconds BETWEEN 1 AND 10", name="ck_shots_duration"),
        CheckConstraint("aspect_ratio IN ('16:9', '9:16')", name="ck_shots_aspect"),
        UniqueConstraint("id", "project_id", name="uq_shots_id_project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(8), nullable=False)

    project: Mapped[Project] = relationship(back_populates="shots")
    references: Mapped[list["ShotReference"]] = relationship(
        back_populates="shot",
        cascade="all, delete-orphan",
        foreign_keys="[ShotReference.shot_id, ShotReference.project_id]",
        overlaps="asset,references",
    )


class ProjectAsset(Base, TimestampMixin):
    __tablename__ = "project_assets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "owner_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_project_assets_project_owner",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "project_id", name="uq_project_assets_id_project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    object_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ProjectAssetStatus] = mapped_column(
        Enum(ProjectAssetStatus, native_enum=False, length=24),
        default=ProjectAssetStatus.READY,
        nullable=False,
    )

    project: Mapped[Project] = relationship(back_populates="assets")
    references: Mapped[list["ShotReference"]] = relationship(
        back_populates="asset",
        foreign_keys="[ShotReference.asset_id, ShotReference.project_id]",
        overlaps="references,shot",
    )


class ShotReference(Base, TimestampMixin):
    __tablename__ = "shot_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["shot_id", "project_id"],
            ["shots.id", "shots.project_id"],
            name="fk_shot_references_shot_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["asset_id", "project_id"],
            ["project_assets.id", "project_assets.project_id"],
            name="fk_shot_references_asset_project",
            ondelete="CASCADE",
        ),
        UniqueConstraint("shot_id", "asset_id", "reference_role", name="uq_shot_reference_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    shot_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    reference_role: Mapped[str] = mapped_column(String(32), nullable=False)

    shot: Mapped[Shot] = relationship(
        back_populates="references",
        foreign_keys=[shot_id, project_id],
        overlaps="asset,references",
    )
    asset: Mapped[ProjectAsset] = relationship(
        back_populates="references",
        foreign_keys=[asset_id, project_id],
        overlaps="references,shot",
    )


class QualityTier(Base, TimestampMixin):
    __tablename__ = "quality_tiers"

    code: Mapped[str] = mapped_column(String(24), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    billing_unit: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PriceVersion(Base, TimestampMixin):
    __tablename__ = "price_versions"
    __table_args__ = (
        UniqueConstraint(
            "tier_code",
            "version",
            "resolution",
            name="uq_price_versions_tier_version_resolution",
        ),
        CheckConstraint("version > 0", name="ck_price_versions_version"),
        CheckConstraint("charge_numerator > 0", name="ck_price_versions_numerator"),
        CheckConstraint("charge_denominator > 0", name="ck_price_versions_denominator"),
        CheckConstraint("resolution IN ('720P', '1080P')", name="ck_price_versions_resolution"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tier_code: Mapped[str] = mapped_column(
        ForeignKey("quality_tiers.code", ondelete="RESTRICT"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String(8), nullable=False)
    charge_numerator: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    charge_denominator: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Quote(Base, TimestampMixin):
    __tablename__ = "generation_quotes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_generation_quotes_project_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["shot_id", "project_id"],
            ["shots.id", "shots.project_id"],
            name="fk_generation_quotes_shot_project",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "user_id", "shot_id", name="uq_generation_quotes_identity"),
        CheckConstraint("duration_ms > 0", name="ck_generation_quotes_duration_ms"),
        CheckConstraint("variant_count = 1", name="ck_generation_quotes_single_variant"),
        CheckConstraint("reserved_ms > 0", name="ck_generation_quotes_reserved_ms"),
        CheckConstraint("resolution IN ('720P', '1080P')", name="ck_generation_quotes_resolution"),
        CheckConstraint("aspect_ratio IN ('16:9', '9:16')", name="ck_generation_quotes_aspect"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    shot_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    price_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_versions.id", ondelete="RESTRICT"), nullable=False
    )
    tier_code: Mapped[str] = mapped_column(
        ForeignKey("quality_tiers.code", ondelete="RESTRICT"), nullable=False
    )
    billing_unit: Mapped[str] = mapped_column(String(24), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    variant_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    resolution: Mapped[str] = mapped_column(String(8), nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(8), nullable=False)
    reserved_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[QuoteStatus] = mapped_column(
        Enum(QuoteStatus, native_enum=False, length=16),
        default=QuoteStatus.OPEN,
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LedgerAccount(Base, TimestampMixin):
    __tablename__ = "wallet_accounts"
    __table_args__ = (
        UniqueConstraint(
            "scope_key", "account_type", "unit", name="uq_wallet_accounts_scope_type_unit"
        ),
        UniqueConstraint("id", "unit", name="uq_wallet_accounts_id_unit"),
        CheckConstraint("owner_type IN ('USER', 'PLATFORM')", name="ck_wallet_accounts_owner_type"),
        CheckConstraint(
            "account_type IN ('USER_AVAILABLE', 'USER_RESERVED', "
            "'PLATFORM_ISSUED', 'PLATFORM_CONSUMED', 'PLATFORM_EXPIRED')",
            name="ck_wallet_accounts_account_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True
    )
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)
    unit: Mapped[str] = mapped_column(String(24), nullable=False)


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    __table_args__ = (
        UniqueConstraint("id", "unit", name="uq_ledger_transactions_id_unit"),
        UniqueConstraint(
            "tx_type",
            "reference_type",
            "reference_id",
            name="uq_ledger_transactions_business_action",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tx_type: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    reference_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_id: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str] = mapped_column(String(24), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    postings: Mapped[list["LedgerPosting"]] = relationship(back_populates="transaction")


class WalletBalance(Base):
    __tablename__ = "wallet_balances"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallet_accounts.id", ondelete="RESTRICT"), primary_key=True
    )
    balance_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class LedgerPosting(Base):
    __tablename__ = "ledger_postings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["transaction_id", "unit"],
            ["ledger_transactions.id", "ledger_transactions.unit"],
            name="fk_ledger_postings_transaction_unit",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_id", "unit"],
            ["wallet_accounts.id", "wallet_accounts.unit"],
            name="fk_ledger_postings_account_unit",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("transaction_id", "account_id", name="uq_ledger_postings_tx_account"),
        CheckConstraint("amount_ms <> 0", name="ck_ledger_postings_nonzero"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    unit: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    transaction: Mapped[LedgerTransaction] = relationship(back_populates="postings")


class GenerationJob(Base, TimestampMixin):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_generation_jobs_project_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["shot_id", "project_id"],
            ["shots.id", "shots.project_id"],
            name="fk_generation_jobs_shot_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["quote_id", "user_id", "shot_id"],
            ["generation_quotes.id", "generation_quotes.user_id", "generation_quotes.shot_id"],
            name="fk_generation_jobs_quote_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["final_output_id", "id"],
            ["generation_outputs.id", "generation_outputs.job_id"],
            name="fk_generation_jobs_final_output",
            use_alter=True,
        ),
        UniqueConstraint("final_output_id", name="uq_generation_jobs_final_output_id"),
        UniqueConstraint("quote_id", name="uq_generation_jobs_quote_id"),
        UniqueConstraint("reserved_tx_id", name="uq_generation_jobs_reserved_tx_id"),
        CheckConstraint("duration_ms > 0", name="ck_generation_jobs_duration_ms"),
        CheckConstraint("resolution IN ('720P', '1080P')", name="ck_generation_jobs_resolution"),
        CheckConstraint("aspect_ratio IN ('16:9', '9:16')", name="ck_generation_jobs_aspect"),
        CheckConstraint("variant_index >= 0", name="ck_generation_jobs_variant_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    shot_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    tier_code: Mapped[str] = mapped_column(String(32), default="FAST", nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String(8), default="720P", nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(8), nullable=False)
    variant_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quote_id: Mapped[uuid.UUID | None] = mapped_column()
    quote_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=24), default=JobStatus.CREATED, nullable=False
    )
    ledger_unit: Mapped[str | None] = mapped_column(String(24))
    reserved_amount_ms: Mapped[int | None] = mapped_column(BigInteger)
    settlement_status: Mapped[SettlementStatus | None] = mapped_column(
        Enum(SettlementStatus, native_enum=False, length=16)
    )
    reserved_tx_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT")
    )
    selected_route_candidate_id: Mapped[uuid.UUID | None] = mapped_column()
    final_output_id: Mapped[uuid.UUID | None] = mapped_column()
    failure_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    mock_mode: Mapped[str] = mapped_column(String(24), default="success", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[list["GenerationAttempt"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="GenerationAttempt.attempt_no",
    )
    outputs: Mapped[list["GenerationOutput"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        foreign_keys="GenerationOutput.job_id",
    )
    events: Mapped[list["JobEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobEvent.created_at"
    )
    outbox_events: Mapped[list["OutboxEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class OutboxEvent(Base, TimestampMixin):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("job_id", "event_type", name="uq_outbox_events_job_event"),
        UniqueConstraint("idempotency_key", name="uq_outbox_events_idempotency_key"),
        CheckConstraint("attempt_count >= 0", name="ck_outbox_events_attempt_count"),
        CheckConstraint(
            "status != 'PROCESSING' OR (locked_at IS NOT NULL AND lock_token IS NOT NULL)",
            name="ck_outbox_events_processing_lease",
        ),
        CheckConstraint(
            "status != 'PUBLISHED' OR published_at IS NOT NULL",
            name="ck_outbox_events_published_at",
        ),
        Index("ix_outbox_events_dispatchable", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, native_enum=False, length=16),
        default=OutboxStatus.PENDING,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_token: Mapped[str | None] = mapped_column(String(36))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workflow_id: Mapped[str | None] = mapped_column(String(255))
    last_error: Mapped[str | None] = mapped_column(Text)

    job: Mapped[GenerationJob] = relationship(back_populates="outbox_events")


class GenerationAttempt(Base, TimestampMixin):
    __tablename__ = "generation_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", name="uq_generation_attempt_job_no"),
        UniqueConstraint("id", "job_id", name="uq_generation_attempts_id_job_id"),
        UniqueConstraint(
            "provider_endpoint_id",
            "provider_job_id",
            name="uq_generation_attempt_provider_job",
        ),
        CheckConstraint("attempt_no >= 1", name="ck_generation_attempts_attempt_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_endpoint_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    provider_code: Mapped[str] = mapped_column(String(50), default="mock", nullable=False)
    provider_job_id: Mapped[str | None] = mapped_column(String(255))
    workflow_version: Mapped[str] = mapped_column(String(100), default="mock:v1", nullable=False)
    worker_version: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[AttemptStatus] = mapped_column(
        Enum(AttemptStatus, native_enum=False, length=24),
        default=AttemptStatus.CREATED,
        nullable=False,
    )
    failure_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_minor: Mapped[int | None] = mapped_column(BigInteger)
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    raw_metrics_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    job: Mapped[GenerationJob] = relationship(back_populates="attempts")
    outputs: Mapped[list["GenerationOutput"]] = relationship(
        back_populates="attempt",
        foreign_keys="[GenerationOutput.attempt_id, GenerationOutput.job_id]",
        overlaps="job,outputs",
    )


class ProviderEventInbox(Base):
    __tablename__ = "provider_event_inbox"
    __table_args__ = (
        UniqueConstraint(
            "provider_code",
            "external_event_id",
            name="uq_provider_event_inbox_external_event",
        ),
        CheckConstraint(
            "status != 'PROCESSING' OR (locked_at IS NOT NULL AND lock_token IS NOT NULL)",
            name="ck_provider_event_inbox_processing_lease",
        ),
        Index("ix_provider_event_inbox_status_received", "status", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider_code: Mapped[str] = mapped_column(String(50), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_job_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ProviderEventInboxStatus] = mapped_column(
        Enum(ProviderEventInboxStatus, native_enum=False, length=16),
        default=ProviderEventInboxStatus.RECEIVED,
        nullable=False,
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_attempts.id", ondelete="RESTRICT"), index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="RESTRICT"), index=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_token: Mapped[str | None] = mapped_column(String(36))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenerationOutput(Base, TimestampMixin):
    __tablename__ = "generation_outputs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "job_id"],
            ["generation_attempts.id", "generation_attempts.job_id"],
            name="fk_generation_outputs_attempt_job",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "job_id", name="uq_generation_outputs_id_job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    object_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Numeric(8, 3, asdecimal=False))
    codec: Mapped[str | None] = mapped_column(String(50))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_status: Mapped[OutputValidationStatus] = mapped_column(
        Enum(OutputValidationStatus, native_enum=False, length=24),
        default=OutputValidationStatus.PENDING,
        nullable=False,
    )

    job: Mapped[GenerationJob] = relationship(back_populates="outputs", foreign_keys=[job_id])
    attempt: Mapped[GenerationAttempt] = relationship(
        back_populates="outputs",
        foreign_keys=[attempt_id, job_id],
        overlaps="job,outputs",
    )


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "job_id"],
            ["generation_attempts.id", "generation_attempts.job_id"],
            name="fk_job_events_attempt_job",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("dedup_key", name="uq_job_events_dedup_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped[GenerationJob] = relationship(back_populates="events")
