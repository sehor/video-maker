import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal
from app.models import GenerationJob, Project, RouteAdmission
from app.project_routing import project_route
from app.routing import MOCK_ROUTE, SIMULATED_RUNPOD_ROUTE_VERSION, get_route_registry
from tests.test_accepted_input import executor, submit
from tests.test_provider_routing import _quoted_shot

pytestmark = pytest.mark.database


def quote_for(client, shot):
    response = client.post(
        "/v1/quotes", json={"shot_id": shot["id"], "tier": "FAST", "resolution": "720P"}
    )
    assert response.status_code == 201
    return response.json()


def test_project_route_survives_default_change_batch_stop_and_success(raw_client):
    routes = get_route_registry()
    previous = routes.active_key
    routes.activate(MOCK_ROUTE.key)
    try:
        shot, quote, _ = _quoted_shot(raw_client, with_reference=True)
        first = submit(raw_client, shot, quote)
        routes.activate(SIMULATED_RUNPOD_ROUTE_VERSION)
        batch = raw_client.post(
            "/v1/batches", json={"items": [{"quote_id": quote_for(raw_client, shot)["id"]}]}
        )
        assert batch.status_code == 202, batch.text
        with SessionLocal() as db:
            project = db.get(Project, uuid.UUID(shot["project_id"]))
            assert project.route_binding_status == "PROVISIONAL"
            assert {j.selected_route_candidate_id for j in db.scalars(select(GenerationJob))} == {
                MOCK_ROUTE.candidate_id
            }
            db.get(RouteAdmission, MOCK_ROUTE.candidate_id).enabled = False
            db.commit()
        rejected = raw_client.post(
            "/v1/generations",
            json={"shot_id": shot["id"], "quote_id": quote_for(raw_client, shot)["id"]},
        )
        assert rejected.status_code == 503
        # Existing accepted jobs can finish after a stop and lock the original route.
        asyncio.run(executor().execute(first))
        with SessionLocal() as db:
            project = db.get(Project, uuid.UUID(shot["project_id"]))
            assert project.route_binding_status == "LOCKED"
            assert project.route_candidate_id == MOCK_ROUTE.candidate_id
            assert project.route_bound_at is not None
            assert db.scalar(select(func.count(GenerationJob.id))) == 2
    finally:
        routes.activate(previous)


def test_two_first_jobs_share_one_binding(raw_client):
    shot, first_quote, _ = _quoted_shot(raw_client, with_reference=False)
    second_quote = quote_for(raw_client, shot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, raw_client, shot, q) for q in [first_quote, second_quote]]
        ids = [future.result(timeout=20) for future in futures]
    with SessionLocal() as db:
        jobs = db.scalars(select(GenerationJob).where(GenerationJob.id.in_(ids))).all()
        project = db.get(Project, uuid.UUID(shot["project_id"]))
        assert {job.selected_route_candidate_id for job in jobs} == {project.route_candidate_id}
        assert project.route_binding_status == "PROVISIONAL"


def test_route_stop_serializes_with_acceptance(raw_client):
    shot, _quote, _ = _quoted_shot(raw_client, with_reference=False)
    with SessionLocal() as accepting:
        route = project_route(accepting, uuid.UUID(shot["project_id"]))
        with SessionLocal() as stopping:
            stopping.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(DBAPIError) as raised:
                stopping.execute(
                    text("UPDATE route_admission SET enabled=false WHERE candidate_id=:id"),
                    {"id": route.candidate_id},
                )
            assert raised.value.orig.sqlstate == "55P03"
            stopping.rollback()
        accepting.commit()
    with SessionLocal() as stopping:
        stopping.get(RouteAdmission, route.candidate_id).enabled = False
        stopping.commit()
    with SessionLocal() as accepting:
        with pytest.raises(Exception, match="暂停接单"):
            project_route(accepting, uuid.UUID(shot["project_id"]))
