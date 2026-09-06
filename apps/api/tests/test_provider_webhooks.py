import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.api as api_module
from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    GenerationOutput,
    JobEvent,
    JobStatus,
    LedgerTransaction,
    ProviderEventInbox,
    ProviderEventInboxStatus,
    SettlementStatus,
)
from app.provider import (
    CancelResult,
    FailureCode,
    MockVideoProvider,
    PollResult,
    ProviderAttempt,
    ProviderFailure,
    ProviderStatus,
    WebhookVerificationRequest,
)
from app.provider_execution import GenerationExecutionService
from app.storage import LocalObjectStorage
from tests.test_mock_jobs import create_shot, generate

WEBHOOK_SECRET = "test-webhook-secret"


def object_storage() -> LocalObjectStorage:
    settings = get_settings()
    return LocalObjectStorage(
        settings.storage_root,
        settings.storage_claim_secret.get_secret_value().encode(),
    )


def webhook_body(
    event_id: str, provider_job_id: str, status: ProviderStatus, **extra: str
) -> bytes:
    return json.dumps(
        {
            "event_id": event_id,
            "provider_job_id": provider_job_id,
            "status": status.value,
            **extra,
        },
        separators=(",", ":"),
    ).encode()


def signature(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def request_for(body: bytes) -> WebhookVerificationRequest:
    return WebhookVerificationRequest(
        headers={"x-provider-signature": signature(body)}, body=body
    )


class PendingProvider(MockVideoProvider):
    async def poll(self, attempt: ProviderAttempt) -> PollResult:
        return PollResult(
            status=ProviderStatus.PENDING,
            provider_job_id=attempt.provider_job_id,
        )


class PendingCancelProvider(PendingProvider):
    async def cancel(self, attempt: ProviderAttempt) -> CancelResult:
        return CancelResult(accepted=True, status=ProviderStatus.PENDING)


class LateSuccessProvider(PendingCancelProvider):
    def __init__(self, webhook_secret: str) -> None:
        super().__init__(webhook_secret=webhook_secret)
        self.succeeded = False

    async def poll(self, attempt: ProviderAttempt) -> PollResult:
        if not self.succeeded:
            return await super().poll(attempt)
        return await MockVideoProvider.poll(self, attempt)


def running_job(
    raw_client: TestClient, provider: MockVideoProvider
) -> tuple[dict, GenerationExecutionService]:
    queued = generate(raw_client, create_shot(raw_client)["id"], "success")
    executor = GenerationExecutionService(
        object_storage(), provider=provider
    )
    asyncio.run(executor.execute(uuid.UUID(queued["id"])))
    running = raw_client.get(f"/v1/generations/{queued['id']}").json()
    assert running["status"] == "RUNNING"
    assert running["attempts"][0]["status"] == "RUNNING"
    return running, executor


def stored_provider_job_id(job: dict) -> str:
    with SessionLocal() as db:
        provider_job_id = db.scalar(
            select(GenerationAttempt.provider_job_id)
            .where(GenerationAttempt.job_id == uuid.UUID(job["id"]))
            .order_by(GenerationAttempt.attempt_no.desc())
            .limit(1)
        )
    assert provider_job_id is not None
    return provider_job_id


def ledger_count(job_id: str, tx_type: str) -> int:
    with SessionLocal() as db:
        return (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(
                    LedgerTransaction.reference_id == job_id,
                    LedgerTransaction.tx_type == tx_type,
                )
            )
            or 0
        )


def test_webhook_requires_valid_signature_and_deduplicates_completion(
    raw_client: TestClient,
) -> None:
    running, _ = running_job(raw_client, PendingProvider())
    provider_job_id = stored_provider_job_id(running)
    body = webhook_body("evt-duplicate", provider_job_id, ProviderStatus.SUCCEEDED)

    rejected = raw_client.post(
        "/v1/provider-webhooks/mock",
        content=body,
        headers={"x-provider-signature": "invalid"},
    )
    assert rejected.status_code == 401
    oversized = raw_client.post(
        "/v1/provider-webhooks/mock",
        content=b"x" * (get_settings().provider_webhook_max_bytes + 1),
    )
    assert oversized.status_code == 413

    headers = {"x-provider-signature": signature(body)}
    first = raw_client.post("/v1/provider-webhooks/mock", content=body, headers=headers)
    duplicate = raw_client.post("/v1/provider-webhooks/mock", content=body, headers=headers)
    conflicting_body = webhook_body(
        "evt-duplicate",
        provider_job_id,
        ProviderStatus.FAILED,
        failure_code=FailureCode.WORKFLOW_FAILED.value,
    )
    conflicting = raw_client.post(
        "/v1/provider-webhooks/mock",
        content=conflicting_body,
        headers={"x-provider-signature": signature(conflicting_body)},
    )
    assert first.status_code == duplicate.status_code == 202
    assert first.json() == duplicate.json() == {
        "event_id": "evt-duplicate",
        "status": "PROCESSED",
    }
    assert conflicting.status_code == 202
    assert conflicting.json() == first.json()

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ProviderEventInbox)) == 1
        assert db.scalar(select(func.count()).select_from(GenerationOutput)) == 1
        assert db.scalar(
            select(func.count()).select_from(JobEvent).where(
                JobEvent.event_type == "provider.completed"
            )
        ) == 1
    assert ledger_count(running["id"], "SETTLE") == 1
    assert ledger_count(running["id"], "RELEASE") == 0


