import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.admin_schemas import (
    AdminDeadLetterOut,
    AdminGenerationDiagnosticsOut,
    AdminOperationalMetricsOut,
    AdminOperationAuditOut,
)
from app.auth import AdminUser
from app.control_plane import operational_metrics
from app.db import get_db
from app.dead_letters import replay_dead_letter
from app.errors import ApiError, not_found
from app.models import (
    AdminOperationAudit,
    DeadLetterEvent,
    DeadLetterStatus,
    GenerationJob,
)

router = APIRouter(prefix="/v1")
Db = Annotated[Session, Depends(get_db)]


@router.get(
    "/admin/generations/{job_id}",
    response_model=AdminGenerationDiagnosticsOut,
    include_in_schema=False,
)
def get_admin_generation_diagnostics(
    job_id: uuid.UUID,
    _admin: AdminUser,
    db: Db,
) -> GenerationJob:
    job = db.scalar(
        select(GenerationJob)
        .where(GenerationJob.id == job_id)
        .options(selectinload(GenerationJob.attempts))
    )
    if job is None:
        raise not_found("job")
    return job


@router.get(
    "/admin/dead-letters",
    response_model=list[AdminDeadLetterOut],
    include_in_schema=False,
)
def list_admin_dead_letters(
    _admin: AdminUser,
    db: Db,
    status: DeadLetterStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[DeadLetterEvent]:
    query = select(DeadLetterEvent)
    if status is not None:
        query = query.where(DeadLetterEvent.status == status)
    return list(
        db.scalars(query.order_by(DeadLetterEvent.created_at.desc()).limit(limit))
    )


@router.post(
    "/admin/dead-letters/{dead_letter_id}/replay",
    response_model=AdminDeadLetterOut,
    include_in_schema=False,
)
def replay_admin_dead_letter(
    dead_letter_id: uuid.UUID,
    admin: AdminUser,
    db: Db,
) -> DeadLetterEvent:
    try:
        result = replay_dead_letter(db, dead_letter_id, admin)
    except RuntimeError as exc:
        raise ApiError(409, "DEAD_LETTER_NOT_REPLAYABLE", str(exc)) from exc
    if result is None:
        raise not_found("dead letter")
    return result


@router.get(
    "/admin/operation-audits",
    response_model=list[AdminOperationAuditOut],
    include_in_schema=False,
)
def list_admin_operation_audits(
    _admin: AdminUser,
    db: Db,
    target_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AdminOperationAudit]:
    query = select(AdminOperationAudit)
    if target_id is not None:
        query = query.where(AdminOperationAudit.target_id == target_id)
    return list(
        db.scalars(query.order_by(AdminOperationAudit.created_at.desc()).limit(limit))
    )


@router.get(
    "/admin/operational-metrics",
    response_model=AdminOperationalMetricsOut,
    include_in_schema=False,
)
def get_admin_operational_metrics(
    _admin: AdminUser,
    db: Db,
) -> dict[str, int | float | None]:
    return operational_metrics(db)
