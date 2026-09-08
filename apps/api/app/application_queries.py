import base64
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.accepted_input import selected_references
from app.errors import ApiError, not_found
from app.models import (
    GenerationBatch,
    GenerationJob,
    LedgerAccount,
    LedgerPosting,
    LedgerTransaction,
    Project,
    ProjectStatus,
    Quote,
    Shot,
)
from app.project_routing import project_route
from app.routing import (
    RouteDisabledError,
    RouteInputError,
    RouteVersion,
    get_route_registry,
)


def active_generation_route() -> RouteVersion:
    try:
        return get_route_registry().active()
    except RouteDisabledError as exc:
        raise ApiError(503, "ROUTE_DISABLED", "当前生成路线已停止接单") from exc


def route_for_shot(
    db: Session, shot: Shot, *, resolution: str, duration_ms: int, route: RouteVersion | None = None
) -> RouteVersion:
    selected = route or project_route(db, shot.project_id)
    has_input = bool(selected_references(db, shot))
    try:
        selected.require_supported(
            resolution=resolution,
            duration_ms=duration_ms,
            aspect_ratio=shot.aspect_ratio,
            has_input=has_input,
        )
    except RouteInputError as exc:
        raise ApiError(422, "NO_ROUTE", "当前生成规格没有可用路线") from exc
    return selected


def encode_cursor(created_at: str, item_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at}|{item_id}".encode()).decode()


def parse_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at, item_id = raw.rsplit("|", 1)
        return (datetime.fromisoformat(created_at), uuid.UUID(item_id))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "CURSOR_INVALID", "分页游标无效") from exc


def owned_project(
    db: Session,
    project_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
) -> Project:
    statement = select(Project).where(Project.id == project_id, Project.owner_id == owner_id)
    if not include_deleted:
        statement = statement.where(Project.status == ProjectStatus.ACTIVE)
    if for_update:
        statement = statement.with_for_update()
    project = db.scalar(statement)
    if project is None:
        raise not_found("project")
    return project


def owned_shot(
    db: Session, shot_id: uuid.UUID, owner_id: uuid.UUID, *, for_update: bool = False
) -> Shot:
    if for_update:
        project_id = db.scalar(select(Shot.project_id).where(Shot.id == shot_id))
        if project_id is None:
            raise not_found("shot")
        owned_project(db, project_id, owner_id, for_update=True)
    statement = (
        select(Shot)
        .join(Project)
        .where(
            Shot.id == shot_id, Project.owner_id == owner_id, Project.status == ProjectStatus.ACTIVE
        )
        .options(selectinload(Shot.references))
    )
    statement = statement.execution_options(populate_existing=True)
    shot = db.scalar(statement)
    if shot is None:
        raise not_found("shot")
    return shot


def load_job(db: Session, job_id: uuid.UUID, owner_id: uuid.UUID) -> GenerationJob:
    job = db.scalar(
        select(GenerationJob)
        .where(GenerationJob.id == job_id, GenerationJob.user_id == owner_id)
        .options(
            selectinload(GenerationJob.attempts),
            selectinload(GenerationJob.outputs),
            selectinload(GenerationJob.events),
        )
    )
    if job is None:
        raise not_found("job")
    return job


def load_batch(db: Session, batch_id: uuid.UUID, owner_id: uuid.UUID) -> GenerationBatch:
    batch = db.scalar(
        select(GenerationBatch)
        .where(GenerationBatch.id == batch_id, GenerationBatch.user_id == owner_id)
        .options(
            selectinload(GenerationBatch.jobs).selectinload(GenerationJob.attempts),
            selectinload(GenerationBatch.jobs).selectinload(GenerationJob.outputs),
            selectinload(GenerationBatch.jobs).selectinload(GenerationJob.events),
        )
    )
    if batch is None:
        raise not_found("batch")
    return batch


def load_quote(db: Session, quote_id: uuid.UUID, owner_id: uuid.UUID) -> Quote:
    quote = db.scalar(select(Quote).where(Quote.id == quote_id, Quote.user_id == owner_id))
    if quote is None:
        raise ApiError(500, "IDEMPOTENCY_RESULT_INVALID", "幂等请求结果记录无效")
    return quote


def load_ledger_transaction(
    db: Session, transaction_id: uuid.UUID, owner_id: uuid.UUID
) -> LedgerTransaction:
    transaction = db.scalar(
        select(LedgerTransaction)
        .join(LedgerPosting)
        .join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
        .where(LedgerTransaction.id == transaction_id, LedgerAccount.owner_id == owner_id)
        .options(selectinload(LedgerTransaction.postings))
    )
    if transaction is None:
        raise ApiError(500, "IDEMPOTENCY_RESULT_INVALID", "幂等请求结果记录无效")
    return transaction
