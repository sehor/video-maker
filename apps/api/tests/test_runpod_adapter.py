import ast
import asyncio
import copy
import json
import uuid
from pathlib import Path

import httpx
import pytest

from app.provider import (
    CostSource,
    FailureCode,
    ProviderAttempt,
    ProviderStatus,
    ProviderSubmissionError,
    SubmitDisposition,
    SubmitRequest,
    WebhookVerificationError,
    WebhookVerificationRequest,
)
from app.runpod import RunPodAdapterConfig, RunPodAdapterError, RunPodVideoProvider

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "workers" / "runpod-comfyui" / "contract" / "fixtures"
ATTEMPT_ID = uuid.UUID("9d52c1db-f071-4876-86f7-3ccf597ac4db")
JOB_ID = uuid.UUID("3b59a290-7f80-4d80-a651-9786225e17d1")
WORKFLOW_SHA256 = "454f03238c881b751529524c1db47c16619cb0dce13cb7c694f55653acd35fad"
MODEL_SHA256 = "2" * 64
IMAGE_DIGEST = f"sha256:{'3' * 64}"


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def config(**overrides: object) -> RunPodAdapterConfig:
    values: dict[str, object] = {
        "endpoint_id": "endpoint-fixture",
        "api_key": "runpod-secret-fixture",
        "image_digest": IMAGE_DIGEST,
        "workflow_id": "fast_wan_i2v_720_v1",
        "workflow_sha256": WORKFLOW_SHA256,
        "model_sha256": {"fixture-model": MODEL_SHA256},
    }
    values.update(overrides)
    return RunPodAdapterConfig(**values)  # type: ignore[arg-type]


def submit_request(**overrides: object) -> SubmitRequest:
    fixture = load_fixture("request-16x9.json")
    values: dict[str, object] = {
        "attempt_id": ATTEMPT_ID,
        "idempotency_key": f"attempt:{ATTEMPT_ID}:submit:v1",
        "prompt": fixture["prompt"],
        "negative_prompt": None,
        "duration_ms": fixture["duration_ms"],
        "aspect_ratio": fixture["aspect_ratio"],
        "resolution": fixture["resolution"],
        "workflow_id": fixture["workflow_id"],
        "job_id": JOB_ID,
        "input_claim": fixture["input_claim"],
        "output_claim": fixture["output_claim"],
        "callback_claim": fixture["callback_claim"],
    }
    values.update(overrides)
    return SubmitRequest(**values)  # type: ignore[arg-type]


def attempt(provider_job_id: str | None = "runpod-job-1") -> ProviderAttempt:
    return ProviderAttempt(
        attempt_id=ATTEMPT_ID,
        idempotency_key=f"attempt:{ATTEMPT_ID}:submit:v1",
        provider_job_id=provider_job_id,
    )


def provider_with_handler(
    handler: httpx.MockTransport,
) -> tuple[RunPodVideoProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=handler,
        base_url="https://api.runpod.ai",
        headers={"authorization": "Bearer runpod-secret-fixture"},
    )
    return RunPodVideoProvider(config(), client=client), client


def test_runpod_module_has_no_orm_storage_or_user_url_dependencies() -> None:
    path = ROOT / "apps" / "api" / "app" / "runpod.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert "sqlalchemy" not in imports
    assert "app.models" not in imports
    assert "app.storage" not in imports
    assert "base_url" not in RunPodAdapterConfig.__dataclass_fields__


def test_config_requires_image_workflow_and_model_digests() -> None:
    with pytest.raises(RunPodAdapterError):
        config(image_digest="runpod/worker-comfyui:latest")
    with pytest.raises(RunPodAdapterError):
        config(model_sha256={})
    with pytest.raises(RunPodAdapterError):
        config(workflow_sha256="latest")
    with pytest.raises(RunPodAdapterError):
        config(worker_version="latest")


