import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.background import BackgroundTask

from app.auth import CurrentUser
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.errors import ApiError, not_found
from app.generation_options import (
    InternalGenerationOptions,
    get_internal_generation_options,
)
from app.idempotency import acquire, complete, replay_result_id
from app.ledger import (
    create_quote,
    finish_reservation,
    grant_seconds,
    reserve_quote_for_job,
    reserve_quotes_for_batch,
    wallet_balances,
)
from app.models import (
    AttemptStatus,
    BatchStatus,
    GenerationAttempt,
    GenerationBatch,
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
    ProjectStatus,
    Quote,
    SettlementStatus,
    Shot,
    ShotReference,
)
from app.outbox import DispatchResult, OutboxDispatcher, enqueue_generation_workflow
from app.project_cleanup import (
    StorageCleanupDispatcher,
    request_project_deletion,
)
from app.provider_cancel_outbox import (
    ProviderCancelDispatcher,
    ProviderCancelRequest,
    enqueue_provider_cancel,
)
from app.provider_execution import GenerationExecutionService
from app.provider_registry import ProviderNotConfiguredError
from app.routing import (
    RouteDisabledError,
    RouteInputError,
    RouteVersion,
    get_provider_registry,
    get_route_registry,
)
from app.schemas import (
    BatchCreate,
    GenerationBatchOut,
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
from app.storage import LocalObjectStorage, ObjectStorage, validate_media_header
from app.workflow import create_workflow_starter

router = APIRouter(prefix="/v1")
Db = Annotated[Session, Depends(get_db)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
GenerationOptions = Annotated[InternalGenerationOptions, Depends(get_internal_generation_options)]
workflow_starter = create_workflow_starter(get_settings())


def storage() -> ObjectStorage:
    settings = get_settings()
    return LocalObjectStorage(
        settings.storage_root,
        settings.storage_claim_secret.get_secret_value().encode(),
    )


def storage_claim_ttl() -> timedelta:
    return timedelta(seconds=get_settings().storage_claim_ttl_seconds)


def storage_response(store: ObjectStorage, key: str, media_type: str) -> StreamingResponse:
    stat = store.stat(key)
    source = store.open(store.read_claim(key, expires_in=storage_claim_ttl()))
    return StreamingResponse(
        source,
        media_type=media_type,
        headers={"Content-Length": str(stat.size_bytes)},
        background=BackgroundTask(source.close),
    )


def provider_executor(
    provider_code: str, *, require_webhook_secret: bool = False
) -> GenerationExecutionService:
    providers = get_provider_registry()
    try:
        providers.get(provider_code)
    except ProviderNotConfiguredError as exc:
        raise not_found("provider") from exc
    secret = get_settings().mock_provider_webhook_secret
    if require_webhook_secret and provider_code == "mock" and secret is None:
        raise ApiError(503, "PROVIDER_WEBHOOK_DISABLED", "Provider webhook 未配置")
    return GenerationExecutionService(storage(), provider_registry=providers)


def active_generation_route() -> RouteVersion:
    try:
        return get_route_registry().active()
    except RouteDisabledError as exc:
        raise ApiError(503, "ROUTE_DISABLED", "当前生成路线已停止接单") from exc


def route_for_shot(
    db: Session,
    shot: Shot,
    *,
    resolution: str,
    duration_ms: int,
    route: RouteVersion | None = None,
) -> RouteVersion:
    selected = route or active_generation_route()
    has_input = (
        db.scalar(select(ShotReference.id).where(ShotReference.shot_id == shot.id).limit(1))
        is not None
    )
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


async def dispatch_generation_outbox() -> DispatchResult:
    return await OutboxDispatcher(
        SessionLocal,
        workflow_starter,
        max_attempts=get_settings().outbox_max_attempts,
    ).dispatch_once()


async def execute_provider_cancel(request: ProviderCancelRequest) -> None:
    await provider_executor(request.provider_code).request_cancel(
        request.job_id, request.attempt_id, request.idempotency_key
    )


async def dispatch_provider_cancel_outbox() -> DispatchResult:
    return await ProviderCancelDispatcher(
        SessionLocal,
        execute_provider_cancel,
        max_attempts=get_settings().outbox_max_attempts,
    ).dispatch_once()


async def dispatch_storage_cleanup_outbox() -> DispatchResult:
    return await StorageCleanupDispatcher(
        SessionLocal,
        storage(),
        max_attempts=get_settings().outbox_max_attempts,
    ).dispatch_once()


async def reconcile_generation_job(job_id: uuid.UUID) -> object:
    return await GenerationExecutionService(storage()).execute(job_id)


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
    db: Session,
    shot_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Shot:
    statement = (
        select(Shot)
        .join(Project)
        .where(
            Shot.id == shot_id,
            Project.owner_id == owner_id,
            Project.status == ProjectStatus.ACTIVE,
        )
        .options(selectinload(Shot.references))
    )
    if for_update:
        statement = statement.with_for_update(of=Project)
    shot = db.scalar(statement)
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
    statement = select(Project).where(
        Project.owner_id == user.id, Project.status == ProjectStatus.ACTIVE
    )
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
    project = owned_project(db, project_id, user.id, for_update=True)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, key, value)
    db.commit()
    db.refresh(project)
    return project


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: uuid.UUID, user: CurrentUser, db: Db) -> Response:
    project = owned_project(
        db,
        project_id,
        user.id,
        include_deleted=True,
        for_update=True,
    )
    request_project_deletion(db, project)
    db.commit()
    return Response(status_code=204)


