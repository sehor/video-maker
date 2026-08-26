import base64
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth import CurrentUser
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.errors import ApiError, not_found
from app.idempotency import acquire, complete, replay_result_id
from app.ledger import (
    create_quote,
    finish_reservation,
    grant_seconds,
    reserve_quote_for_job,
    wallet_balances,
)
from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobStatus,
    LedgerAccount,
    LedgerPosting,
    LedgerTransaction,
    OutputValidationStatus,
    Project,
    ProjectAsset,
    ProjectAssetStatus,
    Quote,
    SettlementStatus,
    Shot,
    ShotReference,
)
from app.outbox import DispatchResult, OutboxDispatcher, enqueue_generation_workflow
from app.provider import (
    MockVideoProvider,
    WebhookVerificationError,
    WebhookVerificationRequest,
)
from app.provider_execution import GenerationExecutionService
from app.schemas import (
    GenerationCreate,
    GenerationJobList,
    GenerationJobOut,
    LedgerTransactionList,
    LedgerTransactionOut,
    ProjectAssetOut,
    ProjectCreate,
    ProjectList,
    ProjectOut,
    ProjectUpdate,
    ProviderWebhookAck,
    QuoteCreate,
    QuoteOut,
    ShotCreate,
    ShotList,
    ShotOut,
    ShotReferenceCreate,
    ShotReferenceOut,
    ShotUpdate,
    TestGrantCreate,
    WalletOut,
)
from app.state_machine import transition_attempt, transition_job
from app.storage import LocalObjectStorage
from app.workflow import HatchetWorkflowStarter

router = APIRouter(prefix="/v1")
Db = Annotated[Session, Depends(get_db)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
workflow_starter = HatchetWorkflowStarter()


def storage() -> LocalObjectStorage:
    return LocalObjectStorage(get_settings().storage_root)


def provider_executor(
    provider_code: str, *, require_webhook_secret: bool = False
) -> GenerationExecutionService:
    if provider_code != "mock":
        raise not_found("provider")
    secret = get_settings().mock_provider_webhook_secret
    if require_webhook_secret and secret is None:
        raise ApiError(503, "PROVIDER_WEBHOOK_DISABLED", "Provider webhook 未配置")
    return GenerationExecutionService(
        storage(), provider=MockVideoProvider(webhook_secret=secret)
    )


async def dispatch_generation_outbox() -> DispatchResult:
    return await OutboxDispatcher(SessionLocal, workflow_starter).dispatch_once()


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


def owned_shot(db: Session, shot_id: uuid.UUID, owner_id: uuid.UUID) -> Shot:
    shot = db.scalar(
        select(Shot)
        .join(Project)
        .where(Shot.id == shot_id, Project.owner_id == owner_id)
        .options(selectinload(Shot.references))
    )
    if shot is None:
        raise not_found("shot")
    return shot


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
            select(Shot)
            .where(Shot.project_id == project_id)
            .options(selectinload(Shot.references))
            .order_by(Shot.created_at, Shot.id)
        )
    )
    return ShotList(items=shots)


@router.get("/shots/{shot_id}", response_model=ShotOut)
def get_shot(shot_id: uuid.UUID, user: CurrentUser, db: Db) -> Shot:
    return owned_shot(db, shot_id, user.id)


@router.patch("/shots/{shot_id}", response_model=ShotOut)
def update_shot(shot_id: uuid.UUID, payload: ShotUpdate, user: CurrentUser, db: Db) -> Shot:
    shot = get_shot(shot_id, user, db)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(shot, key, value)
    db.commit()
    db.refresh(shot)
    return shot


@router.post("/shots/{shot_id}/references", response_model=ShotReferenceOut, status_code=201)
def create_shot_reference(
    shot_id: uuid.UUID,
    payload: ShotReferenceCreate,
    user: CurrentUser,
    db: Db,
) -> ShotReference:
    shot = owned_shot(db, shot_id, user.id)
    asset = db.scalar(
        select(ProjectAsset).where(
            ProjectAsset.id == payload.asset_id,
            ProjectAsset.project_id == shot.project_id,
            ProjectAsset.owner_id == user.id,
            ProjectAsset.status == ProjectAssetStatus.READY,
        )
    )
    if asset is None:
        raise not_found("asset")
    reference = ShotReference(
        project_id=shot.project_id,
        shot_id=shot.id,
        asset_id=asset.id,
        reference_role=payload.reference_role,
    )
    db.add(reference)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "SHOT_REFERENCE_EXISTS", "该镜头引用已存在") from exc
    db.refresh(reference)
    return reference


