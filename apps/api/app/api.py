import base64
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import CurrentUser
from app.config import get_settings
from app.db import get_db
from app.errors import ApiError, not_found
from app.ledger import (
    create_quote,
    finish_reservation,
    grant_test_seconds,
    reserve_quote,
    wallet_balances,
)
from app.models import (
    Asset,
    Attempt,
    AttemptStatus,
    Job,
    JobStatus,
    Output,
    Project,
    SettlementStatus,
    Shot,
)
from app.outbox import drain_local_tasks, enqueue_generation
from app.provider import transition
from app.schemas import (
    AssetOut,
    GenerationCreate,
    JobList,
    JobOut,
    ProjectCreate,
    ProjectList,
    ProjectOut,
    ProjectUpdate,
    QuoteCreate,
    QuoteOut,
    ShotCreate,
    ShotList,
    ShotOut,
    ShotUpdate,
    TestGrantCreate,
    WalletOut,
)
from app.storage import LocalObjectStorage

router = APIRouter(prefix="/v1")
Db = Annotated[Session, Depends(get_db)]


def storage() -> LocalObjectStorage:
    return LocalObjectStorage(get_settings().storage_root)


def encode_cursor(created_at: str, item_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at}|{item_id}".encode()).decode()


def parse_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at, item_id = raw.rsplit("|", 1)
        return datetime.fromisoformat(created_at), uuid.UUID(item_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "CURSOR_INVALID", "分页游标无效") from exc


def owned_project(db: Session, project_id: uuid.UUID, owner_id: uuid.UUID) -> Project:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_id == owner_id)
    )
    if project is None:
        raise not_found("project")
    return project


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, user: CurrentUser, db: Db) -> Project:
    project = Project(owner_id=user.id, **payload.model_dump())
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/projects", response_model=ProjectList)
def list_projects(
    user: CurrentUser,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> ProjectList:
    statement = select(Project).where(Project.owner_id == user.id)
    parsed = parse_cursor(cursor)
    if parsed:
        created_at, item_id = parsed
        statement = statement.where(
            or_(
                Project.created_at < created_at,
                and_(Project.created_at == created_at, Project.id < item_id),
            )
        )
    items = list(
        db.scalars(
            statement.order_by(Project.created_at.desc(), Project.id.desc()).limit(limit + 1)
        )
    )
    next_cursor = None
    if len(items) > limit:
        last = items[limit - 1]
        next_cursor = encode_cursor(last.created_at.isoformat(), last.id)
        items = items[:limit]
    return ProjectList(items=items, next_cursor=next_cursor)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: uuid.UUID, user: CurrentUser, db: Db) -> Project:
    return owned_project(db, project_id, user.id)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: uuid.UUID, payload: ProjectUpdate, user: CurrentUser, db: Db
) -> Project:
    project = owned_project(db, project_id, user.id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, key, value)
    db.commit()
    db.refresh(project)
    return project


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: uuid.UUID, user: CurrentUser, db: Db) -> Response:
    project = owned_project(db, project_id, user.id)
    db.delete(project)
    db.commit()
    return Response(status_code=204)


@router.post("/projects/{project_id}/shots", response_model=ShotOut, status_code=201)
def create_shot(project_id: uuid.UUID, payload: ShotCreate, user: CurrentUser, db: Db) -> Shot:
    owned_project(db, project_id, user.id)
    shot = Shot(project_id=project_id, **payload.model_dump())
    db.add(shot)
    db.commit()
    db.refresh(shot)
    return shot


@router.get("/projects/{project_id}/shots", response_model=ShotList)
def list_shots(project_id: uuid.UUID, user: CurrentUser, db: Db) -> ShotList:
    owned_project(db, project_id, user.id)
    shots = list(
        db.scalars(
            select(Shot).where(Shot.project_id == project_id).order_by(Shot.created_at, Shot.id)
        )
    )
    return ShotList(items=shots)


@router.get("/shots/{shot_id}", response_model=ShotOut)
def get_shot(shot_id: uuid.UUID, user: CurrentUser, db: Db) -> Shot:
    shot = db.scalar(
        select(Shot).join(Project).where(Shot.id == shot_id, Project.owner_id == user.id)
    )
    if shot is None:
        raise not_found("shot")
    return shot


@router.patch("/shots/{shot_id}", response_model=ShotOut)
def update_shot(shot_id: uuid.UUID, payload: ShotUpdate, user: CurrentUser, db: Db) -> Shot:
    shot = get_shot(shot_id, user, db)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(shot, key, value)
    db.commit()
    db.refresh(shot)
    return shot


async def save_asset(
    project_id: uuid.UUID,
    file: UploadFile,
    user: CurrentUser,
    db: Db,
) -> Asset:
    owned_project(db, project_id, user.id)
    store = storage()
    stored = await store.save_upload("assets", file, get_settings().max_upload_bytes)
    asset = Asset(
        project_id=project_id,
        owner_id=user.id,
        storage_key=stored.key,
        original_filename=(file.filename or "upload")[:255],
        mime_type=stored.mime_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )
    db.add(asset)
    try:
        db.commit()
    except Exception:
        store.delete(stored.key)
        raise
    db.refresh(asset)
    return asset


@router.post("/uploads", response_model=AssetOut, status_code=201)
async def upload_asset(
    project_id: Annotated[uuid.UUID, Form()],
    file: Annotated[UploadFile, File()],
    user: CurrentUser,
    db: Db,
) -> Asset:
    return await save_asset(project_id, file, user, db)


@router.post("/projects/{project_id}/assets", response_model=AssetOut, status_code=201)
async def upload_project_asset(
    project_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    user: CurrentUser,
    db: Db,
) -> Asset:
    return await save_asset(project_id, file, user, db)


