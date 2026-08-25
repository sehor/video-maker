import base64
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth import CurrentUser
from app.config import get_settings
from app.db import get_db
from app.errors import ApiError, not_found
from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobStatus,
    OutputValidationStatus,
    Project,
    ProjectAsset,
    ProjectAssetStatus,
    Shot,
    ShotReference,
)
from app.provider import MockVideoProvider, transition
from app.schemas import (
    GenerationCreate,
    GenerationJobList,
    GenerationJobOut,
    ProjectAssetOut,
    ProjectCreate,
    ProjectList,
    ProjectOut,
    ProjectUpdate,
    ShotCreate,
    ShotList,
    ShotOut,
    ShotReferenceCreate,
    ShotReferenceOut,
    ShotUpdate,
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


@router.post("/generations", response_model=GenerationJobOut, status_code=202)
def generate(
    payload: GenerationCreate,
    background: BackgroundTasks,
    user: CurrentUser,
    db: Db,
) -> GenerationJob:
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
    attempt = GenerationAttempt(
        job_id=job.id,
        attempt_no=1,
        provider_code="mock",
        workflow_version="mock:v1",
        status=AttemptStatus.CREATED,
    )
    db.add(attempt)
    transition(db, job, JobStatus.QUEUED, "job.queued", f"{job.id}:queued")
    db.commit()
    background.add_task(MockVideoProvider(storage()).submit, job.id)
    return load_job(db, job.id, user.id)


@router.get("/generations/{job_id}", response_model=GenerationJobOut)
def get_generation(job_id: uuid.UUID, user: CurrentUser, db: Db) -> GenerationJob:
    return load_job(db, job_id, user.id)


@router.post("/generations/{job_id}/cancel", response_model=GenerationJobOut)
def cancel_generation(job_id: uuid.UUID, user: CurrentUser, db: Db) -> GenerationJob:
    job = load_job(db, job_id, user.id)
    if job.status not in {JobStatus.CREATED, JobStatus.QUEUED, JobStatus.RUNNING}:
        raise ApiError(409, "JOB_NOT_CANCELLABLE", "当前任务状态不能取消")
    if transition(db, job, JobStatus.CANCELLED, "job.cancelled", f"{job.id}:cancelled"):
        job.finished_at = datetime.now(UTC)
        for attempt in job.attempts:
            if attempt.status in {AttemptStatus.CREATED, AttemptStatus.RUNNING}:
                attempt.status = AttemptStatus.CANCELLED
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
