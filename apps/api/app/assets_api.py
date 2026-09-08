import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import Response

from app import application_assets as use_cases
from app.auth import CurrentUser
from app.blocking_io import run_blocking
from app.bootstrap import storage, storage_claim_ttl
from app.db import SessionLocal
from app.http_dependencies import Db, storage_response
from app.schemas import (
    ProjectAssetList,
    ProjectAssetOut,
)

router = APIRouter(prefix="/v1")


@router.get("/projects/{project_id}/assets", response_model=ProjectAssetList)
def list_project_assets(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: str | None = None,
):
    return use_cases.list_project_assets(project_id, user, db, limit, cursor)


@router.post("/uploads", response_model=ProjectAssetOut, status_code=201)
async def upload_asset(
    project_id: Annotated[uuid.UUID, Form()],
    file: Annotated[UploadFile, File()],
    user: CurrentUser,
) -> ProjectAssetOut:
    return await run_blocking(
        use_cases.save_asset,
        project_id,
        file.file,
        user.id,
        file.filename,
        file.content_type or "application/octet-stream",
        session_factory=SessionLocal,
        storage_factory=storage,
        claim_ttl=storage_claim_ttl(),
    )


@router.post("/projects/{project_id}/assets", response_model=ProjectAssetOut, status_code=201)
async def upload_project_asset(
    project_id: uuid.UUID, file: Annotated[UploadFile, File()], user: CurrentUser, db: Db
) -> ProjectAssetOut:
    return await run_blocking(
        use_cases.save_asset,
        project_id,
        file.file,
        user.id,
        file.filename,
        file.content_type or "application/octet-stream",
        session_factory=SessionLocal,
        storage_factory=storage,
        claim_ttl=storage_claim_ttl(),
    )


@router.get("/assets/{asset_id}/content")
def download_asset(asset_id: uuid.UUID, user: CurrentUser, db: Db) -> Response:
    item = use_cases.download_asset(asset_id, user, db)
    return storage_response(storage(), item.object_key, item.media_type)


@router.get("/outputs/{output_id}/content")
def download_output(output_id: uuid.UUID, user: CurrentUser, db: Db) -> Response:
    item = use_cases.download_output(output_id, user, db)
    return storage_response(storage(), item.object_key, item.media_type)