@router.get("/assets/{asset_id}/content")
def download_asset(asset_id: uuid.UUID, user: CurrentUser, db: Db) -> FileResponse:
    asset = db.scalar(select(Asset).where(Asset.id == asset_id, Asset.owner_id == user.id))
    if asset is None:
        raise not_found("asset")
    return FileResponse(storage().path_for(asset.storage_key), media_type=asset.mime_type)


def load_job(db: Session, job_id: uuid.UUID, owner_id: uuid.UUID) -> Job:
    job = db.scalar(
        select(Job)
        .where(Job.id == job_id, Job.owner_id == owner_id)
        .options(selectinload(Job.attempts), selectinload(Job.outputs), selectinload(Job.events))
    )
    if job is None:
        raise not_found("job")
    return job


@router.post("/quotes", response_model=QuoteOut, status_code=201)
def quote_generation(payload: QuoteCreate, user: CurrentUser, db: Db):
    shot = db.scalar(
        select(Shot).join(Project).where(Shot.id == payload.shot_id, Project.owner_id == user.id)
    )
    if shot is None:
        raise not_found("shot")
    return create_quote(
        db,
        user,
        shot,
        tier_code=payload.tier,
        resolution=payload.resolution,
        variant_count=payload.variant_count,
    )


@router.get("/wallet", response_model=WalletOut)
def get_wallet(user: CurrentUser, db: Db) -> WalletOut:
    return WalletOut(balances=wallet_balances(db, user.id))


@router.post("/wallet/test-grants", response_model=WalletOut, status_code=201)
def issue_test_seconds(payload: TestGrantCreate, user: CurrentUser, db: Db) -> WalletOut:
    if get_settings().environment == "production":
        raise not_found("endpoint")
    grant_test_seconds(
        db,
        user,
        payload.tier,
        payload.amount_ms,
        payload.idempotency_key,
        payload.reason,
    )
    return WalletOut(balances=wallet_balances(db, user.id))


@router.post("/generations", response_model=JobOut, status_code=202)
def generate(
    payload: GenerationCreate,
    background: BackgroundTasks,
    user: CurrentUser,
    db: Db,
) -> Job:
    shot = db.scalar(
        select(Shot).join(Project).where(Shot.id == payload.shot_id, Project.owner_id == user.id)
    )
    if shot is None:
        raise not_found("shot")
    job_id = uuid.uuid4()
    quote = reserve_quote(db, payload.quote_id, user, shot, job_id)
    snapshot = {
        "quote_id": str(quote.id),
        "price_version_id": str(quote.price_version_id),
        "tier": quote.tier_code,
        "ledger_unit": quote.ledger_unit,
        "duration_ms": quote.duration_ms,
        "variant_count": quote.variant_count,
        "resolution": quote.resolution,
        "aspect_ratio": quote.aspect_ratio,
        "reserved_ms": quote.reserved_ms,
    }
    job = Job(
        id=job_id,
        owner_id=user.id,
        project_id=shot.project_id,
        shot_id=shot.id,
        status=JobStatus.CREATED,
        mock_mode=payload.mock_mode,
        quote_id=quote.id,
        quote_snapshot=snapshot,
        ledger_unit=quote.ledger_unit,
        reserved_ms=quote.reserved_ms,
        settlement_status=SettlementStatus.RESERVED,
    )
    db.add(job)
    db.flush()
    attempt = Attempt(job_id=job.id, number=1, provider="mock", status=AttemptStatus.CREATED)
    db.add(attempt)
    transition(db, job, JobStatus.QUEUED, "job.queued", f"{job.id}:queued")
    enqueue_generation(db, job.id)
    db.commit()
    background.add_task(drain_local_tasks, storage())
    return load_job(db, job.id, user.id)


@router.get("/generations/{job_id}", response_model=JobOut)
def get_generation(job_id: uuid.UUID, user: CurrentUser, db: Db) -> Job:
    return load_job(db, job_id, user.id)


@router.post("/generations/{job_id}/cancel", response_model=JobOut)
def cancel_generation(job_id: uuid.UUID, user: CurrentUser, db: Db) -> Job:
    job = load_job(db, job_id, user.id)
    if job.status not in {JobStatus.CREATED, JobStatus.QUEUED, JobStatus.RUNNING}:
        raise ApiError(409, "JOB_NOT_CANCELLABLE", "当前任务状态不能取消")
    if transition(db, job, JobStatus.CANCELLED, "job.cancelled", f"{job.id}:cancelled"):
        for attempt in job.attempts:
            if attempt.status in {AttemptStatus.CREATED, AttemptStatus.RUNNING}:
                attempt.status = AttemptStatus.CANCELLED
        finish_reservation(db, job, settle=False)
        db.commit()
    return load_job(db, job.id, user.id)


@router.get("/jobs", response_model=JobList)
def list_jobs(user: CurrentUser, db: Db) -> JobList:
    jobs = list(
        db.scalars(
            select(Job)
            .where(Job.owner_id == user.id)
            .options(
                selectinload(Job.attempts), selectinload(Job.outputs), selectinload(Job.events)
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(100)
        )
    )
    return JobList(items=jobs)


@router.get("/outputs/{output_id}/content")
def download_output(output_id: uuid.UUID, user: CurrentUser, db: Db) -> FileResponse:
    output = db.scalar(
        select(Output).join(Job).where(Output.id == output_id, Job.owner_id == user.id)
    )
    if output is None:
        raise not_found("output")
    if not output.is_valid:
        raise ApiError(422, "OUTPUT_INVALID", "该输出未通过媒体校验")
    return FileResponse(storage().path_for(output.storage_key), media_type=output.mime_type)