def test_poll_and_webhook_race_produces_one_final_result(raw_client: TestClient) -> None:
    class RacingProvider(PendingProvider):
        def __init__(self) -> None:
            super().__init__(webhook_secret=WEBHOOK_SECRET)
            self.armed = False
            self.pollers = 0
            self.ready = asyncio.Event()

        async def poll(self, attempt: ProviderAttempt) -> PollResult:
            if not self.armed:
                return await super().poll(attempt)
            self.pollers += 1
            if self.pollers == 2:
                self.ready.set()
            await self.ready.wait()
            return await MockVideoProvider.poll(self, attempt)

    provider = RacingProvider()
    running, executor = running_job(raw_client, provider)
    body = webhook_body(
        "evt-race", stored_provider_job_id(running), ProviderStatus.SUCCEEDED
    )
    provider.armed = True

    async def race() -> None:
        await asyncio.gather(
            executor.execute(uuid.UUID(running["id"])),
            executor.handle_webhook("mock", request_for(body)),
        )

    asyncio.run(race())
    with SessionLocal() as db:
        job = db.get(GenerationJob, uuid.UUID(running["id"]))
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED
        assert job.settlement_status == SettlementStatus.SETTLED
        assert db.scalar(
            select(func.count()).select_from(GenerationOutput).where(
                GenerationOutput.job_id == job.id
            )
        ) == 1
    assert ledger_count(running["id"], "SETTLE") == 1
    assert ledger_count(running["id"], "RELEASE") == 0


def test_stale_webhook_claim_is_reconciled_after_lease_expiry(
    raw_client: TestClient,
) -> None:
    provider = MockVideoProvider(webhook_secret=WEBHOOK_SECRET)
    running, _ = running_job(raw_client, PendingProvider())
    body = webhook_body(
        "evt-stale-claim",
        stored_provider_job_id(running),
        ProviderStatus.SUCCEEDED,
    )
    with SessionLocal() as db:
        db.add(
            ProviderEventInbox(
                provider_code="mock",
                external_event_id="evt-stale-claim",
                provider_job_id=stored_provider_job_id(running),
                provider_status=ProviderStatus.SUCCEEDED.value,
                payload_hash=hashlib.sha256(body).hexdigest(),
                status=ProviderEventInboxStatus.PROCESSING,
                locked_at=datetime.now(UTC) - timedelta(minutes=6),
                lock_token="stale-provider-event-claim",
            )
        )
        db.commit()

    recovered = GenerationExecutionService(
        object_storage(), provider=provider
    )
    result = asyncio.run(recovered.handle_webhook("mock", request_for(body)))
    assert result.status == ProviderEventInboxStatus.PROCESSED
    with SessionLocal() as db:
        job = db.get(GenerationJob, uuid.UUID(running["id"]))
        assert job is not None and job.status == JobStatus.SUCCEEDED


