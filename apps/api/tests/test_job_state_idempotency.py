import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    JobEvent,
    JobStatus,
    LedgerTransaction,
)
from app.provider_execution import GenerationExecutionService
from app.state_machine import transition_attempt, transition_job
from tests.test_mock_jobs import create_shot
from tests.test_quote_ledger import grant, quote


def queued_job(client: TestClient, monkeypatch) -> dict:
    async def stay_queued(self, job_id):
        return None

    monkeypatch.setattr(GenerationExecutionService, "execute", stay_queued)
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    response = client.post(
        "/v1/generations",
        headers={"Idempotency-Key": f"generation:{uuid.uuid4()}"},
        json={"shot_id": shot["id"], "quote_id": item["id"]},
    )
    assert response.status_code == 202
    return response.json()


def test_success_records_complete_job_and_attempt_state_machines(client: TestClient) -> None:
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    response = client.post(
        "/v1/generations",
        headers={"Idempotency-Key": "generation:complete-state-machine"},
        json={"shot_id": shot["id"], "quote_id": item["id"]},
    )
    assert response.status_code == 202
    job = client.get(f"/v1/generations/{response.json()['id']}").json()
    with SessionLocal() as db:
        events = list(
            db.scalars(
                select(JobEvent).where(JobEvent.job_id == uuid.UUID(job["id"]))
            )
        )

    job_transitions = {
        (event.from_status, event.to_status)
        for event in events
        if event.attempt_id is None
    }
    attempt_transitions = {
        (event.from_status, event.to_status)
        for event in events
        if event.attempt_id is not None and event.from_status != event.to_status
    }
    assert job_transitions == {
        ("CREATED", "RESERVED"),
        ("RESERVED", "QUEUED"),
        ("QUEUED", "ROUTING"),
        ("ROUTING", "SUBMITTED"),
        ("SUBMITTED", "RUNNING"),
        ("RUNNING", "POSTPROCESSING"),
        ("POSTPROCESSING", "VALIDATING"),
        ("VALIDATING", "SUCCEEDED"),
    }
    assert attempt_transitions == {
        ("CREATED", "SUBMITTING"),
        ("SUBMITTING", "SUBMITTED"),
        ("SUBMITTED", "RUNNING"),
        ("RUNNING", "SUCCEEDED"),
    }
    assert any(
        event.event_type == "provider.poll_started"
        and event.from_status == event.to_status == "RUNNING"
        for event in events
    )


def test_illegal_transitions_are_rejected_and_rollback_removes_event(
    client: TestClient, monkeypatch
) -> None:
    job = queued_job(client, monkeypatch)
    job_id = uuid.UUID(job["id"])
    with SessionLocal() as db:
        stored_job = db.get(GenerationJob, job_id)
        attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
        assert stored_job is not None and attempt is not None
        assert not transition_job(
            db, stored_job, JobStatus.SUCCEEDED, "illegal.job", f"job:{job_id}:illegal"
        )
        assert not transition_attempt(
            db,
            attempt,
            AttemptStatus.SUCCEEDED,
            "illegal.attempt",
            f"attempt:{attempt.id}:illegal",
        )
        assert transition_job(
            db, stored_job, JobStatus.ROUTING, "job.routing", f"job:{job_id}:rollback"
        )
        db.rollback()

    with SessionLocal() as db:
        stored_job = db.get(GenerationJob, job_id)
        assert stored_job is not None and stored_job.status == JobStatus.QUEUED
        assert db.scalar(
            select(func.count())
            .select_from(JobEvent)
            .where(JobEvent.dedup_key.in_([f"job:{job_id}:illegal", f"job:{job_id}:rollback"]))
        ) == 0


def test_stale_terminal_competitors_only_allow_one_winner(
    client: TestClient, monkeypatch
) -> None:
    job = queued_job(client, monkeypatch)
    job_id = uuid.UUID(job["id"])

    with SessionLocal() as db:
        attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_id))
        assert attempt is not None
        assert transition_attempt(
            db,
            attempt,
            AttemptStatus.SUBMITTING,
            "attempt.submitting",
            f"attempt:{attempt.id}:competition-setup",
        )
        attempt_id = attempt.id
        db.commit()

    attempt_winner = SessionLocal()
    attempt_competitor = SessionLocal()
    try:
        winner_attempt = attempt_winner.get(GenerationAttempt, attempt_id)
        stale_attempt = attempt_competitor.get(GenerationAttempt, attempt_id)
        assert winner_attempt is not None and stale_attempt is not None
        attempt_competitor.commit()
        assert transition_attempt(
            attempt_winner,
            winner_attempt,
            AttemptStatus.FAILED_FINAL,
            "attempt.failed",
            f"attempt:{attempt_id}:terminal-winner",
        )
        attempt_winner.commit()
        assert not transition_attempt(
            attempt_competitor,
            stale_attempt,
            AttemptStatus.CANCELLED,
            "attempt.cancelled",
            f"attempt:{attempt_id}:terminal-loser",
        )
        attempt_competitor.rollback()
    finally:
        attempt_winner.close()
        attempt_competitor.close()

    winner = SessionLocal()
    competitor = SessionLocal()
    try:
        winner_job = winner.get(GenerationJob, job_id)
        stale_job = competitor.get(GenerationJob, job_id)
        assert winner_job is not None and stale_job is not None
        competitor.commit()
        assert transition_job(
            winner,
            winner_job,
            JobStatus.FAILED_FINAL,
            "job.failed",
            f"job:{job_id}:terminal-winner",
        )
        winner.commit()
        assert not transition_job(
            competitor,
            stale_job,
            JobStatus.CANCELLED,
            "job.cancelled",
            f"job:{job_id}:terminal-loser",
        )
        competitor.rollback()
    finally:
        winner.close()
        competitor.close()

    with SessionLocal() as db:
        assert db.get(GenerationJob, job_id).status == JobStatus.FAILED_FINAL
        assert db.get(GenerationAttempt, attempt_id).status == AttemptStatus.FAILED_FINAL
        job_terminal_events = db.scalar(
            select(func.count())
            .select_from(JobEvent)
            .where(JobEvent.dedup_key.like(f"job:{job_id}:terminal-%"))
        )
        attempt_terminal_events = db.scalar(
            select(func.count())
            .select_from(JobEvent)
            .where(JobEvent.dedup_key.like(f"attempt:{attempt_id}:terminal-%"))
        )
        assert job_terminal_events == attempt_terminal_events == 1


