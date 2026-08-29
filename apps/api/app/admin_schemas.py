import uuid
from datetime import datetime

from pydantic import Field

from app.models import AttemptStatus, JobStatus
from app.schemas import OrmModel


class AdminGenerationAttemptOut(OrmModel):
    id: uuid.UUID
    attempt_no: int
    status: AttemptStatus
    failure_code: str | None
    provider_endpoint_id: uuid.UUID | None
    provider_code: str
    provider_job_id: str | None
    workflow_version: str
    worker_version: str | None
    image_digest: str | None
    worker_commit: str | None
    comfyui_version: str | None
    comfyui_commit: str | None
    workflow_hash: str | None
    model_hashes_json: dict[str, str] | None
    gpu_type: str | None
    queue_ms: int | None
    cold_start_ms: int | None
    runtime_ms: int | None
    billable_ms: int | None
    cost_minor: int | None
    cost_currency: str | None
    cost_source: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminGenerationDiagnosticsOut(OrmModel):
    id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID
    shot_id: uuid.UUID
    status: JobStatus
    failure_code: str | None
    error_message: str | None
    selected_route_candidate_id: uuid.UUID | None
    mock_mode: str
    attempts: list[AdminGenerationAttemptOut] = Field(default_factory=list)
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