def test_submit_sends_only_fixed_claim_contract_and_policy() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.url == "https://api.runpod.ai/v2/endpoint-fixture/run"
        return httpx.Response(200, json={"id": "runpod-job-1", "status": "IN_QUEUE"})

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    result = asyncio.run(provider.submit(submit_request()))
    asyncio.run(client.aclose())

    assert result.disposition == SubmitDisposition.ACCEPTED
    assert result.provider_job_id == "runpod-job-1"
    assert set(captured) == {"input", "policy"}
    worker_input = captured["input"]
    assert isinstance(worker_input, dict)
    assert set(worker_input) == {
        "job_id",
        "attempt_id",
        "workflow_id",
        "prompt",
        "duration_ms",
        "aspect_ratio",
        "resolution",
        "input_claim",
        "output_claim",
        "callback_claim",
    }
    serialized = json.dumps(worker_input)
    assert not any(field in serialized for field in ("workflow_api", "model_path", "url"))


def test_submit_rejects_url_claim_without_network_call() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    with pytest.raises(ProviderSubmissionError) as captured:
        asyncio.run(
            provider.submit(submit_request(input_claim="https://example.invalid/input.png"))
        )
    asyncio.run(client.aclose())

    assert captured.value.failure.code == FailureCode.INVALID_INPUT
    assert calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("idempotency_key", "attempt:wrong:submit:v1"),
        ("resolution", "720P"),
    ],
)
def test_submit_rejects_request_outside_fixed_contract_without_network_call(
    field: str, value: str
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    with pytest.raises(ProviderSubmissionError):
        asyncio.run(provider.submit(submit_request(**{field: value})))
    asyncio.run(client.aclose())

    assert calls == 0


@pytest.mark.parametrize(
    "legacy", [{}, {"size_bytes": 0, "sha256": "ignored"}, {"size_bytes": None, "sha256": None}]
)
def test_poll_maps_success_with_provenance_timings_and_artifact(legacy) -> None:
    worker_output = load_fixture("response-success.json")
    worker_output["output"].pop("size_bytes", None)
    worker_output["output"].pop("sha256", None)
    worker_output["output"].update(legacy)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/status/runpod-job-1")
        return httpx.Response(
            200,
            json={
                "id": "runpod-job-1",
                "status": "COMPLETED",
                "delayTime": 12,
                "executionTime": 34,
                "output": worker_output,
            },
        )

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    result = asyncio.run(provider.poll(attempt()))
    asyncio.run(client.aclose())

    assert result.status == ProviderStatus.SUCCEEDED
    assert result.output is not None
    assert result.output.content is None
    assert result.output.sha256 is None
    assert result.output.object_key == f"outputs/{ATTEMPT_ID}.mp4"
    assert (result.output.duration_ms, result.output.width, result.output.height) == (
        5000,
        1280,
        720,
    )
    assert (result.output.fps, result.output.codec) == (16, "h264")
    assert result.output.metrics is not None
    assert result.output.metrics.gpu_type == "fixture-gpu"
    assert result.output.metrics.cost_source == CostSource.ESTIMATE
    assert result.output.versions is not None
    assert result.output.versions.image_digest == IMAGE_DIGEST
    assert result.output.versions.workflow_sha256 == WORKFLOW_SHA256
    assert dict(result.output.versions.model_sha256) == {"fixture-model": MODEL_SHA256}


@pytest.mark.parametrize("field", ["duration_ms", "width", "height", "fps", "codec"])
def test_poll_rejects_legacy_response_missing_media_metadata(field) -> None:
    worker_output = load_fixture("response-success.json")
    worker_output["output"].pop(field)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "runpod-job-1",
                "status": "COMPLETED",
                "output": worker_output,
            },
        )

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    result = asyncio.run(provider.poll(attempt()))
    asyncio.run(client.aclose())
    assert result.status == ProviderStatus.FAILED
    assert result.failure.code == FailureCode.POLICY_REJECTED


