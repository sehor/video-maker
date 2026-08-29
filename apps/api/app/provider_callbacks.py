import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import ProviderEventInbox, ProviderEventInboxStatus
from app.provider import ProviderEvent
from app.provider_execution_context import AttemptContext

PROVIDER_EVENT_LEASE = timedelta(minutes=5)
logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class ProviderWebhookResult:
    event_id: str
    status: ProviderEventInboxStatus


class ProviderCallbackService:
    """Owns durable webhook receipt, leasing, deduplication, and completion marks."""

    _session_factory: sessionmaker[Session]

    def _receive_provider_event(
        self,
        provider_code: str,
        body: bytes,
        event: ProviderEvent,
    ) -> tuple[uuid.UUID, str | None]:
        payload_hash = hashlib.sha256(body).hexdigest()
        now = datetime.now(UTC)
        lock_token = str(uuid.uuid4())
        with self._session_factory() as db:
            inbox = db.scalar(
                select(ProviderEventInbox).where(
                    ProviderEventInbox.provider_code == provider_code,
                    ProviderEventInbox.external_event_id == event.event_id,
                )
            )
            if inbox is None:
                inbox = ProviderEventInbox(
                    provider_code=provider_code,
                    external_event_id=event.event_id,
                    provider_job_id=event.provider_job_id,
                    provider_status=event.status.value,
                    payload_hash=payload_hash,
                    failure_code=event.failure.code.value if event.failure else None,
                    status=ProviderEventInboxStatus.RECEIVED,
                )
                db.add(inbox)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    inbox = db.scalar(
                        select(ProviderEventInbox).where(
                            ProviderEventInbox.provider_code == provider_code,
                            ProviderEventInbox.external_event_id == event.event_id,
                        )
                    )
                    if inbox is None:
                        raise
            if inbox.payload_hash != payload_hash:
                logger.warning(
                    "provider.webhook_event_conflict",
                    provider=provider_code,
                    event_id=event.event_id,
                )
                return inbox.id, None
            changed = db.execute(
                update(ProviderEventInbox)
                .where(
                    ProviderEventInbox.id == inbox.id,
                    (
                        (ProviderEventInbox.status == ProviderEventInboxStatus.RECEIVED)
                        | (
                            (ProviderEventInbox.status == ProviderEventInboxStatus.PROCESSING)
                            & (ProviderEventInbox.locked_at < now - PROVIDER_EVENT_LEASE)
                        )
                    ),
                )
                .values(
                    status=ProviderEventInboxStatus.PROCESSING,
                    locked_at=now,
                    lock_token=lock_token,
                )
                .execution_options(synchronize_session=False)
            )
            db.commit()
            return inbox.id, lock_token if changed.rowcount == 1 else None

    def _reset_provider_event(self, event_id: uuid.UUID, lock_token: str) -> None:
        with self._session_factory() as db:
            db.execute(
                update(ProviderEventInbox)
                .where(
                    ProviderEventInbox.id == event_id,
                    ProviderEventInbox.status == ProviderEventInboxStatus.PROCESSING,
                    ProviderEventInbox.lock_token == lock_token,
                )
                .values(
                    status=ProviderEventInboxStatus.RECEIVED,
                    locked_at=None,
                    lock_token=None,
                )
            )
            db.commit()

    def _finish_provider_event(
        self, event_id: uuid.UUID, lock_token: str, context: AttemptContext
    ) -> None:
        with self._session_factory() as db:
            db.execute(
                update(ProviderEventInbox)
                .where(
                    ProviderEventInbox.id == event_id,
                    ProviderEventInbox.status == ProviderEventInboxStatus.PROCESSING,
                    ProviderEventInbox.lock_token == lock_token,
                )
                .values(
                    status=ProviderEventInboxStatus.PROCESSED,
                    attempt_id=context.attempt_id,
                    job_id=context.job_id,
                    locked_at=None,
                    lock_token=None,
                    processed_at=datetime.now(UTC),
                )
            )
            db.commit()

    def _webhook_result(self, event_id: uuid.UUID) -> ProviderWebhookResult:
        with self._session_factory() as db:
            inbox = db.get(ProviderEventInbox, event_id)
            if inbox is None:
                raise RuntimeError("provider webhook inbox row disappeared")
            return ProviderWebhookResult(
                event_id=inbox.external_event_id,
                status=inbox.status,
            )
