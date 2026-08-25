import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.errors import ApiError
from app.ledger import finish_reservation
from app.models import (
    GenerationJob,
    LedgerPosting,
    LedgerTransaction,
    PriceVersion,
    Quote,
    QuoteStatus,
    SettlementStatus,
    WalletBalance,
)
from app.provider import MockVideoProvider
from tests.test_mock_jobs import create_shot


def grant(client: TestClient, amount_ms: int, key: str | None = None) -> dict:
    response = client.post(
        "/v1/wallet/test-grants",
        json={
            "tier": "FAST",
            "amount_ms": amount_ms,
            "idempotency_key": key or f"grant:{uuid.uuid4()}",
            "reason": "quote ledger test",
        },
    )
    assert response.status_code == 201
    return response.json()


def quote(client: TestClient, shot_id: str, tier: str = "FAST") -> dict:
    response = client.post(
        "/v1/quotes",
        json={
            "shot_id": shot_id,
            "tier": tier,
            "resolution": "720P",
            "variant_count": 1,
        },
    )
    assert response.status_code == 201
    return response.json()


def submit(client: TestClient, shot_id: str, quote_id: str, mode: str = "success"):
    return client.post(
        "/v1/generations",
        json={"shot_id": shot_id, "quote_id": quote_id, "mock_mode": mode},
    )


def refresh(client: TestClient, response) -> dict:
    assert response.status_code == 202
    return client.get(f"/v1/generations/{response.json()['id']}").json()


def test_quote_snapshot_keeps_its_price_when_catalog_changes(
    client: TestClient, monkeypatch
) -> None:
    async def stay_queued(self, job_id):
        return None

    monkeypatch.setattr(MockVideoProvider, "submit", stay_queued)
    shot = create_shot(client)
    first = quote(client, shot["id"])
    assert first["reserved_ms"] == 2_000

    unavailable = client.post(
        "/v1/quotes",
        json={
            "shot_id": shot["id"],
            "tier": "STUDIO",
            "resolution": "1080P",
            "variant_count": 1,
        },
    )
    assert unavailable.status_code == 422
    assert unavailable.json()["error"]["code"] == "TIER_UNAVAILABLE"

    with SessionLocal() as db:
        db.add(
            PriceVersion(
                tier_code="FAST",
                version=2,
                resolution="720P",
                charge_numerator=2,
                charge_denominator=1,
                enabled=True,
            )
        )
        db.commit()

    second = quote(client, shot["id"])
    assert second["reserved_ms"] == 4_000
    grant(client, 2_000)
    job = refresh(client, submit(client, shot["id"], first["id"]))
    assert job["quote_snapshot"]["reserved_ms"] == 2_000
    assert job["reserved_ms"] == 2_000


def test_success_settles_once_and_every_transaction_is_balanced(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 5_000, "grant:balanced-once")
    grant(client, 5_000, "grant:balanced-once")
    item = quote(client, shot["id"])
    job = refresh(client, submit(client, shot["id"], item["id"]))

    assert job["status"] == "SUCCEEDED"
    assert job["settlement_status"] == "SETTLED"
    wallet = client.get("/v1/wallet").json()["balances"]["FAST_MS"]
    assert wallet["USER_AVAILABLE"] == 3_000
    assert wallet["USER_RESERVED"] == 0

    replay = submit(client, shot["id"], item["id"])
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "QUOTE_ALREADY_USED"

    with SessionLocal() as db:
        stored_job = db.get(GenerationJob, uuid.UUID(job["id"]))
        assert stored_job is not None
        assert finish_reservation(db, stored_job, settle=True) is False
        db.commit()
        assert db.execute(
            select(LedgerPosting.transaction_id)
            .group_by(LedgerPosting.transaction_id, LedgerPosting.unit)
            .having(func.sum(LedgerPosting.amount_ms) != 0)
        ).all() == []
        for account_id, balance_ms in db.execute(
            select(WalletBalance.account_id, WalletBalance.balance_ms)
        ):
            posting_total = db.scalar(
                select(func.coalesce(func.sum(LedgerPosting.amount_ms), 0)).where(
                    LedgerPosting.account_id == account_id
                )
            )
            assert posting_total == balance_ms
        assert (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(LedgerTransaction.tx_type == "SETTLE")
            )
            == 1
        )