@router.post("/projects/{project_id}/shots", response_model=ShotOut, status_code=201)
def create_shot(project_id: uuid.UUID, payload: ShotCreate, user: CurrentUser, db: Db) -> Shot:
    owned_project(db, project_id, user.id, for_update=True)
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
    shot = owned_shot(db, shot_id, user.id, for_update=True)
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
    shot = owned_shot(db, shot_id, user.id, for_update=True)
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
    owned_shot(db, shot_id, user.id, for_update=True)
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
    owned_project(db, project_id, user.id, for_update=True)
    store = storage()
    mime_type = file.content_type or "application/octet-stream"
    first = await file.read(16)
    validate_media_header(mime_type, first)
    claim = store.write_claim(
        "assets",
        mime_type=mime_type,
        max_bytes=get_settings().max_upload_bytes,
        expires_in=storage_claim_ttl(),
    )
    await file.seek(0)
    stored = store.put(claim, file.file, mime_type)
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
def download_asset(asset_id: uuid.UUID, user: CurrentUser, db: Db) -> Response:
    asset = db.scalar(
        select(ProjectAsset).where(
            ProjectAsset.id == asset_id,
            ProjectAsset.owner_id == user.id,
            ProjectAsset.status == ProjectAssetStatus.READY,
        )
    )
    if asset is None:
        raise not_found("asset")
    return storage_response(storage(), asset.object_key, asset.media_type)


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
    shot = owned_shot(db, payload.shot_id, user.id, for_update=True)
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


@router.post(
    "/wallet/test-grants",
    response_model=LedgerTransactionOut,
    status_code=201,
    include_in_schema=False,
)
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
    generation_options: GenerationOptions,
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
        select(Shot)
        .join(Project)
        .where(
            Shot.id == payload.shot_id,
            Project.owner_id == user.id,
            Project.status == ProjectStatus.ACTIVE,
        )
        .with_for_update(of=Project)
    )
    if shot is None:
        raise not_found("shot")
    route = route_for_shot(
        db,
        shot,
        resolution="720P",
        duration_ms=shot.duration_seconds * 1000,
    )
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
        mock_mode=generation_options.mode_for(0),
        selected_route_candidate_id=route.candidate_id,
    )
    db.add(job)
    db.flush()
    reserve_quote_for_job(db, user, shot, payload.quote_id, job)
    attempt = GenerationAttempt(
        job_id=job.id,
        attempt_no=1,
        provider_endpoint_id=route.provider_endpoint_id,
        provider_code=route.provider_code,
        workflow_version=route.workflow_id,
        status=AttemptStatus.CREATED,
    )
    db.add(attempt)
    if not transition_job(db, job, JobStatus.QUEUED, "job.queued", f"job:{job.id}:queued:v1"):
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