def test_poll_rejects_attempt_or_release_provenance_mismatch() -> None:
    for mutation in ("attempt", "digest"):
        worker_output = copy.deepcopy(load_fixture("response-success.json"))
        if mutation == "attempt":
            worker_output["attempt_id"] = str(uuid.uuid4())
        else:
            versions = worker_output["versions"]
            assert isinstance(versions, dict)
            versions["image_digest"] = f"sha256:{'4' * 64}"

        def handler(
            _request: httpx.Request,
            response_output: dict[str, object] = worker_output,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "runpod-job-1",
                    "status": "COMPLETED",
                    "output": response_output,
                },
            )

        provider, client = provider_with_handler(httpx.MockTransport(handler))
        result = asyncio.run(provider.poll(attempt()))
        asyncio.run(client.aclose())
        assert result.status == ProviderStatus.FAILED
        assert result.failure is not None
        assert result.failure.code == FailureCode.POLICY_REJECTED


@pytest.mark.parametrize("provider_status", ["FAILED", "COMPLETED"])
def test_poll_maps_structured_worker_failure(provider_status: str) -> None:
    worker_output = load_fixture("response-failure.json")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "runpod-job-1",
                "status": provider_status,
                "output": worker_output,
            },
        )

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    result = asyncio.run(provider.poll(attempt()))
    asyncio.run(client.aclose())

    assert result.status == ProviderStatus.FAILED
    assert result.failure is not None
    assert result.failure.code == FailureCode.OUT_OF_MEMORY
    assert result.failure.message == "Worker exhausted GPU memory"
    assert result.metrics is not None
    assert result.metrics.cost_minor == 1
    assert result.versions is not None
    assert result.versions.image_digest == IMAGE_DIGEST
    assert result.cost is not None
    assert result.cost.source == CostSource.ESTIMATE


def test_poll_maps_invalid_worker_error_without_escaping_adapter() -> None:
    worker_output = copy.deepcopy(load_fixture("response-failure.json"))
    error = worker_output["error"]
    assert isinstance(error, dict)
    error["code"] = "UNRECOGNIZED_WORKER_ERROR"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "runpod-job-1",
                "status": "COMPLETED",
                "output": worker_output,
            },
        )

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    result = asyncio.run(provider.poll(attempt()))
    asyncio.run(client.aclose())

    assert result.status == ProviderStatus.FAILED
    assert result.failure is not None
    assert result.failure.code == FailureCode.WORKFLOW_FAILED


def test_poll_maps_provider_http_errors_without_leaking_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="secret provider diagnostic")

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    result = asyncio.run(provider.poll(attempt()))
    asyncio.run(client.aclose())

    assert result.failure is not None
    assert result.failure.code == FailureCode.PROVIDER_AUTHENTICATION
    assert "secret provider diagnostic" not in result.failure.message


def test_poll_rejects_mismatched_provider_job_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "other-job", "status": "IN_QUEUE"})

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    result = asyncio.run(provider.poll(attempt()))
    asyncio.run(client.aclose())

    assert result.status == ProviderStatus.FAILED
    assert result.failure is not None
    assert result.failure.code == FailureCode.INTERNAL_ERROR


def test_cancel_is_best_effort_and_webhook_is_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/cancel/runpod-job-1")
        return httpx.Response(200, json={"id": "runpod-job-1", "status": "CANCELLED"})

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    cancelled = asyncio.run(provider.cancel(attempt()))
    with pytest.raises(WebhookVerificationError):
        asyncio.run(provider.verify_webhook(WebhookVerificationRequest({}, b"{}")))
    asyncio.run(client.aclose())

    assert cancelled.accepted
    assert cancelled.status == ProviderStatus.CANCELLED


def test_cancel_rejects_mismatched_provider_job_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "other-job", "status": "CANCELLED"})

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    cancelled = asyncio.run(provider.cancel(attempt()))
    asyncio.run(client.aclose())

    assert not cancelled.accepted
    assert cancelled.status == ProviderStatus.UNKNOWN


def test_submit_disconnect_is_unknown_and_never_retried_by_adapter() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("disconnected", request=request)

    provider, client = provider_with_handler(httpx.MockTransport(handler))
    result = asyncio.run(provider.submit(submit_request()))
    asyncio.run(client.aclose())

    assert result.disposition == SubmitDisposition.UNKNOWN
    assert result.provider_job_id is None
    assert calls == 1
