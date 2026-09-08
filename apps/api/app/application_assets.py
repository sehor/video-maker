import uuid
from datetime import timedelta

from fastapi import UploadFile
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.application_queries import (
    encode_cursor,
    owned_project,
    parse_cursor,
)
from app.config import get_settings
from app.errors import ApiError, not_found
from app.models import (
    AppUser,
    GenerationJob,
    GenerationOutput,
    OutputValidationStatus,
    ProjectAsset,
    ProjectAssetStatus,
)
from app.schemas import (
    ProjectAssetList,
)
from app.storage import ObjectStorage, validate_media_header


def list_project_assets(
    project_id: uuid.UUID, user: AppUser, db: Session, limit: int = 100, cursor: str | None = None
):
    owned_project(db, project_id, user.id)
    statement = select(ProjectAsset).where(
        ProjectAsset.project_id == project_id,
        ProjectAsset.owner_id == user.id,
        ProjectAsset.status == ProjectAssetStatus.READY,
    )
    parsed = parse_cursor(cursor)
    if parsed is not None:
        created, item_id = parsed
        statement = statement.where(
            or_(
                ProjectAsset.created_at > created,
                and_(ProjectAsset.created_at == created, ProjectAsset.id > item_id),
            )
        )
    items = list(
        db.scalars(statement.order_by(ProjectAsset.created_at, ProjectAsset.id).limit(limit + 1))
    )
    next_cursor = (
        encode_cursor(items[limit - 1].created_at.isoformat(), items[limit - 1].id)
        if len(items) > limit
        else None
    )
    return ProjectAssetList(items=items[:limit], next_cursor=next_cursor)


async def save_asset(
    project_id: uuid.UUID,
    file: UploadFile,
    user: AppUser,
    db: Session,
    *,
    store: ObjectStorage,
    claim_ttl: timedelta,
) -> ProjectAsset:
    owned_project(db, project_id, user.id, for_update=True)
    mime_type = file.content_type or "application/octet-stream"
    first = await file.read(16)
    validate_media_header(mime_type, first)
    claim = store.write_claim(
        "assets",
        mime_type=mime_type,
        max_bytes=get_settings().max_upload_bytes,
        expires_in=claim_ttl,
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


def download_asset(asset_id: uuid.UUID, user: AppUser, db: Session) -> ProjectAsset:
    asset = db.scalar(
        select(ProjectAsset).where(
            ProjectAsset.id == asset_id,
            ProjectAsset.owner_id == user.id,
            ProjectAsset.status == ProjectAssetStatus.READY,
        )
    )
    if asset is None:
        raise not_found("asset")
    return asset


def download_output(output_id: uuid.UUID, user: AppUser, db: Session) -> GenerationOutput:
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
    return output
