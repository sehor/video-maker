import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.errors import ApiError
from app.ledger import finish_reservation, utcnow
from app.models import (
    GenerationBatch,
    GenerationJob,
    LedgerPosting,
    LedgerTransaction,
    OutboxEvent,
    Quote,
    QuoteStatus,
    SettlementStatus,
)
from app.outbox import DispatchResult, OutboxDispatcher
from tests.conftest import InlineWorkflowStarter


def create_project_shots(client: TestClient, count: int) -> list[dict]:
    project_response = client.post(
        "/v1/projects",
        json={"name": f"batch-{uuid.uuid4()}", "description": "batch test"},
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["id"]
    shots = []
    for index in range(count):
        response = client.post(
            f"/v1/projects/{project_id}/shots",
            json={
                "title": f"shot-{index}",
                "prompt": "海边日落",
                "duration_seconds": 2,
                "aspect_ratio": "16:9",
            },
        )
        assert response.status_code == 201
        shots.append(response.json())
    return shots


def grant(client: TestClient, amount_ms: int) -> None:
    response = client.post(
        "/v1/wallet/test-grants",
        json={
            "tier": "FAST",
            "amount_ms": amount_ms,
            "idempotency_key": f"batch-grant:{uuid.uuid4()}",
            "reason": "batch atomic test",
        },
    )
    assert response.status_code == 201


def quote(client: TestClient, shot_id: str, tier: str = "FAST") -> dict:
    response = client.post(
        "/v1/quotes",
        json={"shot_id": shot_id, "tier": tier, "resolution": "720P"},
    )
    assert response.status_code == 201
    return response.json()


def batch_payload(quotes: list[dict]) -> dict:
    return {"items": [{"quote_id": item["id"]} for item in quotes]}


def test_batch_creates_jobs_reserve_and_outboxes_in_one_transaction(
    client: TestClient,
) -> None:
    shots = create_project_shots(client, 2)
    grant(client, 4_000)
    quotes = [quote(client, shot["id"]) for shot in shots]
    headers = {"Idempotency-Key": "batch:create:atomic"}

    response = client.post("/v1/batches", headers=headers, json=batch_payload(quotes))
    assert response.status_code == 202
    batch = response.json()
    assert batch["status"] == "QUEUED"
    assert batch["ledger_unit"] == "FAST_MS"
    assert batch["reserved_amount_ms"] == 4_000
    assert len(batch["jobs"]) == 2
    assert {job["status"] for job in batch["jobs"]} == {"QUEUED"}
    assert {job["settlement_status"] for job in batch["jobs"]} == {"RESERVED"}

    replay = client.post("/v1/batches", headers=headers, json=batch_payload(quotes))
    assert replay.status_code == 202
    assert replay.json()["id"] == batch["id"]

    with SessionLocal() as db:
        stored = db.get(GenerationBatch, uuid.UUID(batch["id"]))
        assert stored is not None
        jobs = list(db.scalars(select(GenerationJob).where(GenerationJob.batch_id == stored.id)))
        assert len(jobs) == 2
        assert {job.reserved_tx_id for job in jobs} == {stored.reserved_tx_id}
        assert db.scalar(
            select(func.count()).select_from(LedgerTransaction).where(
                LedgerTransaction.tx_type == "RESERVE",
                LedgerTransaction.reference_type == "batch",
                LedgerTransaction.reference_id == str(stored.id),
            )
        ) == 1
        assert db.scalar(
            select(func.sum(LedgerPosting.amount_ms)).where(
                LedgerPosting.transaction_id == stored.reserved_tx_id
            )
        ) == 0
        assert db.scalar(
            select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.job_id.in_([job.id for job in jobs])
            )
        ) == 2
        assert {
            db.get(Quote, uuid.UUID(item["id"])).status for item in quotes
        } == {QuoteStatus.USED}

    wallet = client.get("/v1/wallet").json()["balances"]["FAST_MS"]
    assert wallet["USER_AVAILABLE"] == 0
    assert wallet["USER_RESERVED"] == 4_000
    assert client.get(f"/v1/batches/{batch['id']}").status_code == 200


def test_expired_quote_rolls_back_entire_batch(client: TestClient, monkeypatch) -> None:
    shots = create_project_shots(client, 2)
    grant(client, 4_000)
    quotes = [quote(client, shot["id"]) for shot in shots]
    monkeypatch.setattr("app.ledger.utcnow", lambda: utcnow() + timedelta(minutes=16))

    response = client.post("/v1/batches", json=batch_payload(quotes))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "QUOTE_EXPIRED"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(GenerationBatch)) == 0
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.scalar(
            select(func.count()).select_from(LedgerTransaction).where(
                LedgerTransaction.tx_type == "RESERVE"
            )
        ) == 0
        assert {
            db.get(Quote, uuid.UUID(item["id"])).status for item in quotes
        } == {QuoteStatus.OPEN}


def test_insufficient_balance_never_partially_freezes_batch(client: TestClient) -> None:
    shots = create_project_shots(client, 2)
    grant(client, 2_000)
    quotes = [quote(client, shot["id"]) for shot in shots]

    response = client.post("/v1/batches", json=batch_payload(quotes))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WALLET_INSUFFICIENT"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(GenerationBatch)) == 0
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.scalar(
            select(func.count()).select_from(LedgerTransaction).where(
                LedgerTransaction.tx_type == "RESERVE"
            )
        ) == 0
        assert {
            db.get(Quote, uuid.UUID(item["id"])).status for item in quotes
        } == {QuoteStatus.OPEN}
    wallet = client.get("/v1/wallet").json()["balances"]["FAST_MS"]
    assert wallet["USER_AVAILABLE"] == 2_000
    assert wallet["USER_RESERVED"] == 0


