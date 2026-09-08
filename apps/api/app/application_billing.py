from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.application_queries import load_ledger_transaction, load_quote, owned_shot
from app.config import get_settings
from app.errors import not_found
from app.idempotency import acquire, complete, replay_result_id
from app.ledger import create_quote, grant_seconds, wallet_balances
from app.models import AppUser, LedgerAccount, LedgerPosting, LedgerTransaction
from app.schemas import (
    LedgerTransactionList,
    QuoteCreate,
    TestGrantCreate,
    WalletOut,
)


def create_generation_quote(
    payload: QuoteCreate, user: AppUser, db: Session, idempotency_key: str | None = None
):
    decision = acquire(
        db,
        user_id=user.id,
        scope="POST:/v1/quotes",
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
    )
    replay_id = replay_result_id(decision, "generation_quote")
    if replay_id is not None:
        return load_quote(db, replay_id, user.id)
    shot = owned_shot(db, payload.shot_id, user.id, for_update=True)
    quote = create_quote(
        db,
        user,
        shot,
        tier_code=payload.tier,
        resolution=payload.resolution,
        variant_count=payload.variant_count,
    )
    complete(
        db, decision.record, result_type="generation_quote", result_id=quote.id, response_status=201
    )
    db.commit()
    db.refresh(quote)
    return quote


def create_test_grant(
    payload: TestGrantCreate, user: AppUser, db: Session, idempotency_key: str | None = None
) -> LedgerTransaction:
    if get_settings().environment == "production":
        raise not_found("endpoint")
    decision = acquire(
        db,
        user_id=user.id,
        scope="POST:/v1/wallet/test-grants",
        key=idempotency_key,
        payload=payload.model_dump(mode="json"),
    )
    replay_id = replay_result_id(decision, "ledger_transaction")
    if replay_id is not None:
        return load_ledger_transaction(db, replay_id, user.id)
    transaction = grant_seconds(
        db,
        user,
        tier_code=payload.tier,
        amount_ms=payload.amount_ms,
        idempotency_key=payload.idempotency_key,
        reason=payload.reason,
    )
    complete(
        db,
        decision.record,
        result_type="ledger_transaction",
        result_id=transaction.id,
        response_status=201,
    )
    db.commit()
    return load_ledger_transaction(db, transaction.id, user.id)


def get_wallet(user: AppUser, db: Session) -> WalletOut:
    return WalletOut(balances=wallet_balances(db, user.id))


def list_ledger_transactions(user: AppUser, db: Session, limit: int = 50) -> LedgerTransactionList:
    transactions = list(
        db.scalars(
            select(LedgerTransaction)
            .join(LedgerPosting)
            .join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
            .where(LedgerAccount.owner_id == user.id)
            .options(selectinload(LedgerTransaction.postings))
            .distinct()
            .order_by(LedgerTransaction.created_at.desc(), LedgerTransaction.id.desc())
            .limit(limit)
        ).unique()
    )
    return LedgerTransactionList(items=transactions)
