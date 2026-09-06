"""Exercise migrated PostgreSQL protections through SQL, bypassing application guards."""

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_support import isolated_schema
from test_support import test_database_url as configured_test_url

from app.db import SessionLocal, engine
from app.models import LedgerPosting, LedgerTransaction
from tests.test_mock_jobs import create_shot
from tests.test_quote_ledger import grant, quote

pytestmark = pytest.mark.database


def test_failed_isolated_run_cleans_only_its_own_schema():
    schema = None
    with engine.connect() as connection:
        current = connection.scalar(text("SELECT current_schema()"))
    with pytest.raises(RuntimeError, match="injected"):
        with isolated_schema(configured_test_url()) as url:
            schema = url.query["options"].split()[0].split("=", 1)[1]
            raise RuntimeError("injected failure")
    with engine.connect() as connection:
        assert connection.scalar(text(
            "SELECT count(*) FROM pg_namespace WHERE nspname = :schema"
        ), {"schema": schema}) == 0
        assert connection.scalar(text("SELECT current_schema()")) == current
        assert connection.scalar(text("SELECT count(*) FROM alembic_version")) == 1


@pytest.mark.parametrize("statement", [
    "UPDATE ledger_postings SET amount_ms = amount_ms + 1",
    "DELETE FROM ledger_postings",
])
def test_committed_postings_cannot_be_changed_even_via_sql(raw_client, statement):
    grant(raw_client, 1_000)
    with engine.connect() as connection:
        before = connection.execute(text(
            "SELECT id, amount_ms FROM ledger_postings ORDER BY id"
        )).all()
    assert len(before) == 2
    with (
        pytest.raises(DBAPIError, match="ledger postings are immutable"),
        engine.begin() as connection,
    ):
        connection.execute(text(statement))
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT id, amount_ms FROM ledger_postings ORDER BY id"
        )).all() == before


def test_unbalanced_transaction_is_rejected_at_commit(raw_client):
    grant(raw_client, 1_000)
    transaction_id = uuid.uuid4()
    with SessionLocal() as db:
        existing = db.scalar(select(LedgerPosting))
        db.add(LedgerTransaction(
            id=transaction_id, tx_type="TEST", idempotency_key=f"unbalanced:{transaction_id}",
            reference_type="test", reference_id=str(transaction_id), unit=existing.unit,
        ))
        db.flush()
        db.add(LedgerPosting(transaction_id=transaction_id, account_id=existing.account_id,
                             unit=existing.unit, amount_ms=1))
        db.flush()  # The constraint is deferred: only COMMIT must reject it.
        with pytest.raises(DBAPIError, match="is not balanced"):
            db.commit()
        db.rollback()
    with SessionLocal() as db:
        assert db.get(LedgerTransaction, transaction_id) is None
        assert db.scalar(select(func.sum(LedgerPosting.amount_ms))) == 0


def test_quote_terms_and_terminal_status_are_enforced_by_database(raw_client):
    shot = create_shot(raw_client)
    quoted = quote(raw_client, shot["id"])
    parameters = {"id": uuid.UUID(quoted["id"])}
    with pytest.raises(DBAPIError, match="terms are immutable"), engine.begin() as connection:
        connection.execute(text(
            "UPDATE generation_quotes SET reserved_ms = reserved_ms + 1 WHERE id = :id"
        ), parameters)
    with engine.begin() as connection:
        connection.execute(text("UPDATE generation_quotes SET status = 'EXPIRED' WHERE id = :id"),
                           parameters)
    with pytest.raises(DBAPIError, match="cannot move backwards"), engine.begin() as connection:
        connection.execute(text("UPDATE generation_quotes SET status = 'OPEN' WHERE id = :id"),
                           parameters)
