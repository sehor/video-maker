import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.accepted_input import capture_input
from app.application_queries import load_batch, load_job, owned_shot, route_for_shot
from app.errors import ApiError
from app.generation_options import InternalGenerationOptions
from app.idempotency import acquire, complete, replay_result_id
from app.ledger import finish_reservation, reserve_quote_for_job, reserve_quotes_for_batch
from app.models import (
    AppUser,
    AttemptStatus,
    BatchStatus,
    GenerationAttempt,
    GenerationBatch,
    GenerationJob,
    JobStatus,
    SettlementStatus,
)
from app.outbox import enqueue_generation_workflow
from app.project_routing import project_route
from app.provider_cancel_outbox import enqueue_provider_cancel
from app.schemas import (
    BatchCreate,
    GenerationCreate,
    GenerationJobList,
)
from app.state_machine import transition_attempt, transition_job


def generate(
    payload: GenerationCreate,
    user: AppUser,
    db: Session,
    generation_options: InternalGenerationOptions,
    idempotency_key: str | None = None,
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
    shot = owned_shot(db, payload.shot_id, user.id, for_update=True)
    route = route_for_shot(db, shot, resolution="720P", duration_ms=shot.duration_seconds * 1000)
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
    job.input_snapshot_json = capture_input(db, shot, job)
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
        db, decision.record, result_type="generation_job", result_id=job.id, response_status=202
    )
    db.commit()
    return load_job(db, job.id, user.id)


def create_batch(
    payload: BatchCreate,
    user: AppUser,
    db: Session,
    generation_options: InternalGenerationOptions,
    idempotency_key: str | None = None,
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
    batch = GenerationBatch(id=uuid.uuid4(), user_id=user.id, status=BatchStatus.QUEUED)
    claimed = reserve_quotes_for_batch(db, user, batch, [item.quote_id for item in payload.items])
    route = project_route(db, batch.project_id)
    for index, (_item, (quote, shot, snapshot)) in enumerate(
        zip(payload.items, claimed, strict=True)
    ):
        route_for_shot(
            db, shot, resolution=quote.resolution, duration_ms=quote.duration_ms, route=route
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
        job.input_snapshot_json = capture_input(db, shot, job)
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
        if not transition_job(db, job, JobStatus.QUEUED, "job.queued", f"job:{job.id}:queued:v1"):
            raise ApiError(409, "JOB_STATE_CONFLICT", "任务状态已变化")
        enqueue_generation_workflow(db, job)
    complete(
        db, decision.record, result_type="generation_batch", result_id=batch.id, response_status=202
    )
    db.commit()
    return load_batch(db, batch.id, user.id)


def get_batch(batch_id: uuid.UUID, user: AppUser, db: Session) -> GenerationBatch:
    return load_batch(db, batch_id, user.id)


def get_generation(job_id: uuid.UUID, user: AppUser, db: Session) -> GenerationJob:
    return load_job(db, job_id, user.id)


def cancel_generation(
    job_id: uuid.UUID, user: AppUser, db: Session, idempotency_key: str | None = None
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
            } and (
                not transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.CANCELLED,
                    "attempt.cancelled",
                    f"attempt:{attempt.id}:cancelled:v1",
                )
            ):
                raise ApiError(409, "ATTEMPT_STATE_CONFLICT", "任务尝试状态已变化")
        if job.settlement_status == SettlementStatus.RESERVED:
            finish_reservation(db, job, settle=False)
    complete(
        db, decision.record, result_type="generation_job", result_id=job.id, response_status=200
    )
    db.commit()
    return load_job(db, job.id, user.id)


def list_jobs(user: AppUser, db: Session) -> GenerationJobList:
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
