from typing import Annotated

from fastapi import APIRouter, Query

from app import application_billing as use_cases
from app.auth import CurrentUser
from app.http_dependencies import Db, IdempotencyKey
from app.models import (
    LedgerTransaction,
)
from app.schemas import (
    LedgerTransactionList,
    LedgerTransactionOut,
    QuoteCreate,
    QuoteOut,
    TestGrantCreate,
    WalletOut,
)

router = APIRouter(prefix="/v1")


@router.post("/quotes", response_model=QuoteOut, status_code=201)
def create_generation_quote(
    payload: QuoteCreate, user: CurrentUser, db: Db, idempotency_key: IdempotencyKey = None
):
    return use_cases.create_generation_quote(payload, user, db, idempotency_key)


@router.post(
    "/wallet/test-grants",
    response_model=LedgerTransactionOut,
    status_code=201,
    include_in_schema=False,
)
def create_test_grant(
    payload: TestGrantCreate, user: CurrentUser, db: Db, idempotency_key: IdempotencyKey = None
) -> LedgerTransaction:
    return use_cases.create_test_grant(payload, user, db, idempotency_key)


@router.get("/wallet", response_model=WalletOut)
def get_wallet(user: CurrentUser, db: Db) -> WalletOut:
    return use_cases.get_wallet(user, db)


@router.get("/ledger", response_model=LedgerTransactionList)
def list_ledger_transactions(
    user: CurrentUser, db: Db, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> LedgerTransactionList:
    return use_cases.list_ledger_transactions(user, db, limit)
