import asyncio
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest

from app.errors import ApiError
from app.provider import (
    CostSource,
    FailureCode,
    ProviderAttempt,
    ProviderStatus,
    SubmitDisposition,
    SubmitRequest,
    WebhookVerificationError,
    WebhookVerificationRequest,
)
from app.simulators import (
    DeterministicRunPodSimulator,
    FakeRemoteStorage,
    FaultInjector,
    FaultPoint,
    ManualClock,
    put_fake_object,
)

CLAIM_SECRET = b"deterministic-fake-r2-claim-secret-32-bytes"
ATTEMPT_ID = uuid.UUID("791d3a69-93bb-4703-b7ee-2b4cc9808f72")
JOB_ID = uuid.UUID("3b59a290-7f80-4d80-a651-9786225e17d1")
PNG = b"\x89PNG\r\n\x1a\nsimulated-r2-object"


def submit_request(*, mode: str = "success") -> SubmitRequest:
    return SubmitRequest(
        job_id=JOB_ID,
        attempt_id=ATTEMPT_ID,
        idempotency_key=f"attempt:{ATTEMPT_ID}:submit:v1",
        prompt="deterministic offline fixture",
        negative_prompt=None,
        duration_ms=5_000,
        aspect_ratio="16:9",
        resolution="720p",
        workflow_id="fast_wan_i2v_720_v1",
        input_claim="c3lzdGVtLWlucHV0.c2lnbmF0dXJl",
        output_claim="c3lzdGVtLW91dHB1dA.c2lnbmF0dXJl",
        callback_claim="c3lzdGVtLWNhbGxiYWNr.c2lnbmF0dXJl",
        mode=mode,
    )


def provider_attempt(provider_job_id: str | None) -> ProviderAttempt:
    return ProviderAttempt(
        attempt_id=ATTEMPT_ID,
        idempotency_key=f"attempt:{ATTEMPT_ID}:submit:v1",
        provider_job_id=provider_job_id,
    )


def poll(simulator: DeterministicRunPodSimulator, attempt: ProviderAttempt):
    return asyncio.run(simulator.poll(attempt))


def assert_api_error(code: str, operation) -> None:
    with pytest.raises(ApiError) as raised:
        operation()
    assert raised.value.code == code


def test_stateful_runpod_success_has_stable_queue_run_and_provenance() -> None:
    clock = ManualClock()
    simulator = DeterministicRunPodSimulator(clock=clock)
    submitted = asyncio.run(simulator.submit(submit_request()))
    attempt = provider_attempt(submitted.provider_job_id)

    assert submitted.disposition == SubmitDisposition.ACCEPTED
    assert poll(simulator, attempt).status == ProviderStatus.PENDING

    clock.advance(timedelta(seconds=2))
    assert poll(simulator, attempt).status == ProviderStatus.RUNNING

    clock.advance(timedelta(seconds=3))
    completed = poll(simulator, attempt)
    snapshot = simulator.job(submitted.provider_job_id)
    assert completed.status == ProviderStatus.SUCCEEDED
    assert completed.output is not None
    assert (completed.output.duration_ms, completed.output.width, completed.output.height) == (
        5_000,
        1280,
        720,
    )
    assert snapshot.started_at == snapshot.submitted_at + timedelta(seconds=2)
    assert snapshot.finished_at == snapshot.submitted_at + timedelta(seconds=5)
    assert snapshot.provenance.gpu_type == "NVIDIA L40S (SIMULATED)"
    assert snapshot.provenance.image_digest.startswith("sha256:")
    assert snapshot.provenance.worker_version == "worker-comfyui-simulator/1.0.0"
    assert snapshot.provenance.comfyui_version == "v0.29.0"
    assert snapshot.provenance.workflow_sha256 == (
        "454f03238c881b751529524c1db47c16619cb0dce13cb7c694f55653acd35fad"
    )
    assert sorted(snapshot.provenance.model_sha256) == [
        "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "wan2.1_i2v_14B_fp8_e4m3fn.safetensors",
        "wan_2.1_vae.safetensors",
    ]
    assert snapshot.metrics.cost_minor == 7
    assert snapshot.metrics.cost_source == CostSource.ESTIMATED
    assert poll(simulator, attempt) == completed