@router.delete("/shots/{shot_id}/references/{reference_id}", status_code=204)
def delete_shot_reference(
    shot_id: uuid.UUID,
    reference_id: uuid.UUID,
    user: CurrentUser,
    db: Db,
) -> Response:
    owned_shot(db, shot_id, user.id)
    reference = db.scalar(
        select(ShotReference).where(
            ShotReference.id == reference_id, ShotReference.shot_id == shot_id
        )
    )
    if reference is None:
        raise not_found("shot_reference")
    db.delete(reference)
    db.commit()
    return Response(status_code=204)


async def save_asset(
    project_id: uuid.UUID,
    file: UploadFile,
    user: CurrentUser,
    db: Db,
) -> ProjectAsset:
    owned_project(db, project_id, user.id)
    store = storage()
    stored = await store.save_upload("assets", file, get_settings().max_upload_bytes)
    asset = ProjectAsset(
        project_id=project_id,
        owner_id=user.id,
        object_key=stored.key,
        original_filename=(file.filename or "upload")[:255],
        media_type=stored.mime_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        status=ProjectAssetStatus.READY,
    )
    db.add(asset)
    try:
        db.commit()
    except Exception:
        store.delete(stored.key)
        raise
    db.refresh(asset)
    return asset


@router.post("/uploads", response_model=ProjectAssetOut, status_code=201)
async def upload_asset(
    project_id: Annotated[uuid.UUID, Form()],
    file: Annotated[UploadFile, File()],
    user: CurrentUser,
    db: Db,
) -> ProjectAsset:
    return await save_asset(project_id, file, user, db)


@router.post("/projects/{project_id}/assets", response_model=ProjectAssetOut, status_code=201)
async def upload_project_asset(
    project_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    user: CurrentUser,
    db: Db,
) -> ProjectAsset:
    return await save_asset(project_id, file, user, db)


@router.get("/assets/{asset_id}/content")
def download_asset(asset_id: uuid.UUID, user: CurrentUser, db: Db) -> FileResponse:
    asset = db.scalar(
        select(ProjectAsset).where(
            ProjectAsset.id == asset_id,
            ProjectAsset.owner_id == user.id,
            ProjectAsset.status == ProjectAssetStatus.READY,
        )
    )
    if asset is None:
        raise not_found("asset")
    return FileResponse(storage().path_for(asset.object_key), media_type=asset.media_type)


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
        .where(
            LedgerTransaction.id == transaction_id,
            LedgerAccount.owner_id == owner_id,
        )
        .options(selectinload(LedgerTransaction.postings))
    )
    if transaction is None:
        raise ApiError(500, "IDEMPOTENCY_RESULT_INVALID", "幂等请求结果记录无效")
    return transaction


@router.post("/quotes", response_model=QuoteOut, status_code=201)
def create_generation_quote(
    payload: QuoteCreate,
    user: CurrentUser,
    db: Db,
    idempotency_key: IdempotencyKey = None,
):
    decision = acquire(
        db,
        user_id=user.id,
        scope="POST:/v1/quotes",
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
    )
    replay_id = replay_result_id(decision, "generation_quote")
    if replay_id is not None:
        return load_quote(db, replay_id, user.id)
    shot = owned_shot(db, payload.shot_id, user.id)
    quote = create_quote(
        db,
        user,
        shot,
        tier_code=payload.tier,
        resolution=payload.resolution,
        variant_count=payload.variant_count,
    )
    complete(
        db,
        decision.record,
        result_type="generation_quote",
        result_id=quote.id,
        response_status=201,
    )
    db.commit()
    db.refresh(quote)
    return quote


