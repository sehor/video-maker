from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "run_worker_comfyui_poc.py"
SPEC = importlib.util.spec_from_file_location("run_worker_poc", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
run_worker_poc = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = run_worker_poc
SPEC.loader.exec_module(run_worker_poc)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeProvider:
    def __init__(
        self, *, never_complete: bool = False, cancel_fails: bool = False
    ) -> None:
        self.never_complete = never_complete
        self.cancel_fails = cancel_fails
        self.requests: list[tuple[dict[str, Any], int]] = []
        self.polls: dict[str, int] = {}
        self.cancelled: list[str] = []

    def submit(self, request: dict[str, Any], execution_timeout_ms: int) -> str:
        self.requests.append((request, execution_timeout_ms))
        job_id = f"job-{len(self.requests)}"
        self.polls[job_id] = 0
        return job_id

    def status(self, job_id: str) -> dict[str, Any]:
        self.polls[job_id] += 1
        if self.never_complete or self.polls[job_id] == 1:
            return {"status": "IN_PROGRESS"}
        request = self.requests[int(job_id.split("-")[1]) - 1][0]
        response_path = (
            ROOT
            / "workers"
            / "runpod-comfyui"
            / "contract"
            / "fixtures"
            / "response-success.json"
        )
        response = json.loads(response_path.read_text(encoding="utf-8"))
        response["attempt_id"] = request["attempt_id"]
        return {
            "status": "COMPLETED",
            "executionTime": 1_000,
            "delayTime": 100,
            "output": response,
        }

    def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)
        if self.cancel_fails:
            raise run_worker_poc.ProviderError("synthetic cancellation failure")


def request_fixture(name: str) -> dict[str, Any]:
    path = ROOT / "workers" / "runpod-comfyui" / "contract" / "fixtures" / name
    return json.loads(path.read_text(encoding="utf-8"))


class RunWorkerPocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media_dir = self.root / "media"
        self.media_dir.mkdir()
        request_paths: list[Path] = []
        for name in ("request-16x9.json", "request-9x16.json"):
            request = request_fixture(name)
            path = self.root / name
            path.write_text(json.dumps(request), encoding="utf-8")
            request_paths.append(path)
            (self.media_dir / f"{request['attempt_id']}.mp4").write_bytes(b"synthetic")
        self.config = run_worker_poc.PocConfig(
            api_key="test-secret-never-record",
            endpoint_id="endpoint-test",
            registry_image="registry.invalid/video-factory/worker:poc-5.8.7",
            image_digest="sha256:" + "a" * 64,
            cost_limit_usd=Decimal("1"),
            cost_per_second_usd=Decimal("0.01"),
            cancellation_reserve_seconds=5,
            poll_seconds=10,
            request_paths=(request_paths[0], request_paths[1]),
            media_dir=self.media_dir,
            model_hashes={"model.safetensors": "b" * 64},
        )

    def test_environment_presence_reports_booleans_without_values(self) -> None:
        result = run_worker_poc.environment_presence(
            {
                "RUNPOD_API_KEY": "do-not-print",
                "RUNPOD_ENDPOINT_ID": "endpoint",
                "POC_COST_LIMIT_USD": "5",
                "POC_COST_PER_SECOND_USD": "0.01",
                "POC_REGISTRY_IMAGE": "registry.invalid/project/image:poc",
                "POC_REGISTRY_USERNAME": "user",
                "POC_REGISTRY_TOKEN": "do-not-print",
                "POC_IMAGE_DIGEST": "sha256:digest",
                "POC_REQUEST_16X9": "request-landscape.json",
                "POC_REQUEST_9X16": "request-portrait.json",
                "POC_MEDIA_DIR": "media",
                "POC_MODEL_SHA256_JSON": "models.json",
            }
        )
        self.assertEqual(
            result,
            {
                "runpod_api_key": True,
                "runpod_endpoint_id": True,
                "cost_limit": True,
                "cost_rate": True,
                "registry_image": True,
                "registry_auth": True,
                "image_digest": True,
                "request_16x9": True,
                "request_9x16": True,
                "media_dir": True,
                "model_hashes": True,
            },
        )
        self.assertNotIn("do-not-print", json.dumps(result))

    def test_paid_poc_runs_serially_and_records_no_claims_or_secrets(self) -> None:
        evidence_path = self.root / "evidence.json"
        provider = FakeProvider()
        clock = FakeClock()
        evidence = run_worker_poc.run_paid_poc(
            self.config,
            evidence_path,
            client=provider,
            clock=clock,
            sleep=clock.sleep,
            media_validator=lambda _path, aspect: {
                "validated": True,
                "aspect_ratio": aspect,
            },
        )
        self.assertEqual(evidence["status"], "SUCCEEDED")
        self.assertEqual(
            [run["status"] for run in evidence["runs"]], ["VALIDATED", "VALIDATED"]
        )
        self.assertEqual([run["gpu"] for run in evidence["runs"]], ["fixture-gpu"] * 2)
        self.assertEqual([run["cold_start_ms"] for run in evidence["runs"]], [0, 0])
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.cancelled, [])
        serialized = evidence_path.read_text(encoding="utf-8")
        self.assertNotIn(self.config.api_key, serialized)
        for request_path in self.config.request_paths:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            for field in ("input_claim", "output_claim", "callback_claim"):
                self.assertNotIn(request[field], serialized)

    def test_budget_is_checked_before_first_submit(self) -> None:
        config = copy.copy(self.config)
        object.__setattr__(config, "cost_limit_usd", Decimal("0.5"))
        provider = FakeProvider()
        with self.assertRaises(run_worker_poc.CostLimitError):
            run_worker_poc.run_paid_poc(
                config,
                self.root / "budget-evidence.json",
                client=provider,
                media_validator=lambda _path, _aspect: {},
            )
        self.assertEqual(provider.requests, [])

    def test_execution_timeout_cancels_job_and_records_failure(self) -> None:
        provider = FakeProvider(never_complete=True)
        clock = FakeClock()
        evidence_path = self.root / "timeout-evidence.json"
        with self.assertRaises(run_worker_poc.CostLimitError):
            run_worker_poc.run_paid_poc(
                self.config,
                evidence_path,
                client=provider,
                clock=clock,
                sleep=clock.sleep,
                media_validator=lambda _path, _aspect: {},
            )
        self.assertEqual(provider.cancelled, ["job-1"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["status"], "FAILED")
        self.assertEqual(evidence["cleanup"]["active_jobs_remaining"], 0)
        self.assertEqual(evidence["rollback"]["status"], "COMPLETED")
        self.assertEqual(evidence["failures"][0]["type"], "CostLimitError")

    def test_finally_records_failed_rollback(self) -> None:
        provider = FakeProvider(never_complete=True, cancel_fails=True)
        clock = FakeClock()
        evidence_path = self.root / "rollback-evidence.json"
        with self.assertRaises(run_worker_poc.CostLimitError):
            run_worker_poc.run_paid_poc(
                self.config,
                evidence_path,
                client=provider,
                clock=clock,
                sleep=clock.sleep,
                media_validator=lambda _path, _aspect: {},
            )
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(evidence["cleanup"]["cancel_failures"], 1)
        self.assertGreaterEqual(evidence["cleanup"]["active_jobs_remaining"], 1)
        self.assertEqual(evidence["rollback"]["status"], "INCOMPLETE")

    def test_cleanup_replays_only_unfinished_recorded_jobs(self) -> None:
        evidence_path = self.root / "interrupted-evidence.json"
        evidence = run_worker_poc.new_evidence(self.config)
        evidence["runs"] = [
            {"provider_job_id": "job-unfinished", "status": "IN_PROGRESS"},
            {"provider_job_id": "job-complete", "status": "COMPLETED"},
        ]
        run_worker_poc.atomic_write_json(evidence_path, evidence)
        provider = FakeProvider()
        cancelled = run_worker_poc.cleanup_from_evidence(
            run_worker_poc.CleanupConfig("secret", "endpoint-test"),
            evidence_path,
            client=provider,
        )
        self.assertEqual(cancelled, 1)
        self.assertEqual(provider.cancelled, ["job-unfinished"])
        updated = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["runs"][0]["status"], "CANCELLED_BY_CLEANUP")
        self.assertEqual(updated["rollback"]["status"], "COMPLETED")

    def test_checked_in_synthetic_claims_cannot_be_submitted(self) -> None:
        checked_in = (
            ROOT
            / "workers"
            / "runpod-comfyui"
            / "contract"
            / "fixtures"
            / "request-16x9.json"
        )
        with self.assertRaises(run_worker_poc.ConfigurationError):
            run_worker_poc.load_request(checked_in, "16:9")

    def test_real_evidence_cannot_be_written_inside_repository(self) -> None:
        with self.assertRaises(run_worker_poc.ConfigurationError):
            run_worker_poc.require_private_evidence_path(
                ROOT / "issue-14-evidence.json"
            )

    def test_poc_media_validator_accepts_the_existing_r10_fixture(self) -> None:
        media = (
            ROOT
            / "apps"
            / "api"
            / "tests"
            / "fixtures"
            / "media"
            / "valid-720p-h264.mp4"
        )
        facts = run_worker_poc.validate_media(media, "16:9")
        self.assertEqual((facts["width"], facts["height"]), (1280, 720))
        self.assertEqual(
            (facts["container"], facts["codec"], facts["pix_fmt"]),
            ("mp4", "h264", "yuv420p"),
        )

    def test_evidence_template_contains_all_gate_sections(self) -> None:
        path = ROOT / "workers" / "runpod-comfyui" / "evidence-template.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            value["status"], "BLOCKED_PENDING_CREDENTIAL_AND_COST_AUTHORIZATION"
        )
        self.assertEqual(
            [run["aspect_ratio"] for run in value["runs"]], ["16:9", "9:16"]
        )
        self.assertIn("cleanup", value)
        self.assertIn("rollback", value)
        self.assertIn("cost", value)
        self.assertIn("failures", value)
        self.assertIsNone(value["decision"])


if __name__ == "__main__":
    unittest.main()
