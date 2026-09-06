import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import (
    AttemptStatus,
    BatchStatus,
    JobStatus,
    OutputValidationStatus,
    ProjectAssetStatus,
    ProviderEventInboxStatus,
    QuoteStatus,
    SettlementStatus,
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


class QualityTierOut(OrmModel):
    code: str
    display_name: str
    billing_unit: str
    enabled: bool


class QualityTierList(BaseModel):
    items: list[QualityTierOut]


class QuoteCreate(BaseModel):
    shot_id: uuid.UUID
    tier: str = Field(min_length=1, max_length=24, pattern=r"^[A-Z][A-Z0-9_]*$")
    resolution: str = "720P"
    variant_count: int = Field(default=1, ge=1, le=1)

    @field_validator("tier", mode="before")
    @classmethod
    def normalize_tier(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("resolution", mode="before")
    @classmethod
    def normalize_resolution(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.upper()
        if normalized not in {"720P", "1080P"}:
            raise ValueError("resolution must be 720P or 1080P")
        return normalized


class QuoteOut(OrmModel):
    id: uuid.UUID
    project_id: uuid.UUID
    shot_id: uuid.UUID
    price_version_id: uuid.UUID
    tier_code: str
    billing_unit: str
    duration_ms: int
    variant_count: int
    resolution: str
    aspect_ratio: str
    reserved_ms: int
    status: QuoteStatus
    expires_at: datetime
    created_at: datetime


class TestGrantCreate(BaseModel):
    tier: str = Field(min_length=1, max_length=24, pattern=r"^[A-Z][A-Z0-9_]*$")
    amount_ms: int = Field(gt=0, le=86_400_000)
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("tier", mode="before")
    @classmethod
    def normalize_tier(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class LedgerPostingOut(OrmModel):
    id: uuid.UUID
    account_id: uuid.UUID
    unit: str
    amount_ms: int
    created_at: datetime


class LedgerTransactionOut(OrmModel):
    id: uuid.UUID
    tx_type: str
    idempotency_key: str
    reference_type: str
    reference_id: str
    unit: str
    metadata_json: dict[str, object]
    postings: list[LedgerPostingOut] = Field(default_factory=list)
    created_at: datetime


class LedgerTransactionList(BaseModel):
    items: list[LedgerTransactionOut]


class WalletOut(BaseModel):
    balances: dict[str, dict[str, int]]


class GenerationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_id: uuid.UUID
    quote_id: uuid.UUID


class BatchItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: uuid.UUID


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BatchItemCreate] = Field(min_length=1, max_length=100)

    @field_validator("items")
    @classmethod
    def require_unique_quotes(cls, items: list[BatchItemCreate]) -> list[BatchItemCreate]:
        quote_ids = [item.quote_id for item in items]
        if len(set(quote_ids)) != len(quote_ids):
            raise ValueError("batch quote_id values must be unique")
        return items


class GenerationAttemptOut(OrmModel):
    id: uuid.UUID
    attempt_no: int
    status: AttemptStatus
    failure_code: str | None

    @field_validator("failure_code", mode="before")
    @classmethod
    def hide_internal_failure_code(cls, value: object) -> object:
        return _public_failure_code(value)


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


class GenerationJobOut(OrmModel):
    id: uuid.UUID
    project_id: uuid.UUID
    shot_id: uuid.UUID
    tier_code: str
    duration_ms: int
    resolution: str
    aspect_ratio: str
    variant_index: int
    quote_id: uuid.UUID | None
    quote_snapshot: dict[str, object] = Field(
        default_factory=dict, validation_alias="quote_snapshot_json"
    )
    ledger_unit: str | None
    reserved_ms: int | None = Field(default=None, validation_alias="reserved_amount_ms")
    settlement_status: SettlementStatus | None
    status: JobStatus
    final_output_id: uuid.UUID | None
    failure_code: str | None
    error_message: str | None
    attempts: list[GenerationAttemptOut] = Field(default_factory=list)
    outputs: list[GenerationOutputOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def hide_internal_failure_details(self) -> "GenerationJobOut":
        public_code = _public_failure_code(self.failure_code)
        if public_code != self.failure_code:
            self.failure_code = public_code
            self.error_message = "生成失败，请稍后重试"
        return self


class GenerationJobList(BaseModel):
    items: list[GenerationJobOut]
    next_cursor: str | None = None


class GenerationBatchOut(OrmModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: BatchStatus
    ledger_unit: str
    reserved_amount_ms: int
    jobs: list[GenerationJobOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ProviderWebhookAck(BaseModel):
    event_id: str
    status: ProviderEventInboxStatus


_PUBLIC_FAILURE_CODES = {
    "INVALID_INPUT",
    "POLICY_REJECTED",
    "USER_CANCELLED",
    "UNSUPPORTED_PARAMETER",
    "NO_ROUTE",
    "OUTPUT_MISSING",
    "OUTPUT_CORRUPTED",
    "OUTPUT_INVALID_MEDIA",
}


def _public_failure_code(value: object) -> object:
    if value is None or value in _PUBLIC_FAILURE_CODES:
        return value
    return "GENERATION_FAILED"


def __getattr__(name: str) -> object:
    """Keep legacy internal DTO imports without mixing Admin definitions back in."""

    if name in {"AdminGenerationAttemptOut", "AdminGenerationDiagnosticsOut"}:
        from app import admin_schemas

        return getattr(admin_schemas, name)
    raise AttributeError(name)
