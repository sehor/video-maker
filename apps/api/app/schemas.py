import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    AttemptStatus,
    JobStatus,
    OutputValidationStatus,
    ProjectAssetStatus,
)


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class ProjectOut(OrmModel):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ProjectList(BaseModel):
    items: list[ProjectOut]
    next_cursor: str | None = None


class ShotCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=4000)
    duration_seconds: int = Field(ge=1, le=10)
    aspect_ratio: Literal["16:9", "9:16"]


class ShotUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    duration_seconds: int | None = Field(default=None, ge=1, le=10)
    aspect_ratio: Literal["16:9", "9:16"] | None = None


class ShotReferenceCreate(BaseModel):
    asset_id: uuid.UUID
    reference_role: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z][A-Z0-9_]*$")


class ShotReferenceOut(OrmModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    reference_role: str
    created_at: datetime


class ShotOut(OrmModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    prompt: str
    duration_seconds: int
    aspect_ratio: str
    references: list[ShotReferenceOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ShotList(BaseModel):
    items: list[ShotOut]
    next_cursor: str | None = None


class ProjectAssetOut(OrmModel):
    id: uuid.UUID
    project_id: uuid.UUID
    original_filename: str
    media_type: str
    size_bytes: int
    sha256: str
    status: ProjectAssetStatus
    created_at: datetime


class GenerationCreate(BaseModel):
    shot_id: uuid.UUID
    mock_mode: Literal["success", "delayed", "failure", "timeout", "duplicate", "corrupt"] = (
        "success"
    )


class GenerationAttemptOut(OrmModel):
    id: uuid.UUID
    attempt_no: int
    provider_code: str
    status: AttemptStatus
    provider_job_id: str | None
    workflow_version: str
    failure_code: str | None


class GenerationOutputOut(OrmModel):
    id: uuid.UUID
    attempt_id: uuid.UUID
    media_type: str
    duration_ms: int | None
    width: int | None
    height: int | None
    fps: float | None
    codec: str | None
    size_bytes: int
    sha256: str
    validation_status: OutputValidationStatus


class JobEventOut(OrmModel):
    id: uuid.UUID
    attempt_id: uuid.UUID | None
    event_type: str
    from_status: str | None
    to_status: str
    created_at: datetime


class GenerationJobOut(OrmModel):
    id: uuid.UUID
    project_id: uuid.UUID
    shot_id: uuid.UUID
    tier_code: str
    duration_ms: int
    resolution: str
    aspect_ratio: str
    variant_index: int
    status: JobStatus
    final_output_id: uuid.UUID | None
    failure_code: str | None
    error_message: str | None
    mock_mode: str
    attempts: list[GenerationAttemptOut] = Field(default_factory=list)
    outputs: list[GenerationOutputOut] = Field(default_factory=list)
    events: list[JobEventOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class GenerationJobList(BaseModel):
    items: list[GenerationJobOut]
    next_cursor: str | None = None