def test_generation_idempotency_reuses_job_and_rejects_changed_body(
    client: TestClient, monkeypatch
) -> None:
    async def stay_queued(self, job_id):
        return None

    monkeypatch.setattr(GenerationExecutionService, "execute", stay_queued)
    shot = create_shot(client)
    grant(client, 2_000)
    item = quote(client, shot["id"])
    headers = {"Idempotency-Key": "generation:replay"}
    payload = {"shot_id": shot["id"], "quote_id": item["id"]}

    first = client.post("/v1/generations", headers=headers, json=payload)
    replay = client.post("/v1/generations", headers=headers, json=payload)
    conflict = client.post(
        "/v1/generations",
        headers=headers,
        json={**payload, "quote_id": str(uuid.uuid4())},
    )

    assert first.status_code == replay.status_code == 202
    assert first.json()["id"] == replay.json()["id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(LedgerTransaction)
                .where(LedgerTransaction.tx_type == "RESERVE")
            )
            == 1
        )


def test_idempotency_key_rejects_control_or_space_characters(client: TestClient) -> None:
    response = client.post(
        "/v1/quotes",
        headers={"Idempotency-Key": "invalid key"},
        json={
            "shot_id": str(uuid.uuid4()),
            "tier": "FAST",
            "resolution": "720P",
            "variant_count": 1,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_INVALID"


def test_stage_two_write_replays_do_not_repeat_grant_quote_or_cancel(
    client: TestClient, monkeypatch
) -> None:
    async def stay_queued(self, job_id):
        return None

    monkeypatch.setattr(GenerationExecutionService, "execute", stay_queued)
    shot = create_shot(client)
    grant_payload = {
        "tier": "FAST",
        "amount_ms": 2_000,
        "idempotency_key": "ledger:stage-two-replay",
        "reason": "idempotency acceptance",
    }
    grant_headers = {"Idempotency-Key": "api:grant-replay"}
    first_grant = client.post("/v1/wallet/test-grants", headers=grant_headers, json=grant_payload)
    replay_grant = client.post("/v1/wallet/test-grants", headers=grant_headers, json=grant_payload)
    assert first_grant.status_code == replay_grant.status_code == 201
    assert first_grant.json()["id"] == replay_grant.json()["id"]

    quote_payload = {
        "shot_id": shot["id"],
        "tier": "FAST",
        "resolution": "720P",
        "variant_count": 1,
    }
    quote_headers = {"Idempotency-Key": "api:quote-replay"}
    first_quote = client.post("/v1/quotes", headers=quote_headers, json=quote_payload)
    replay_quote = client.post("/v1/quotes", headers=quote_headers, json=quote_payload)
    assert first_quote.status_code == replay_quote.status_code == 201
    assert first_quote.json()["id"] == replay_quote.json()["id"]

    generation = client.post(
        "/v1/generations",
        headers={"Idempotency-Key": "api:generation-for-cancel"},
        json={
            "shot_id": shot["id"],
            "quote_id": first_quote.json()["id"],
        },
    )
    cancel_path = f"/v1/generations/{generation.json()['id']}/cancel"
    cancel_headers = {"Idempotency-Key": "api:cancel-replay"}
    first_cancel = client.post(cancel_path, headers=cancel_headers)
    replay_cancel = client.post(cancel_path, headers=cancel_headers)
    assert first_cancel.status_code == replay_cancel.status_code == 200
    assert first_cancel.json()["id"] == replay_cancel.json()["id"]
    assert replay_cancel.json()["status"] == "CANCELLED"

    with SessionLocal() as db:
        tx_counts = dict(
            db.execute(
                select(LedgerTransaction.tx_type, func.count()).group_by(
                    LedgerTransaction.tx_type
                )
            ).all()
        )
        assert tx_counts == {"GRANT": 1, "RELEASE": 1, "RESERVE": 1}
