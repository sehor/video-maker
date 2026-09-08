"""Verify test isolation against PostgreSQL without changing business assertions."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from test_support import reset_committed_data, rollback_sessions

from app.db import Base, SessionLocal, engine
from app.models import AppUser, LedgerPosting
from tests.test_quote_ledger import grant

pytestmark = pytest.mark.database


def test_rollback_sessions_preserve_local_commits_but_leave_no_rows():
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    original = factory.kw.copy()
    for _ in range(2):
        with rollback_sessions(factory, engine):
            with factory() as db:
                db.add(AppUser(auth_subject="rollback-isolation"))
                db.commit()
            with factory() as db:
                assert db.scalar(select(AppUser).where(
                    AppUser.auth_subject == "rollback-isolation"
                )) is not None
            with SessionLocal() as observer:
                assert observer.scalar(select(AppUser).where(
                    AppUser.auth_subject == "rollback-isolation"
                )) is None
        assert factory.kw == original
    with SessionLocal() as observer:
        assert observer.scalar(select(AppUser).where(
            AppUser.auth_subject == "rollback-isolation"
        )) is None


def test_rollback_sessions_restore_factory_after_failure():
    factory = sessionmaker(bind=engine)
    original = factory.kw.copy()
    with pytest.raises(RuntimeError, match="injected"):
        with rollback_sessions(factory, engine):
            with factory() as db:
                db.add(AppUser(auth_subject="failed-isolation"))
                db.commit()
            raise RuntimeError("injected")
    assert factory.kw == original
    with SessionLocal() as observer:
        assert observer.scalar(select(AppUser).where(
            AppUser.auth_subject == "failed-isolation"
        )) is None


def test_default_isolation_keeps_real_commit_visibility():
    with SessionLocal() as db:
        db.add(AppUser(auth_subject="real-commit"))
        db.commit()
        # Keep the writer's connection checked out to require a different backend.
        writer_pid = db.scalar(text("SELECT pg_backend_pid()"))
        with SessionLocal() as observer:
            assert observer.scalar(text("SELECT pg_backend_pid()")) != writer_pid
            assert observer.scalar(select(AppUser).where(
                AppUser.auth_subject == "real-commit"
            )) is not None


def test_plain_row_cleanup_keeps_table_files_and_route_seed():
    with engine.begin() as connection:
        before = connection.scalar(text("SELECT pg_relation_filenode('app_users')"))
        connection.execute(AppUser.__table__.insert().values(auth_subject="row-cleanup"))
        assert reset_committed_data(connection, Base.metadata) == []
        assert connection.scalar(text("SELECT pg_relation_filenode('app_users')")) == before
        assert connection.scalar(select(func.count()).select_from(AppUser)) == 0
        assert connection.scalar(text("SELECT count(*) FROM route_admission")) == (
            connection.scalar(text("SELECT count(*) FROM generation_route_versions"))
        )


def test_ledger_cleanup_preserves_immutable_trigger(raw_client):
    grant(raw_client, 1000)
    with engine.begin() as connection:
        assert reset_committed_data(connection, Base.metadata) == ["ledger_postings"]
        assert connection.scalar(select(func.count()).select_from(LedgerPosting)) == 0
    grant(raw_client, 1000)
    with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="immutable"):
            with connection.begin_nested():
                connection.execute(text("DELETE FROM ledger_postings"))
        assert connection.scalar(select(func.count()).select_from(LedgerPosting)) == 2
