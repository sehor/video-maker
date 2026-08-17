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
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class JobStatus(str, enum.Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED_FINAL = "FAILED_FINAL"
    CANCELLED = "CANCELLED"


class AttemptStatus(str, enum.Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class BatchStatus(str, enum.Enum):
    RESERVED = "RESERVED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class QuoteStatus(str, enum.Enum):
    OPEN = "OPEN"
    USED = "USED"
    EXPIRED = "EXPIRED"


class SettlementStatus(str, enum.Enum):
    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class WorkflowRunStatus(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


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


class QualityTier(Base, TimestampMixin):
    __tablename__ = "quality_tiers"

    code: Mapped[str] = mapped_column(String(24), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    ledger_unit: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PriceVersion(Base, TimestampMixin):
    __tablename__ = "price_versions"
    __table_args__ = (
        UniqueConstraint(
            "tier_code", "version", "resolution", name="uq_price_version_tier_version_resolution"
        ),
        CheckConstraint("charge_numerator > 0", name="ck_price_charge_numerator"),
        CheckConstraint("charge_denominator > 0", name="ck_price_charge_denominator"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tier_code: Mapped[str] = mapped_column(
        ForeignKey("quality_tiers.code", ondelete="RESTRICT"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String(16), nullable=False)
    charge_numerator: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    charge_denominator: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Quote(Base, TimestampMixin):
    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint("duration_ms > 0", name="ck_quotes_duration_ms"),
        CheckConstraint("variant_count > 0", name="ck_quotes_variant_count"),
        CheckConstraint("reserved_ms > 0", name="ck_quotes_reserved_ms"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    shot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shots.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    price_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_versions.id", ondelete="RESTRICT"), nullable=False
    )
    tier_code: Mapped[str] = mapped_column(String(24), nullable=False)
    ledger_unit: Mapped[str] = mapped_column(String(24), nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    variant_count: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String(16), nullable=False)
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
    __tablename__ = "ledger_accounts"
    __table_args__ = (
        UniqueConstraint("scope_key", "code", "unit", name="uq_ledger_account_scope_code_unit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    unit: Mapped[str] = mapped_column(String(24), nullable=False)


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    reference_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LedgerPosting(Base):
    __tablename__ = "ledger_postings"
    __table_args__ = (
        UniqueConstraint("transaction_id", "account_id", name="uq_posting_tx_account"),
        CheckConstraint("amount_ms <> 0", name="ck_posting_nonzero"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    unit: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WalletBalance(Base):
    __tablename__ = "wallet_balances"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), primary_key=True
    )
    balance_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


@event.listens_for(LedgerPosting, "before_update")
@event.listens_for(LedgerPosting, "before_delete")
def reject_posting_mutation(*_args: object) -> None:
    raise ValueError("ledger postings are immutable")


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    shots: Mapped[list["Shot"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Shot(Base, TimestampMixin):
    __tablename__ = "shots"
    __table_args__ = (
        CheckConstraint("duration_seconds BETWEEN 1 AND 10", name="ck_shots_duration"),
        CheckConstraint("aspect_ratio IN ('16:9', '9:16')", name="ck_shots_aspect"),
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


class Asset(Base, TimestampMixin):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class Batch(Base, TimestampMixin):
    __tablename__ = "batches"
    __table_args__ = (
        CheckConstraint("reserved_ms > 0", name="ck_batches_reserved_ms"),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_batch_owner_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ledger_unit: Mapped[str] = mapped_column(String(24), nullable=False)
    reserved_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[BatchStatus] = mapped_column(
        Enum(BatchStatus, native_enum=False, length=24),
        default=BatchStatus.RESERVED,
        nullable=False,
    )

    jobs: Mapped[list["Job"]] = relationship(back_populates="batch")


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    shot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shots.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("batches.id", ondelete="RESTRICT"), index=True
    )
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("quotes.id", ondelete="RESTRICT"), unique=True
    )
    quote_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ledger_unit: Mapped[str | None] = mapped_column(String(24))
    reserved_ms: Mapped[int | None] = mapped_column(BigInteger)
    settlement_status: Mapped[SettlementStatus | None] = mapped_column(
        Enum(SettlementStatus, native_enum=False, length=16)
    )
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=24), default=JobStatus.CREATED, nullable=False
    )
    mock_mode: Mapped[str] = mapped_column(String(24), default="success", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))

    attempts: Mapped[list["Attempt"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Attempt.number"
    )
    batch: Mapped[Batch | None] = relationship(back_populates="jobs")
    outputs: Mapped[list["Output"]] = relationship(back_populates="job")
    events: Mapped[list["JobEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobEvent.created_at"
    )


class Attempt(Base, TimestampMixin):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("job_id", "number", name="uq_attempt_job_number"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), default="mock", nullable=False)
    provider_job_id: Mapped[str | None] = mapped_column(String(255))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[AttemptStatus] = mapped_column(
        Enum(AttemptStatus, native_enum=False, length=24),
        default=AttemptStatus.CREATED,
        nullable=False,
    )

    job: Mapped[Job] = relationship(back_populates="attempts")


class Output(Base, TimestampMixin):
    __tablename__ = "outputs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempts.id", ondelete="RESTRICT"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    job: Mapped[Job] = relationship(back_populates="outputs")


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (UniqueConstraint("dedup_key", name="uq_job_events_dedup_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped[Job] = relationship(back_populates="events")


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workflow_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    workflow_name: Mapped[str] = mapped_column(String(80), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[WorkflowRunStatus] = mapped_column(
        Enum(WorkflowRunStatus, native_enum=False, length=16),
        default=WorkflowRunStatus.ACCEPTED,
        nullable=False,
    )
    run_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
