import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import JobStatus


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


class ShotOut(OrmModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    prompt: str
    duration_seconds: int
    aspect_ratio: str
    created_at: datetime
    updated_at: datetime


class ShotList(BaseModel):
    items: list[ShotOut]
    next_cursor: str | None = None


class AssetOut(OrmModel):
    id: uuid.UUID
    project_id: uuid.UUID
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class GenerationCreate(BaseModel):
    shot_id: uuid.UUID
    quote_id: uuid.UUID
    mock_mode: Literal["success", "delayed", "failure", "timeout", "duplicate", "corrupt"] = (
        "success"
    )


class AttemptOut(OrmModel):
    id: uuid.UUID
    number: int
    provider: str
    status: str
    provider_job_id: str | None


class OutputOut(OrmModel):
    id: uuid.UUID
    mime_type: str
    size_bytes: int
    sha256: str
    is_valid: bool


class JobEventOut(OrmModel):
    id: uuid.UUID
    event_type: str
    from_status: str | None
    to_status: str
    created_at: datetime


class JobOut(OrmModel):
    id: uuid.UUID
    project_id: uuid.UUID
    shot_id: uuid.UUID
    quote_id: uuid.UUID | None
    quote_snapshot: dict | None
    ledger_unit: str | None
    reserved_ms: int | None
    settlement_status: str | None
    status: JobStatus
    mock_mode: str
    error_code: str | None
    error_message: str | None
    attempts: list[AttemptOut] = Field(default_factory=list)
    outputs: list[OutputOut] = Field(default_factory=list)
    events: list[JobEventOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class JobList(BaseModel):
    items: list[JobOut]
    next_cursor: str | None = None


class QuoteCreate(BaseModel):
    shot_id: uuid.UUID
    tier: Literal["FAST", "STUDIO", "CINEMA"]
    resolution: Literal["720p", "1080p"] = "720p"
    variant_count: int = Field(default=1, ge=1, le=4)


class QuoteOut(OrmModel):
    id: uuid.UUID
    project_id: uuid.UUID
    shot_id: uuid.UUID
    price_version_id: uuid.UUID
    tier_code: str
    ledger_unit: str
    duration_ms: int
    variant_count: int
    resolution: str
    aspect_ratio: str
    reserved_ms: int
    status: str
    expires_at: datetime
    used_at: datetime | None
    created_at: datetime


class TestGrantCreate(BaseModel):
    tier: Literal["FAST", "STUDIO", "CINEMA"]
    amount_ms: int = Field(gt=0, le=86_400_000)
    idempotency_key: str = Field(min_length=8, max_length=255)
    reason: str = Field(min_length=3, max_length=500)


class WalletOut(BaseModel):
    balances: dict[str, dict[str, int]]
