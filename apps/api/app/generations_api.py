import uuid

from fastapi import APIRouter

from app import application_generations as use_cases
from app.auth import CurrentUser
from app.http_dependencies import Db, GenerationOptions, IdempotencyKey
from app.models import (
    GenerationBatch,
    GenerationJob,
)
from app.schemas import (
    BatchCreate,
    GenerationBatchOut,
    GenerationCreate,
    GenerationJobList,
    GenerationJobOut,
)

router = APIRouter(prefix="/v1")


@router.post("/generations", response_model=GenerationJobOut, status_code=202)
def generate(
    payload: GenerationCreate,
    user: CurrentUser,
    db: Db,
    generation_options: GenerationOptions,
    idempotency_key: IdempotencyKey = None,
) -> GenerationJob:
    return use_cases.generate(payload, user, db, generation_options, idempotency_key)


@router.post("/batches", response_model=GenerationBatchOut, status_code=202)
def create_batch(
    payload: BatchCreate,
    user: CurrentUser,
    db: Db,
    generation_options: GenerationOptions,
    idempotency_key: IdempotencyKey = None,
) -> GenerationBatch:
    return use_cases.create_batch(payload, user, db, generation_options, idempotency_key)


@router.get("/batches/{batch_id}", response_model=GenerationBatchOut)
def get_batch(batch_id: uuid.UUID, user: CurrentUser, db: Db) -> GenerationBatch:
    return use_cases.get_batch(batch_id, user, db)


@router.get("/generations/{job_id}", response_model=GenerationJobOut)
def get_generation(job_id: uuid.UUID, user: CurrentUser, db: Db) -> GenerationJob:
    return use_cases.get_generation(job_id, user, db)


@router.post("/generations/{job_id}/cancel", response_model=GenerationJobOut)
def cancel_generation(
    job_id: uuid.UUID, user: CurrentUser, db: Db, idempotency_key: IdempotencyKey = None
) -> GenerationJob:
    return use_cases.cancel_generation(job_id, user, db, idempotency_key)


@router.get("/jobs", response_model=GenerationJobList)
def list_jobs(user: CurrentUser, db: Db) -> GenerationJobList:
    return use_cases.list_jobs(user, db)
