from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    JobEvent,
    JobStatus,
)

JOB_TERMINAL_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED_FINAL,
        JobStatus.CANCELLED,
        JobStatus.EXPIRED,
        JobStatus.REJECTED_POLICY,
    }
)
ATTEMPT_TERMINAL_STATUSES = frozenset(
    {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED_RETRYABLE,
        AttemptStatus.FAILED_FINAL,
        AttemptStatus.CANCELLED,
        AttemptStatus.TIMED_OUT,
    }
)

JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.CREATED: frozenset(
        {
            JobStatus.RESERVED,
            JobStatus.CANCELLED,
            JobStatus.EXPIRED,
            JobStatus.REJECTED_POLICY,
        }
    ),
    JobStatus.RESERVED: frozenset(
        {JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.EXPIRED}
    ),
    JobStatus.QUEUED: frozenset(
        {
            JobStatus.ROUTING,
            JobStatus.FAILED_FINAL,
            JobStatus.CANCELLED,
            JobStatus.EXPIRED,
            JobStatus.REJECTED_POLICY,
        }
    ),
    JobStatus.ROUTING: frozenset(
        {
            JobStatus.SUBMITTED,
            JobStatus.FAILED_FINAL,
            JobStatus.CANCELLED,
            JobStatus.EXPIRED,
            JobStatus.REJECTED_POLICY,
        }
    ),
    JobStatus.SUBMITTED: frozenset(
        {
            JobStatus.RUNNING,
            JobStatus.FAILED_FINAL,
            JobStatus.CANCELLED,
            JobStatus.EXPIRED,
        }
    ),
    JobStatus.RUNNING: frozenset(
        {JobStatus.POSTPROCESSING, JobStatus.FAILED_FINAL, JobStatus.CANCELLED}
    ),
    JobStatus.POSTPROCESSING: frozenset(
        {JobStatus.VALIDATING, JobStatus.FAILED_FINAL, JobStatus.CANCELLED}
    ),
    JobStatus.VALIDATING: frozenset(
        {JobStatus.SUCCEEDED, JobStatus.FAILED_FINAL, JobStatus.CANCELLED}
    ),
    **{status: frozenset() for status in JOB_TERMINAL_STATUSES},
}

_ATTEMPT_FAILURES = frozenset(
    {
        AttemptStatus.FAILED_RETRYABLE,
        AttemptStatus.FAILED_FINAL,
        AttemptStatus.CANCELLED,
        AttemptStatus.TIMED_OUT,
    }
)
ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.CREATED: frozenset({AttemptStatus.SUBMITTING, AttemptStatus.CANCELLED}),
    AttemptStatus.SUBMITTING: frozenset({AttemptStatus.SUBMITTED, *_ATTEMPT_FAILURES}),
    AttemptStatus.SUBMITTED: frozenset({AttemptStatus.RUNNING, *_ATTEMPT_FAILURES}),
    AttemptStatus.RUNNING: frozenset({AttemptStatus.SUCCEEDED, *_ATTEMPT_FAILURES}),
    **{status: frozenset() for status in ATTEMPT_TERMINAL_STATUSES},
}


def _event_exists(db: Session, dedup_key: str) -> bool:
    return db.scalar(select(JobEvent.id).where(JobEvent.dedup_key == dedup_key)) is not None


def transition_job(
    db: Session,
    job: GenerationJob,
    to_status: JobStatus,
    event_type: str,
    dedup_key: str,
    payload: Mapping[str, Any] | None = None,
) -> bool:
    from_status = job.status
    if _event_exists(db, dedup_key) or to_status not in JOB_TRANSITIONS[from_status]:
        return False
    changed = db.execute(
        update(GenerationJob)
        .where(GenerationJob.id == job.id, GenerationJob.status == from_status)
        .values(status=to_status)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        db.expire(job, ["status"])
        return False
    set_committed_value(job, "status", to_status)
    db.add(
        JobEvent(
            job_id=job.id,
            event_type=event_type,
            from_status=from_status.value,
            to_status=to_status.value,
            dedup_key=dedup_key,
            payload_json=dict(payload or {}),
        )
    )
    return True


def transition_attempt(
    db: Session,
    attempt: GenerationAttempt,
    to_status: AttemptStatus,
    event_type: str,
    dedup_key: str,
    payload: Mapping[str, Any] | None = None,
) -> bool:
    from_status = attempt.status
    if _event_exists(db, dedup_key) or to_status not in ATTEMPT_TRANSITIONS[from_status]:
        return False
    changed = db.execute(
        update(GenerationAttempt)
        .where(GenerationAttempt.id == attempt.id, GenerationAttempt.status == from_status)
        .values(status=to_status)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        db.expire(attempt, ["status"])
        return False
    set_committed_value(attempt, "status", to_status)
    db.add(
        JobEvent(
            job_id=attempt.job_id,
            attempt_id=attempt.id,
            event_type=event_type,
            from_status=from_status.value,
            to_status=to_status.value,
            dedup_key=dedup_key,
            payload_json=dict(payload or {}),
        )
    )
    return True


# Kept as the original public name while callers migrate to the explicit name.
transition = transition_job