@router.post("/batches", response_model=GenerationBatchOut, status_code=202)
def create_batch(
    payload: BatchCreate,
    user: CurrentUser,
    db: Db,
    generation_options: GenerationOptions,
    idempotency_key: IdempotencyKey = None,
) -> GenerationBatch:
    decision = acquire(
        db,
        user_id=user.id,
        scope="POST:/v1/batches",
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
    )
    replay_id = replay_result_id(decision, "generation_batch")
    if replay_id is not None:
        return load_batch(db, replay_id, user.id)
    route = active_generation_route()

    batch = GenerationBatch(
        id=uuid.uuid4(),
        user_id=user.id,
        status=BatchStatus.QUEUED,
    )
    claimed = reserve_quotes_for_batch(
        db,
        user,
        batch,
        [item.quote_id for item in payload.items],
    )
    for index, (_item, (quote, shot, snapshot)) in enumerate(
        zip(payload.items, claimed, strict=True)
    ):
        route_for_shot(
            db,
            shot,
            resolution=quote.resolution,
            duration_ms=quote.duration_ms,
            route=route,
        )
        job = GenerationJob(
            user_id=user.id,
            project_id=shot.project_id,
            shot_id=shot.id,
            batch_id=batch.id,
            tier_code=quote.tier_code,
            duration_ms=quote.duration_ms,
            resolution=quote.resolution,
            aspect_ratio=quote.aspect_ratio,
            variant_index=0,
            quote_id=quote.id,
            quote_snapshot_json=snapshot,
            status=JobStatus.CREATED,
            ledger_unit=quote.billing_unit,
            reserved_amount_ms=quote.reserved_ms,
            settlement_status=SettlementStatus.RESERVED,
            reserved_tx_id=batch.reserved_tx_id,
            mock_mode=generation_options.mode_for(index),
            selected_route_candidate_id=route.candidate_id,
        )
        db.add(job)
        db.flush()
        if not transition_job(
            db,
            job,
            JobStatus.RESERVED,
            "job.reserved",
            f"job:{job.id}:reserved:v1",
            {"ledger_transaction_id": str(batch.reserved_tx_id), "batch_id": str(batch.id)},
        ):
            raise ApiError(409, "JOB_STATE_CONFLICT", "任务状态已变化")
        db.add(
            GenerationAttempt(
                job_id=job.id,
                attempt_no=1,
                provider_endpoint_id=route.provider_endpoint_id,
                provider_code=route.provider_code,
                workflow_version=route.workflow_id,
                status=AttemptStatus.CREATED,
            )
        )
        if not transition_job(
            db,
            job,
            JobStatus.QUEUED,
            "job.queued",
            f"job:{job.id}:queued:v1",
        ):
            raise ApiError(409, "JOB_STATE_CONFLICT", "任务状态已变化")
        enqueue_generation_workflow(db, job)

    complete(
        db,
        decision.record,
        result_type="generation_batch",
        result_id=batch.id,
        response_status=202,
    )
    db.commit()
    return load_batch(db, batch.id, user.id)


@router.get("/batches/{batch_id}", response_model=GenerationBatchOut)
def get_batch(batch_id: uuid.UUID, user: CurrentUser, db: Db) -> GenerationBatch:
    return load_batch(db, batch_id, user.id)


@router.get("/generations/{job_id}", response_model=GenerationJobOut)
def get_generation(job_id: uuid.UUID, user: CurrentUser, db: Db) -> GenerationJob:
    return load_job(db, job_id, user.id)


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
    db.execute(
        select(GenerationJob.id).where(GenerationJob.id == job.id).with_for_update()
    ).scalar_one()
    db.refresh(job, ["status"])
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
        if job.status != JobStatus.CANCEL_REQUESTED:
            changed = transition_job(
                db,
                job,
                JobStatus.CANCEL_REQUESTED,
                "job.cancel_requested",
                f"job:{job.id}:cancel-requested:v1",
            )
            if not changed:
                db.expire(job, ["status"])
                if job.status != JobStatus.CANCEL_REQUESTED:
                    raise ApiError(409, "JOB_STATE_CONFLICT", "任务状态已变化")
        if not job.attempts:
            raise ApiError(409, "ATTEMPT_STATE_CONFLICT", "任务没有可取消的尝试")
        enqueue_provider_cancel(db, job, job.attempts[-1])
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
def download_output(output_id: uuid.UUID, user: CurrentUser, db: Db) -> Response:
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
    return storage_response(storage(), output.object_key, output.media_type)