def test_late_old_attempt_event_cannot_rewrite_success(raw_client: TestClient) -> None:
    class RetryThenSuccessProvider(MockVideoProvider):
        def __init__(self) -> None:
            super().__init__(webhook_secret=WEBHOOK_SECRET)
            self.failed_attempts: set[uuid.UUID] = set()
            self.first_poll = True

        async def poll(self, attempt: ProviderAttempt) -> PollResult:
            if self.first_poll:
                self.first_poll = False
                self.failed_attempts.add(attempt.attempt_id)
                return PollResult(
                    status=ProviderStatus.FAILED,
                    provider_job_id=attempt.provider_job_id,
                    failure=ProviderFailure(
                        FailureCode.NETWORK_TIMEOUT, "temporary timeout"
                    ),
                )
            return await super().poll(attempt)

    queued = generate(raw_client, create_shot(raw_client)["id"], "success")
    provider = RetryThenSuccessProvider()
    executor = GenerationExecutionService(
        object_storage(), provider=provider
    )
    asyncio.run(executor.execute(uuid.UUID(queued["id"])))

    with SessionLocal() as db:
        attempts = list(
            db.scalars(
                select(GenerationAttempt)
                .where(GenerationAttempt.job_id == uuid.UUID(queued["id"]))
                .order_by(GenerationAttempt.attempt_no)
            )
        )
        assert [attempt.status for attempt in attempts] == [
            AttemptStatus.FAILED_RETRYABLE,
            AttemptStatus.SUCCEEDED,
        ]
        old_provider_job_id = attempts[0].provider_job_id
        job = db.get(GenerationJob, uuid.UUID(queued["id"]))
        assert job is not None
        final_output_id = job.final_output_id

    body = webhook_body("evt-old-attempt", old_provider_job_id, ProviderStatus.SUCCEEDED)
    result = asyncio.run(executor.handle_webhook("mock", request_for(body)))
    assert result.status == ProviderEventInboxStatus.PROCESSED
    with SessionLocal() as db:
        job = db.get(GenerationJob, uuid.UUID(queued["id"]))
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED
        assert job.final_output_id == final_output_id
        assert db.scalar(
            select(func.count()).select_from(GenerationOutput).where(
                GenerationOutput.job_id == job.id
            )
        ) == 1
    assert ledger_count(queued["id"], "SETTLE") == 1
    assert ledger_count(queued["id"], "RELEASE") == 0


def test_submitted_cancel_waits_for_provider_confirmation(
    raw_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = PendingCancelProvider(webhook_secret=WEBHOOK_SECRET)
    running, executor = running_job(raw_client, provider)
    monkeypatch.setattr(api_module, "provider_executor", lambda _: executor)

    response = raw_client.post(
        f"/v1/generations/{running['id']}/cancel",
        headers={"Idempotency-Key": "cancel-running-job"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCEL_REQUESTED"
    assert response.json()["settlement_status"] == "RESERVED"
    assert ledger_count(running["id"], "RELEASE") == 0

    body = webhook_body(
        "evt-cancelled",
        stored_provider_job_id(running),
        ProviderStatus.CANCELLED,
    )
    asyncio.run(executor.handle_webhook("mock", request_for(body)))
    cancelled = raw_client.get(f"/v1/generations/{running['id']}").json()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["settlement_status"] == "RELEASED"
    assert ledger_count(running["id"], "RELEASE") == 1
    assert ledger_count(running["id"], "SETTLE") == 0


def test_late_success_after_cancel_request_settles_once(
    raw_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = LateSuccessProvider(webhook_secret=WEBHOOK_SECRET)
    running, executor = running_job(raw_client, provider)
    monkeypatch.setattr(api_module, "provider_executor", lambda _: executor)

    response = raw_client.post(
        f"/v1/generations/{running['id']}/cancel",
        headers={"Idempotency-Key": "cancel-before-late-success"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCEL_REQUESTED"

    provider.succeeded = True
    provider_job_id = stored_provider_job_id(running)
    success = webhook_body("evt-late-success", provider_job_id, ProviderStatus.SUCCEEDED)
    asyncio.run(executor.handle_webhook("mock", request_for(success)))
    late_cancel = webhook_body("evt-late-cancel", provider_job_id, ProviderStatus.CANCELLED)
    asyncio.run(executor.handle_webhook("mock", request_for(late_cancel)))

    completed = raw_client.get(f"/v1/generations/{running['id']}").json()
    assert completed["status"] == "SUCCEEDED"
    assert completed["settlement_status"] == "SETTLED"
    assert ledger_count(running["id"], "SETTLE") == 1
    assert ledger_count(running["id"], "RELEASE") == 0


def test_duplicate_final_failure_releases_once(raw_client: TestClient) -> None:
    provider = PendingProvider(webhook_secret=WEBHOOK_SECRET)
    running, executor = running_job(raw_client, provider)
    body = webhook_body(
        "evt-failed",
        stored_provider_job_id(running),
        ProviderStatus.FAILED,
        failure_code=FailureCode.WORKFLOW_FAILED.value,
    )
    first = asyncio.run(executor.handle_webhook("mock", request_for(body)))
    duplicate = asyncio.run(executor.handle_webhook("mock", request_for(body)))
    assert first.status == duplicate.status == ProviderEventInboxStatus.PROCESSED
    with SessionLocal() as db:
        job = db.get(GenerationJob, uuid.UUID(running["id"]))
        assert job is not None
        assert job.status == JobStatus.FAILED_FINAL
        assert job.settlement_status == SettlementStatus.RELEASED
    assert ledger_count(running["id"], "RELEASE") == 1
    assert ledger_count(running["id"], "SETTLE") == 0


pytestmark = pytest.mark.database