def test_batch_requires_one_project_and_ledger_unit(client: TestClient) -> None:
    first_project_quote = quote(client, create_project_shots(client, 1)[0]["id"])
    second_project_quote = quote(client, create_project_shots(client, 1)[0]["id"])
    project_mismatch = client.post(
        "/v1/batches",
        json=batch_payload([first_project_quote, second_project_quote]),
    )
    assert project_mismatch.status_code == 422
    assert project_mismatch.json()["error"]["code"] == "BATCH_PROJECT_MISMATCH"

    shots = create_project_shots(client, 2)
    fast_quote = quote(client, shots[0]["id"], "FAST")
    studio_quote = quote(client, shots[1]["id"], "STUDIO")
    unit_mismatch = client.post(
        "/v1/batches",
        json=batch_payload([fast_quote, studio_quote]),
    )
    assert unit_mismatch.status_code == 422
    assert unit_mismatch.json()["error"]["code"] == "BATCH_LEDGER_UNIT_MISMATCH"

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(GenerationBatch)) == 0
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.scalar(
            select(func.count()).select_from(LedgerTransaction).where(
                LedgerTransaction.tx_type == "RESERVE"
            )
        ) == 0
        assert db.scalar(
            select(func.count()).select_from(Quote).where(Quote.status == QuoteStatus.USED)
        ) == 0


def test_batch_cannot_cross_user_boundary(client: TestClient) -> None:
    shots = create_project_shots(client, 2)
    quotes = [quote(client, shot["id"]) for shot in shots]
    other_user = {"x-test-user": "other-user"}

    rejected = client.post(
        "/v1/batches",
        headers=other_user,
        json=batch_payload(quotes),
    )
    assert rejected.status_code == 404
    with SessionLocal() as db:
        assert {
            db.get(Quote, uuid.UUID(item["id"])).status for item in quotes
        } == {QuoteStatus.OPEN}

    grant(client, 4_000)
    created = client.post("/v1/batches", json=batch_payload(quotes))
    assert created.status_code == 202
    hidden = client.get(f"/v1/batches/{created.json()['id']}", headers=other_user)
    assert hidden.status_code == 404


def test_batch_jobs_settle_and_release_independently(client: TestClient) -> None:
    shots = create_project_shots(client, 2)
    grant(client, 4_000)
    quotes = [quote(client, shot["id"]) for shot in shots]
    response = client.post(
        "/v1/batches",
        headers={"x-test-generation-modes": "success,failure"},
        json=batch_payload(quotes),
    )
    assert response.status_code == 202
    batch_id = response.json()["id"]
    dispatcher = OutboxDispatcher(SessionLocal, InlineWorkflowStarter())
    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.PUBLISHED
    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.PUBLISHED
    assert asyncio.run(dispatcher.dispatch_once()) == DispatchResult.IDLE

    batch = client.get(f"/v1/batches/{batch_id}").json()
    assert batch["status"] == "PARTIAL"
    assert {job["status"] for job in batch["jobs"]} == {"SUCCEEDED", "FAILED_FINAL"}
    assert {job["settlement_status"] for job in batch["jobs"]} == {"SETTLED", "RELEASED"}
    wallet = client.get("/v1/wallet").json()["balances"]["FAST_MS"]
    assert wallet["USER_AVAILABLE"] == 2_000
    assert wallet["USER_RESERVED"] == 0

    with SessionLocal() as db:
        jobs = list(
            db.scalars(
                select(GenerationJob)
                .where(GenerationJob.batch_id == uuid.UUID(batch_id))
                .order_by(GenerationJob.created_at, GenerationJob.id)
            )
        )
        assert sorted(
            db.scalars(
                select(LedgerTransaction.tx_type).where(
                    LedgerTransaction.reference_type == "job",
                    LedgerTransaction.reference_id.in_([str(job.id) for job in jobs]),
                )
            )
        ) == ["RELEASE", "SETTLE"]
        for job in jobs:
            same_action = job.settlement_status == SettlementStatus.SETTLED
            assert finish_reservation(db, job, settle=same_action) is False
            with pytest.raises(ApiError) as error:
                finish_reservation(db, job, settle=not same_action)
            assert error.value.code == "LEDGER_ALREADY_FINALIZED"


def test_competing_batches_cannot_overdraw_wallet(client: TestClient) -> None:
    shots = create_project_shots(client, 4)
    grant(client, 4_000)
    quotes = [quote(client, shot["id"]) for shot in shots]
    payloads = [batch_payload(quotes[:2]), batch_payload(quotes[2:])]

    def request(payload: dict) -> tuple[int, str | None]:
        response = client.post("/v1/batches", json=payload)
        return response.status_code, response.json().get("error", {}).get("code")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, payloads))

    assert sorted(status for status, _ in results) == [202, 409]
    assert {error for _, error in results if error} == {"WALLET_INSUFFICIENT"}
    wallet = client.get("/v1/wallet").json()["balances"]["FAST_MS"]
    assert wallet["USER_AVAILABLE"] == 0
    assert wallet["USER_RESERVED"] == 4_000
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(GenerationBatch)) == 1
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 2
        assert db.scalar(
            select(func.count()).select_from(LedgerTransaction).where(
                LedgerTransaction.tx_type == "RESERVE"
            )
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(Quote).where(Quote.status == QuoteStatus.USED)
        ) == 2
