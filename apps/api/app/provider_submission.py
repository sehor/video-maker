from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    AttemptStatus,
    GenerationAttempt,
    GenerationJob,
    JobEvent,
    JobStatus,
)
from app.provider import SubmitRequest
from app.provider_execution_context import AttemptContext
from app.routing import MAX_PROVIDER_OUTPUT_BYTES, CallbackClaimIssuer, RouteRegistry
from app.state_machine import transition_attempt, transition_job
from app.storage import ObjectStorage


class ProviderSubmissionService:
    """Builds claim-only requests and persists the idempotent submit handshake."""

    _storage: ObjectStorage
    _routes: RouteRegistry
    _callback_claims: CallbackClaimIssuer
    _claim_ttl: timedelta
    _session_factory: sessionmaker[Session]

    def _submit_request(self, context: AttemptContext) -> SubmitRequest:
        if context.route_candidate_id is None:
            raise RuntimeError("generation attempt has no immutable route candidate")
        route = self._routes.by_candidate_id(context.route_candidate_id)
        if (
            route.provider_code != context.provider_code
            or route.workflow_id != context.workflow_version
        ):
            raise RuntimeError("generation attempt does not match its route version")
        input_claim = (
            self._storage.read_claim(
                context.reference_object_key,
                expires_in=self._claim_ttl,
            ).token
            if context.reference_object_key is not None
            else None
        )
        if route.requires_input_claim and input_claim is None:
            raise RuntimeError("route requires a reference asset claim")
        output_claim = self._storage.write_claim(
            f"provider-outputs/{context.job_id}/{context.attempt_id}",
            mime_type="video/mp4",
            max_bytes=MAX_PROVIDER_OUTPUT_BYTES,
            expires_in=self._claim_ttl,
        ).token
        callback_claim = self._callback_claims.issue(
            job_id=context.job_id,
            attempt_id=context.attempt_id,
            route=route,
            expires_in=self._claim_ttl,
        )
        return SubmitRequest(
            job_id=context.job_id,
            attempt_id=context.attempt_id,
            idempotency_key=context.idempotency_key,
            prompt=context.prompt,
            negative_prompt=context.negative_prompt,
            duration_ms=context.duration_ms,
            aspect_ratio=context.aspect_ratio,
            resolution=context.resolution.lower(),
            workflow_id=context.workflow_version,
            input_claim=input_claim,
            output_claim=output_claim,
            callback_claim=callback_claim,
            mode=context.mode,
        )

    def _start_submit(self, context: AttemptContext) -> bool:
        with self._session_factory() as db:
            job = db.get(GenerationJob, context.job_id)
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if job is None or attempt is None or attempt.status != AttemptStatus.CREATED:
                return False
            if job.status == JobStatus.QUEUED and not transition_job(
                db, job, JobStatus.ROUTING, "job.routing", f"job:{job.id}:routing:v1"
            ):
                return False
            if not transition_attempt(
                db,
                attempt,
                AttemptStatus.SUBMITTING,
                "attempt.submitting",
                f"attempt:{attempt.id}:submitting:v1",
            ):
                return False
            db.commit()
            return True

    def _record_submit_accepted(
        self, context: AttemptContext, provider_job_id: str, *, reconciled: bool = False
    ) -> None:
        with self._session_factory() as db:
            job = db.get(GenerationJob, context.job_id)
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if job is None or attempt is None:
                return
            if attempt.provider_job_id not in {None, provider_job_id}:
                return
            attempt.provider_job_id = provider_job_id
            if job.status == JobStatus.ROUTING:
                transition_job(
                    db,
                    job,
                    JobStatus.SUBMITTED,
                    "provider.reconciled" if reconciled else "provider.submitted",
                    f"attempt:{attempt.id}:job-submitted:v1",
                    {"provider_job_id": provider_job_id},
                )
            if attempt.status == AttemptStatus.SUBMITTING:
                transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.SUBMITTED,
                    "attempt.reconciled" if reconciled else "attempt.submitted",
                    f"attempt:{attempt.id}:submitted:v1",
                    {"provider_job_id": provider_job_id},
                )
            if job.status == JobStatus.SUBMITTED:
                transition_job(
                    db,
                    job,
                    JobStatus.RUNNING,
                    "provider.started",
                    f"attempt:{attempt.id}:job-running:v1",
                )
                job.started_at = job.started_at or datetime.now(UTC)
            if attempt.status == AttemptStatus.SUBMITTED:
                transition_attempt(
                    db,
                    attempt,
                    AttemptStatus.RUNNING,
                    "attempt.running",
                    f"attempt:{attempt.id}:running:v1",
                )
                attempt.started_at = attempt.started_at or datetime.now(UTC)
            db.commit()

    def _record_submit_unknown(self, context: AttemptContext) -> None:
        with self._session_factory() as db:
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if attempt is None or attempt.status != AttemptStatus.SUBMITTING:
                return
            self._add_same_state_event(
                db,
                attempt,
                "provider.submit_unknown",
                f"attempt:{attempt.id}:submit-unknown:v1",
            )
            db.commit()

    def _record_reconcile_pending(self, context: AttemptContext) -> None:
        with self._session_factory() as db:
            attempt = db.get(GenerationAttempt, context.attempt_id)
            if attempt is None:
                return
            self._add_same_state_event(
                db,
                attempt,
                "provider.reconcile_pending",
                f"attempt:{attempt.id}:reconcile-pending:v1",
            )
            db.commit()

    @staticmethod
    def _add_same_state_event(
        db: Session,
        attempt: GenerationAttempt,
        event_type: str,
        dedup_key: str,
    ) -> None:
        exists = db.scalar(select(JobEvent.id).where(JobEvent.dedup_key == dedup_key))
        if exists is None:
            db.add(
                JobEvent(
                    job_id=attempt.job_id,
                    attempt_id=attempt.id,
                    event_type=event_type,
                    from_status=attempt.status.value,
                    to_status=attempt.status.value,
                    dedup_key=dedup_key,
                    payload_json={},
                )
            )
