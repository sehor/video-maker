import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
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
            ["final_output_id", "id"],
            ["generation_outputs.id", "generation_outputs.job_id"],
            name="fk_generation_jobs_final_output",
            use_alter=True,
        ),
        UniqueConstraint("final_output_id", name="uq_generation_jobs_final_output_id"),
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
    quote_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=24), default=JobStatus.CREATED, nullable=False
    )
    reserved_tx_id: Mapped[uuid.UUID | None] = mapped_column()
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