def test_final_failure_releases_reserved_seconds_once(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    job = refresh(client, submit(client, shot["id"], item["id"], "failure"))
    assert job["status"] == "FAILED_FINAL"
    assert job["settlement_status"] == "RELEASED"
    wallet = client.get("/v1/wallet").json()["balances"]["FAST_MS"]
    assert wallet["USER_AVAILABLE"] == 2_000
    assert wallet["USER_RESERVED"] == 0

    with SessionLocal() as db:
        stored_job = db.get(GenerationJob, uuid.UUID(job["id"]))
        assert stored_job is not None
        assert finish_reservation(db, stored_job, settle=False) is False
        db.commit()
        assert (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(LedgerTransaction.tx_type == "RELEASE")
            )
            == 1
        )


def test_insufficient_balance_rolls_back_quote_and_job(client: TestClient, monkeypatch) -> None:
    async def stay_queued(self, job_id):
        return None

    monkeypatch.setattr(MockVideoProvider, "submit", stay_queued)
    shot = create_shot(client)
    grant(client, 1_000)
    item = quote(client, shot["id"])
    response = submit(client, shot["id"], item["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WALLET_INSUFFICIENT"

    with SessionLocal() as db:
        stored_quote = db.get(Quote, uuid.UUID(item["id"]))
        assert stored_quote is not None and stored_quote.status == QuoteStatus.OPEN
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0


def test_competing_submissions_cannot_overdraw(client: TestClient, monkeypatch) -> None:
    async def stay_queued(self, job_id):
        return None

    monkeypatch.setattr(MockVideoProvider, "submit", stay_queued)
    shot = create_shot(client)
    grant(client, 2_000)
    quotes = [quote(client, shot["id"]) for _ in range(2)]

    def request(item: dict) -> tuple[int, str | None]:
        response = submit(client, shot["id"], item["id"])
        return response.status_code, response.json().get("error", {}).get("code")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, quotes))

    assert sorted(status for status, _ in results) == [202, 409]
    assert {error for _, error in results if error} == {"WALLET_INSUFFICIENT"}
    wallet = client.get("/v1/wallet").json()["balances"]["FAST_MS"]
    assert wallet["USER_AVAILABLE"] == 0
    assert wallet["USER_RESERVED"] == 2_000


def test_grant_idempotency_key_rejects_different_amount(client: TestClient) -> None:
    grant(client, 1_000, "grant:conflict")
    response = client.post(
        "/v1/wallet/test-grants",
        json={
            "tier": "FAST",
            "amount_ms": 2_000,
            "idempotency_key": "grant:conflict",
            "reason": "quote ledger test",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LEDGER_IDEMPOTENCY_CONFLICT"


def test_quote_cannot_cross_user_boundary(client: TestClient, monkeypatch) -> None:
    async def stay_queued(self, job_id):
        return None

    monkeypatch.setattr(MockVideoProvider, "submit", stay_queued)
    shot = create_shot(client)
    item = quote(client, shot["id"])
    response = client.post(
        "/v1/generations",
        headers={"x-test-user": "other"},
        json={"shot_id": shot["id"], "quote_id": item["id"], "mock_mode": "success"},
    )
    assert response.status_code == 404


def test_settle_and_release_are_mutually_exclusive(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    job = refresh(client, submit(client, shot["id"], item["id"]))
    assert job["settlement_status"] == SettlementStatus.SETTLED.value

    with SessionLocal() as db:
        stored_job = db.get(GenerationJob, uuid.UUID(job["id"]))
        assert stored_job is not None
        with pytest.raises(ApiError) as error:
            finish_reservation(db, stored_job, settle=False)
        assert error.value.code == "LEDGER_ALREADY_FINALIZED"