@pytest.mark.parametrize(
    ("mode", "advance_by", "failure_code"),
    [
        ("failure", timedelta(seconds=5), FailureCode.WORKFLOW_FAILED),
        ("timeout", timedelta(seconds=4), FailureCode.QUEUE_TIMEOUT),
    ],
)
def test_failure_and_timeout_are_terminal_and_repeatable(
    mode: str,
    advance_by: timedelta,
    failure_code: FailureCode,
) -> None:
    clock = ManualClock()
    simulator = DeterministicRunPodSimulator(clock=clock)
    submitted = asyncio.run(simulator.submit(submit_request(mode=mode)))
    attempt = provider_attempt(submitted.provider_job_id)

    clock.advance(advance_by)
    first = poll(simulator, attempt)
    second = poll(simulator, attempt)

    assert first == second
    assert first.status == ProviderStatus.FAILED
    assert first.failure is not None
    assert first.failure.code == failure_code


def test_cancel_is_stateful_best_effort_and_idempotent_submit_is_stable() -> None:
    clock = ManualClock()
    simulator = DeterministicRunPodSimulator(clock=clock)
    request = submit_request()
    first = asyncio.run(simulator.submit(request))
    duplicate = asyncio.run(simulator.submit(request))
    attempt = provider_attempt(first.provider_job_id)

    assert first == duplicate
    cancelled = asyncio.run(simulator.cancel(attempt))
    cancelled_again = asyncio.run(simulator.cancel(attempt))
    assert cancelled.accepted is True
    assert cancelled.status == ProviderStatus.CANCELLED
    assert cancelled_again.accepted is False
    assert poll(simulator, attempt).status == ProviderStatus.CANCELLED

    clock.advance(timedelta(days=1))
    assert poll(simulator, attempt).status == ProviderStatus.CANCELLED


def test_fault_injection_is_fifo_fail_once_and_does_not_create_hidden_state() -> None:
    clock = ManualClock()
    faults = FaultInjector()
    simulator = DeterministicRunPodSimulator(clock=clock, faults=faults)
    faults.fail_next(FaultPoint.RUNPOD_SUBMIT, TimeoutError("injected submit timeout"))

    with pytest.raises(TimeoutError, match="injected submit timeout"):
        asyncio.run(simulator.submit(submit_request()))
    assert simulator.jobs() == ()
    assert faults.pending(FaultPoint.RUNPOD_SUBMIT) == 0

    submitted = asyncio.run(simulator.submit(submit_request()))
    attempt = provider_attempt(submitted.provider_job_id)
    faults.fail_next(FaultPoint.RUNPOD_POLL, RuntimeError("injected poll failure"))
    with pytest.raises(RuntimeError, match="injected poll failure"):
        poll(simulator, attempt)
    assert poll(simulator, attempt).status == ProviderStatus.PENDING


def test_cost_is_fixed_and_unknown_attempt_has_no_cost() -> None:
    simulator = DeterministicRunPodSimulator(clock=ManualClock())
    submitted = asyncio.run(simulator.submit(submit_request()))
    cost = asyncio.run(simulator.read_cost(provider_attempt(submitted.provider_job_id)))
    unknown = asyncio.run(simulator.read_cost(provider_attempt("runpod-sim-unknown")))

    assert cost is not None
    assert (cost.amount_minor, cost.currency, cost.source) == (7, "USD", CostSource.ESTIMATED)
    assert unknown is None


def test_webhook_is_explicitly_disabled_for_the_offline_simulator() -> None:
    simulator = DeterministicRunPodSimulator(clock=ManualClock())
    with pytest.raises(WebhookVerificationError, match="disabled"):
        asyncio.run(
            simulator.verify_webhook(WebhookVerificationRequest(headers={}, body=b"{}"))
        )


