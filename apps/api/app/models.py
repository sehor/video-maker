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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class JobStatus(str, enum.Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_FINAL = "FAILED_FINAL"
    CANCELLED = "CANCELLED"


class AttemptStatus(str, enum.Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


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
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=24), default=JobStatus.CREATED, nullable=False
    )
    mock_mode: Mapped[str] = mapped_column(String(24), default="success", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))

    attempts: Mapped[list["Attempt"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Attempt.number"
    )
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
