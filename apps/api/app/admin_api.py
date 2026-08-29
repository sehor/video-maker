import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.admin_schemas import AdminGenerationDiagnosticsOut
from app.auth import AdminUser
from app.db import get_db
from app.errors import not_found
from app.models import GenerationJob

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
