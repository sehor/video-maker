import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AdminOperationAudit,
    AppUser,
    DeadLetterEvent,
    DeadLetterSource,
    DeadLetterStatus,
    OutboxEvent,
    OutboxStatus,
    StorageCleanupEvent,
)

logger = structlog.get_logger()


def add_dead_letter(
    db: Session,
    *,
    source_type: DeadLetterSource,
    source_id: uuid.UUID,
    event_type: str,
    payload: dict[str, object],
    attempt_count: int,
    error: str,
) -> DeadLetterEvent:
    existing = db.scalar(
        select(DeadLetterEvent).where(
            DeadLetterEvent.source_type == source_type,
            DeadLetterEvent.source_id == source_id,
        )
    )
    if existing is not None:
        if existing.status == DeadLetterStatus.REPLAYED:
            existing.event_type = event_type
            existing.payload_json = payload
            existing.attempt_count = attempt_count
            existing.cycle_count += 1
            existing.last_error = error
            existing.status = DeadLetterStatus.OPEN
            existing.replayed_at = None
        return existing
    event = DeadLetterEvent(
        source_type=source_type,
        source_id=source_id,
        event_type=event_type,
        payload_json=payload,
        attempt_count=attempt_count,
        cycle_count=1,
        last_error=error,
        status=DeadLetterStatus.OPEN,
    )
    db.add(event)
    return event


def replay_dead_letter(
    db: Session,
    dead_letter_id: uuid.UUID,
    actor: AppUser,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DeadLetterEvent | None:
    dead_letter = db.scalar(
        select(DeadLetterEvent)
        .where(DeadLetterEvent.id == dead_letter_id)
        .with_for_update()
    )
    if dead_letter is None:
        return None
    if dead_letter.status == DeadLetterStatus.REPLAYED:
        return dead_letter

    now = clock()
    if dead_letter.source_type == DeadLetterSource.OUTBOX:
        source = db.get(OutboxEvent, dead_letter.source_id)
    else:
        source = db.get(StorageCleanupEvent, dead_letter.source_id)
    if source is None or source.status != OutboxStatus.DEAD_LETTER:
        raise RuntimeError("dead-letter source is missing or no longer replayable")

    source.status = OutboxStatus.PENDING
    source.attempt_count = 0
    source.next_attempt_at = now
    source.locked_at = None
    source.lock_token = None
    source.last_error = None
    dead_letter.status = DeadLetterStatus.REPLAYED
    dead_letter.replayed_at = now
    db.add(
        AdminOperationAudit(
            actor_user_id=actor.id,
            operation_type="dead_letter.replay",
            operation_key=(
                f"dead-letter:{dead_letter.id}:cycle:{dead_letter.cycle_count}:replay:v1"
            ),
            target_type="dead_letter_event",
            target_id=dead_letter.id,
            details_json={
                "source_type": dead_letter.source_type.value,
                "source_id": str(dead_letter.source_id),
                "attempt_count": dead_letter.attempt_count,
                "cycle_count": dead_letter.cycle_count,
            },
        )
    )
    db.commit()
    logger.info(
        "dead_letter.replayed",
        dead_letter_id=str(dead_letter.id),
        source_type=dead_letter.source_type.value,
        actor_user_id=str(actor.id),
    )
    return dead_letter