@router.post("/wallet/test-grants", response_model=LedgerTransactionOut, status_code=201)
def create_test_grant(
    payload: TestGrantCreate,
    user: CurrentUser,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> LedgerTransaction:
    if get_settings().environment == "production":
        raise not_found("endpoint")
    decision = acquire(
        db,
        user_id=user.id,
        scope="POST:/v1/wallet/test-grants",
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
    )
    replay_id = replay_result_id(decision, "ledger_transaction")
    if replay_id is not None:
        return load_ledger_transaction(db, replay_id, user.id)
    transaction = grant_seconds(
        db,
        user,
        tier_code=payload.tier,
        amount_ms=payload.amount_ms,
        idempotency_key=payload.idempotency_key,
        reason=payload.reason,
    )
    complete(
        db,
        decision.record,
        result_type="ledger_transaction",
        result_id=transaction.id,
        response_status=201,
    )
    db.commit()
    return load_ledger_transaction(db, transaction.id, user.id)


@router.get("/wallet", response_model=WalletOut)
def get_wallet(user: CurrentUser, db: Db) -> WalletOut:
    return WalletOut(balances=wallet_balances(db, user.id))


@router.get("/ledger", response_model=LedgerTransactionList)
def list_ledger_transactions(
    user: CurrentUser,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> LedgerTransactionList:
    transactions = list(
        db.scalars(
            select(LedgerTransaction)
            .join(LedgerPosting)
            .join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
            .where(LedgerAccount.owner_id == user.id)
            .options(selectinload(LedgerTransaction.postings))
            .distinct()
            .order_by(LedgerTransaction.created_at.desc(), LedgerTransaction.id.desc())
            .limit(limit)
        ).unique()
    )
    return LedgerTransactionList(items=transactions)


@router.post("/generations", response_model=GenerationJobOut, status_code=202)
def generate(
    payload: GenerationCreate,
    user: CurrentUser,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> GenerationJob:
    decision = acquire(
        db,
        user_id=user.id,
        scope="POST:/v1/generations",
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
    )
    replay_id = replay_result_id(decision, "generation_job")
    if replay_id is not None:
        return load_job(db, replay_id, user.id)
    shot = db.scalar(
        select(Shot).join(Project).where(Shot.id == payload.shot_id, Project.owner_id == user.id)
    )
    if shot is None:
        raise not_found("shot")
    job = GenerationJob(
        user_id=user.id,
        project_id=shot.project_id,
        shot_id=shot.id,
        tier_code="FAST",
        duration_ms=shot.duration_seconds * 1000,
        resolution="720P",
        aspect_ratio=shot.aspect_ratio,
        variant_index=0,
        quote_snapshot_json={},
        status=JobStatus.CREATED,
        mock_mode=payload.mock_mode,
    )
    db.add(job)
    db.flush()
    reserve_quote_for_job(db, user, shot, payload.quote_id, job)
    attempt = GenerationAttempt(
        job_id=job.id,
        attempt_no=1,
        provider_code="mock",
        workflow_version="mock:v1",
        status=AttemptStatus.CREATED,
    )
    db.add(attempt)
    if not transition_job(
        db, job, JobStatus.QUEUED, "job.queued", f"job:{job.id}:queued:v1"
    ):
        raise ApiError(409, "JOB_STATE_CONFLICT", "任务状态已变化")
    enqueue_generation_workflow(db, job)
    complete(
        db,
        decision.record,
        result_type="generation_job",
        result_id=job.id,
        response_status=202,
    )
    db.commit()
    return load_job(db, job.id, user.id)


@router.get("/generations/{job_id}", response_model=GenerationJobOut)
def get_generation(job_id: uuid.UUID, user: CurrentUser, db: Db) -> GenerationJob:
    return load_job(db, job_id, user.id)


@router.post(
    "/provider-webhooks/{provider_code}",
    response_model=ProviderWebhookAck,
    status_code=202,
)
async def receive_provider_webhook(
    provider_code: str,
    request: Request,
) -> ProviderWebhookAck:
    max_bytes = get_settings().provider_webhook_max_bytes
    parts: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_bytes:
            raise ApiError(413, "PROVIDER_WEBHOOK_TOO_LARGE", "Provider webhook 请求过大")
        parts.append(chunk)
    body = b"".join(parts)
    executor = provider_executor(provider_code, require_webhook_secret=True)
    try:
        result = await executor.handle_webhook(
            provider_code,
            WebhookVerificationRequest(headers=request.headers, body=body),
        )
    except WebhookVerificationError as exc:
        raise ApiError(
            401,
            "PROVIDER_WEBHOOK_INVALID",
            "Provider webhook 验证失败",
        ) from exc
    return ProviderWebhookAck(event_id=result.event_id, status=result.status)


@router.post("/generations/{job_id}/cancel", response_model=GenerationJobOut)
async def cancel_generation(
    job_id: uuid.UUID,
    user: CurrentUser,
    db: Db,
    idempotency_key: IdempotencyKey = None,
) -> GenerationJob:
    decision = acquire(
        db,
        user_id=user.id,
        scope="POST:/v1/generations/{job_id}/cancel",
        key=idempotency_key,
        payload={"job_id": str(job_id)},
    )
    replay_id = replay_result_id(decision, "generation_job")
    if replay_id is not None:
        return load_job(db, replay_id, user.id)
    job = load_job(db, job_id, user.id)
    if job.status in {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED_FINAL,
        JobStatus.CANCELLED,
        JobStatus.EXPIRED,
        JobStatus.REJECTED_POLICY,
        JobStatus.POSTPROCESSING,
        JobStatus.VALIDATING,
    }:
        raise ApiError(409, "JOB_NOT_CANCELLABLE", "当前任务状态不能取消")
    submitted = job.status in {
        JobStatus.ROUTING,
        JobStatus.SUBMITTED,
        JobStatus.RUNNING,
        JobStatus.CANCEL_REQUESTED,
    }
    if submitted:
        if job.status != JobStatus.CANCEL_REQUESTED and not transition_job(
            db,
            job,
            JobStatus.CANCEL_REQUESTED,
            "job.cancel_requested",
            f"job:{job.id}:cancel-requested:v1",
        ):
            raise ApiError(409, "JOB_STATE_CONFLICT", "任务状态已变化")
    else:
        if not transition_job(
            db, job, JobStatus.CANCELLED, "job.cancelled", f"job:{job.id}:cancelled:v1"
        ):
            raise ApiError(409, "JOB_STATE_CONFLICT", "任务状态已变化")
        job.finished_at = datetime.now(UTC)
        for attempt in job.attempts:
            if attempt.status not in {
                AttemptStatus.SUCCEEDED,
                AttemptStatus.FAILED_RETRYABLE,
                AttemptStatus.FAILED_FINAL,
                AttemptStatus.CANCELLED,
                AttemptStatus.TIMED_OUT,
            } and not transition_attempt(
                db,
                attempt,
                AttemptStatus.CANCELLED,
                "attempt.cancelled",
                f"attempt:{attempt.id}:cancelled:v1",
            ):
                raise ApiError(409, "ATTEMPT_STATE_CONFLICT", "任务尝试状态已变化")
        if job.settlement_status == SettlementStatus.RESERVED:
            finish_reservation(db, job, settle=False)
    complete(
        db,
        decision.record,
        result_type="generation_job",
        result_id=job.id,
        response_status=200,
    )
    db.commit()
    if submitted:
        await provider_executor(job.attempts[-1].provider_code).request_cancel(job.id)
        db.expire_all()
    return load_job(db, job.id, user.id)


@router.get("/jobs", response_model=GenerationJobList)
def list_jobs(user: CurrentUser, db: Db) -> GenerationJobList:
    jobs = list(
        db.scalars(
            select(GenerationJob)
            .where(GenerationJob.user_id == user.id)
            .options(
                selectinload(GenerationJob.attempts),
                selectinload(GenerationJob.outputs),
                selectinload(GenerationJob.events),
            )
            .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
            .limit(100)
        )
    )
    return GenerationJobList(items=jobs)


@router.get("/outputs/{output_id}/content")
def download_output(output_id: uuid.UUID, user: CurrentUser, db: Db) -> FileResponse:
    output = db.scalar(
        select(GenerationOutput)
        .join(GenerationJob, GenerationOutput.job_id == GenerationJob.id)
        .where(
            GenerationOutput.id == output_id,
            GenerationJob.user_id == user.id,
            GenerationJob.final_output_id == output_id,
        )
    )
    if output is None:
        raise not_found("output")
    if output.validation_status != OutputValidationStatus.VALID:
        raise ApiError(422, "OUTPUT_INVALID", "该输出未通过媒体校验")
    return FileResponse(storage().path_for(output.object_key), media_type=output.media_type)
