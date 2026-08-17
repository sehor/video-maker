import base64
import hmac
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, Query, UploadFile
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
    reconciliation_report,
    reserve_batch_quotes,
    reserve_for_admin_retry,
    reserve_quote,
    utcnow,
    wallet_balances,
)
from app.models import (
    Asset,
    Attempt,
    AttemptStatus,
    AuditLog,
    Batch,
    BatchStatus,
    Job,
    JobStatus,
    Output,
    Project,
    Quote,
    SettlementStatus,
    Shot,
)
from app.outbox import drain_local_tasks, enqueue_generation
from app.provider import MockVideoProvider, refresh_batch_status, transition
from app.schemas import (
    AdminAction,
    AssetOut,
    BatchCreate,
    BatchOut,
    GenerationCreate,
    JobList,
    JobOut,
    ProjectCreate,
    ProjectList,
    ProjectOut,
    ProjectUpdate,
    QuoteCreate,
    QuoteOut,
    ReconciliationOut,
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


def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    settings = get_settings()
    expected = settings.admin_api_token
    if settings.environment == "production" and expected == "development-admin-token":
        raise ApiError(503, "ADMIN_NOT_CONFIGURED", "生产环境尚未配置管理员凭据")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise ApiError(403, "ADMIN_REQUIRED", "需要管理员凭据")


Admin = Annotated[None, Depends(require_admin)]


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


def load_admin_job(db: Session, job_id: uuid.UUID) -> Job:
    job = db.scalar(
        select(Job)
        .where(Job.id == job_id)
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
    db.add(
        AuditLog(
            actor_id=user.id,
            action="wallet.test_grant",
            target_type="wallet",
            target_id=str(user.id),
            reason=payload.reason,
            payload={
                "tier": payload.tier,
                "amount_ms": payload.amount_ms,
                "idempotency_key": payload.idempotency_key,
            },
        )
    )
    db.commit()
    return WalletOut(balances=wallet_balances(db, user.id))


def load_batch(db: Session, batch_id: uuid.UUID, owner_id: uuid.UUID) -> Batch:
    batch = db.scalar(
        select(Batch)
        .where(Batch.id == batch_id, Batch.owner_id == owner_id)
        .options(
            selectinload(Batch.jobs).selectinload(Job.attempts),
            selectinload(Batch.jobs).selectinload(Job.outputs),
            selectinload(Batch.jobs).selectinload(Job.events),
        )
    )
    if batch is None:
        raise not_found("batch")
    return batch


@router.post("/batches", response_model=BatchOut, status_code=202)
def create_batch(
    payload: BatchCreate,
    background: BackgroundTasks,
    user: CurrentUser,
    db: Db,
) -> Batch:
    existing = db.scalar(
        select(Batch).where(
            Batch.owner_id == user.id,
            Batch.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        return load_batch(db, existing.id, user.id)
    owned_project(db, payload.project_id, user.id)
    if len(set(payload.quote_ids)) != len(payload.quote_ids):
        raise ApiError(422, "BATCH_DUPLICATE_QUOTE", "批次不能重复使用同一报价")
    quotes = list(
        db.scalars(
            select(Quote)
            .where(Quote.id.in_(payload.quote_ids), Quote.owner_id == user.id)
            .with_for_update()
        )
    )
    quote_by_id = {item.id: item for item in quotes}
    if len(quote_by_id) != len(payload.quote_ids):
        raise ApiError(404, "QUOTE_NOT_FOUND", "批次中存在无效报价")
    shots = list(
        db.scalars(select(Shot).where(Shot.id.in_([item.shot_id for item in quotes])))
    )
    shot_by_id = {item.id: item for item in shots}
    ordered = [
        (quote_by_id[item_id], shot_by_id[quote_by_id[item_id].shot_id])
        for item_id in payload.quote_ids
    ]
    if any(item.project_id != payload.project_id for item, _ in ordered):
        raise ApiError(422, "BATCH_PROJECT_MISMATCH", "批次报价必须属于同一项目")
    batch = Batch(
        owner_id=user.id,
        project_id=payload.project_id,
        ledger_unit=ordered[0][0].ledger_unit,
        reserved_ms=sum(item.reserved_ms for item, _ in ordered),
        idempotency_key=payload.idempotency_key,
        status=BatchStatus.RESERVED,
    )
    db.add(batch)
    db.flush()
    reserve_batch_quotes(db, batch, user, ordered)
    for item, shot in ordered:
        job = Job(
            owner_id=user.id,
            project_id=payload.project_id,
            shot_id=shot.id,
            batch_id=batch.id,
            status=JobStatus.CREATED,
            mock_mode=payload.mock_mode,
            quote_id=item.id,
            quote_snapshot={
                "quote_id": str(item.id),
                "price_version_id": str(item.price_version_id),
                "tier": item.tier_code,
                "ledger_unit": item.ledger_unit,
                "duration_ms": item.duration_ms,
                "variant_count": item.variant_count,
                "resolution": item.resolution,
                "aspect_ratio": item.aspect_ratio,
                "reserved_ms": item.reserved_ms,
            },
            ledger_unit=item.ledger_unit,
            reserved_ms=item.reserved_ms,
            settlement_status=SettlementStatus.RESERVED,
        )
        db.add(job)
        db.flush()
        attempt = Attempt(job_id=job.id, number=1, provider="mock", status=AttemptStatus.CREATED)
        db.add(attempt)
        db.flush()
        transition(db, job, JobStatus.QUEUED, "job.queued", f"{job.id}:queued")
        enqueue_generation(db, job.id, attempt.id)
    batch.status = BatchStatus.RUNNING
    db.commit()
    background.add_task(drain_local_tasks, storage())
    return load_batch(db, batch.id, user.id)


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: uuid.UUID, user: CurrentUser, db: Db) -> Batch:
    return load_batch(db, batch_id, user.id)


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
    enqueue_generation(db, job.id, attempt.id)
    db.commit()
    background.add_task(drain_local_tasks, storage())
    return load_job(db, job.id, user.id)


@router.get("/generations/{job_id}", response_model=JobOut)
def get_generation(job_id: uuid.UUID, user: CurrentUser, db: Db) -> Job:
    return load_job(db, job_id, user.id)


@router.post("/generations/{job_id}/cancel", response_model=JobOut)
async def cancel_generation(job_id: uuid.UUID, user: CurrentUser, db: Db) -> Job:
    job = load_job(db, job_id, user.id)
    if job.status not in {
        JobStatus.CREATED,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.CANCEL_REQUESTED,
    }:
        raise ApiError(409, "JOB_NOT_CANCELLABLE", "当前任务状态不能取消")
    active_attempt = next(
        (
            item
            for item in reversed(job.attempts)
            if item.status
            in {
                AttemptStatus.CREATED,
                AttemptStatus.SUBMITTING,
                AttemptStatus.SUBMITTED,
                AttemptStatus.RUNNING,
            }
        ),
        None,
    )
    if active_attempt is None or active_attempt.provider_job_id is None:
        if transition(db, job, JobStatus.CANCELLED, "job.cancelled", f"{job.id}:cancelled"):
            if active_attempt is not None:
                active_attempt.status = AttemptStatus.CANCELLED
            job.error_code = "USER_CANCELLED"
            job.error_message = "用户取消任务"
            finish_reservation(db, job, settle=False)
            refresh_batch_status(db, job.batch_id)
            db.commit()
        return load_job(db, job.id, user.id)
    if job.status != JobStatus.CANCEL_REQUESTED and transition(
        db,
        job,
        JobStatus.CANCEL_REQUESTED,
        "job.cancel_requested",
        f"{job.id}:cancel-requested",
    ):
        job.cancel_requested_at = utcnow()
        db.commit()
    await MockVideoProvider(storage()).cancel(active_attempt.provider_job_id)
    db.expire_all()
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


@router.get("/admin/jobs/{job_id}", response_model=JobOut)
def admin_get_job(job_id: uuid.UUID, db: Db, _admin: Admin) -> Job:
    return load_admin_job(db, job_id)


@router.post("/admin/jobs/{job_id}/force-release", response_model=JobOut)
def admin_force_release(
    job_id: uuid.UUID, payload: AdminAction, db: Db, _admin: Admin
) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        raise not_found("job")
    audit = AuditLog(
        action="job.force_release",
        target_type="job",
        target_id=str(job.id),
        reason=payload.reason,
        payload={"previous_status": job.status.value},
    )
    db.add(audit)
    if not finish_reservation(db, job, settle=False):
        raise ApiError(409, "JOB_NOT_RESERVED", "任务没有可释放的冻结秒数")
    if job.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED_FINAL, JobStatus.CANCELLED}:
        transition(
            db,
            job,
            JobStatus.FAILED_FINAL,
            "admin.force_released",
            f"audit:{audit.id}:force-release",
        )
        job.error_code = "ADMIN_FORCE_RELEASED"
        job.error_message = payload.reason
    refresh_batch_status(db, job.batch_id)
    db.commit()
    return load_admin_job(db, job.id)


@router.post("/admin/jobs/{job_id}/retry", response_model=JobOut, status_code=202)
def admin_retry_job(
    job_id: uuid.UUID,
    payload: AdminAction,
    background: BackgroundTasks,
    db: Db,
    _admin: Admin,
) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        raise not_found("job")
    if job.status != JobStatus.FAILED_FINAL or job.settlement_status != SettlementStatus.RELEASED:
        raise ApiError(409, "JOB_NOT_RETRYABLE", "只有已最终失败并返还秒数的任务可人工重试")
    audit = AuditLog(
        action="job.retry",
        target_type="job",
        target_id=str(job.id),
        reason=payload.reason,
        payload={"previous_attempts": len(job.attempts)},
    )
    db.add(audit)
    db.flush()
    reserve_for_admin_retry(db, job, audit.id)
    next_number = max((item.number for item in job.attempts), default=0) + 1
    attempt = Attempt(
        job_id=job.id,
        number=next_number,
        provider="mock",
        status=AttemptStatus.CREATED,
    )
    db.add(attempt)
    db.flush()
    job.max_attempts = next_number
    job.error_code = None
    job.error_message = None
    transition(
        db,
        job,
        JobStatus.QUEUED,
        "admin.retry_scheduled",
        f"audit:{audit.id}:retry",
    )
    enqueue_generation(db, job.id, attempt.id)
    refresh_batch_status(db, job.batch_id)
    db.commit()
    background.add_task(drain_local_tasks, storage())
    return load_admin_job(db, job.id)


@router.get("/admin/reconciliation", response_model=ReconciliationOut)
def admin_reconciliation(db: Db, _admin: Admin) -> dict[str, object]:
    return reconciliation_report(db)


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