@pytest.mark.parametrize(
    "invalid_request",
    [
        replace(submit_request(), workflow_id="user-workflow"),
        replace(submit_request(), input_claim="https://attacker.invalid/input.png"),
        replace(submit_request(), output_claim="https://attacker.invalid/output.mp4"),
        replace(submit_request(), callback_claim="https://attacker.invalid/callback"),
    ],
)
def test_simulator_rejects_uncontrolled_workflow_and_urls(
    invalid_request: SubmitRequest,
) -> None:
    simulator = DeterministicRunPodSimulator(clock=ManualClock())
    with pytest.raises(ValueError):
        asyncio.run(simulator.submit(invalid_request))


def test_fake_remote_storage_has_signed_claims_and_stable_object_metadata() -> None:
    clock = ManualClock()
    storage = FakeRemoteStorage(CLAIM_SECRET, clock=clock)
    write_claim = storage.write_claim("inputs", mime_type="image/png", max_bytes=len(PNG))

    assert write_claim.object_key == "inputs/00000000000000000000000000000001.png"
    assert write_claim.token.count(".") == 1
    assert storage.resolve_claim(write_claim.token) == write_claim

    stored = storage.put(write_claim, PNG, "image/png")
    metadata = storage.metadata(stored.key)
    assert (metadata.key, metadata.mime_type, metadata.size_bytes, metadata.sha256) == (
        stored.key,
        "image/png",
        stored.size_bytes,
        stored.sha256,
    )
    assert metadata.etag == f'"{stored.sha256}"'
    assert metadata.created_at == clock()
    assert storage.keys() == (stored.key,)

    read_claim = storage.read_claim(stored.key)
    with storage.open(storage.resolve_claim(read_claim.token)) as source:
        assert source.read() == PNG
    assert_api_error(
        "STORAGE_CLAIM_INVALID",
        lambda: storage.open(replace(read_claim, token=read_claim.token + "x")),
    )


def test_fake_remote_storage_clock_expiry_and_faults_are_controllable() -> None:
    clock = ManualClock()
    faults = FaultInjector()
    storage = FakeRemoteStorage(CLAIM_SECRET, clock=clock, faults=faults)
    stored, metadata = put_fake_object(
        storage,
        namespace="outputs",
        content=PNG,
        mime_type="image/png",
    )
    claim = storage.read_claim(stored.key, expires_in=timedelta(seconds=5))

    faults.fail_next(FaultPoint.STORAGE_OPEN, OSError("injected remote read fault"))
    with pytest.raises(OSError, match="injected remote read fault"):
        storage.open(claim)
    with storage.open(claim) as source:
        assert source.read() == PNG

    clock.advance(timedelta(seconds=5))
    assert_api_error("STORAGE_CLAIM_EXPIRED", lambda: storage.open(claim))
    assert storage.metadata(stored.key) == metadata


def test_simulator_ids_claims_and_hashes_repeat_across_fresh_instances() -> None:
    first_clock = ManualClock()
    second_clock = ManualClock()
    first = DeterministicRunPodSimulator(clock=first_clock)
    second = DeterministicRunPodSimulator(clock=second_clock)
    first_submit = asyncio.run(first.submit(submit_request()))
    second_submit = asyncio.run(second.submit(submit_request()))

    assert first_submit == second_submit
    assert first.job(first_submit.provider_job_id) == second.job(second_submit.provider_job_id)

    first_storage = FakeRemoteStorage(CLAIM_SECRET, clock=first_clock)
    second_storage = FakeRemoteStorage(CLAIM_SECRET, clock=second_clock)
    first_claim = first_storage.write_claim("outputs", mime_type="video/mp4", max_bytes=128)
    second_claim = second_storage.write_claim("outputs", mime_type="video/mp4", max_bytes=128)
    assert first_claim == second_claim
