import os
import uuid
from pathlib import Path
from typing import Annotated

os.environ["DATABASE_URL"] = f"sqlite+pysqlite:////tmp/video-maker-test-{uuid.uuid4().hex}.db"
os.environ["STORAGE_ROOT"] = "./test-storage"
os.environ["OUTBOX_POLL_INTERVAL_SECONDS"] = "0"

import pytest
from fastapi import Header
from fastapi.testclient import TestClient

from app.auth import Identity, get_identity
from app.config import get_settings
from app.db import Base, engine
from app.main import app

get_settings.cache_clear()


def test_identity(x_test_user: Annotated[str | None, Header()] = None) -> Identity:
    return Identity(subject=x_test_user or "user-a")


app.dependency_overrides[get_identity] = test_identity


@pytest.fixture(autouse=True)
def clean_database(tmp_path: Path):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    settings = get_settings()
    settings.storage_root = tmp_path / "storage"
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client
