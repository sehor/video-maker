from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import AttemptStatus, GenerationAttempt, JobEvent
from app.provider_execution_context import AttemptContext


@dataclass(frozen=True, slots=True)
class ProviderPollingPolicy:
    initial_delay: timedelta = timedelta(seconds=1)
    maximum_delay: timedelta = timedelta(seconds=30)
    deadline: timedelta = timedelta(minutes=30)
    maximum_polls: int = 120

    def __post_init__(self) -> None:
        if self.initial_delay <= timedelta(0):
            raise ValueError("initial polling delay must be positive")
        if self.maximum_delay < self.initial_delay:
            raise ValueError("maximum polling delay must not be shorter than initial delay")
        if self.deadline <= timedelta(0):
            raise ValueError("polling deadline must be positive")
        if self.maximum_polls <= 0:
            raise ValueError("maximum polls must be positive")

    def delay_after(self, poll_count: int) -> timedelta:
        if poll_count <= 0:
            raise ValueError("poll count must be positive")
        multiplier = 1 << min(poll_count - 1, 30)
        return min(self.initial_delay * multiplier, self.maximum_delay)


@dataclass(frozen=True, slots=True)
class PollReservation:
    poll_count: int
    acquired: bool
    exhausted: bool


class ProviderPollingService:
    """Persists polling leases and budgets before external provider calls."""

    _session_factory: sessionmaker[Session]
    _polling: ProviderPollingPolicy
    _clock: Callable[[], datetime]

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        polling: ProviderPollingPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._polling = polling
        self._clock = clock

    def reserve_poll(self, context: AttemptContext) -> PollReservation:
        """Persist one poll slot before the external call so crashes consume its budget."""

        with self._session_factory() as db:
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if attempt is None:
                return PollReservation(0, acquired=False, exhausted=False)
            poll_count = (
                db.scalar(
                    select(func.count())
                    .select_from(JobEvent)
                    .where(
                        JobEvent.attempt_id == attempt.id,
                        JobEvent.event_type == "provider.poll_started",
                    )
                )
                or 0
            )
            created_at = attempt.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            deadline_at = created_at.astimezone(UTC) + self._polling.deadline
            if attempt.status not in {
                AttemptStatus.SUBMITTING,
                AttemptStatus.SUBMITTED,
                AttemptStatus.RUNNING,
            }:
                return PollReservation(poll_count, acquired=False, exhausted=False)
            if self.now() >= deadline_at or poll_count >= self._polling.maximum_polls:
                return PollReservation(poll_count, acquired=False, exhausted=True)

            next_poll_count = poll_count + 1
            db.add(
                JobEvent(
                    job_id=attempt.job_id,
                    attempt_id=attempt.id,
                    event_type="provider.poll_started",
                    from_status=attempt.status.value,
                    to_status=attempt.status.value,
                    dedup_key=f"attempt:{attempt.id}:poll:{next_poll_count}:v1",
                    payload_json={
                        "poll_count": next_poll_count,
                        "deadline_at": deadline_at.isoformat(),
                    },
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return PollReservation(next_poll_count, acquired=False, exhausted=False)
            return PollReservation(next_poll_count, acquired=True, exhausted=False)

    def now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("provider execution clock must be timezone-aware")
        return now.astimezone(UTC)
