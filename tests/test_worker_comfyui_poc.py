from __future__ import annotations

import copy
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "workers" / "runpod-comfyui" / "contract" / "validate.py"
SPEC = importlib.util.spec_from_file_location("worker_contract", VALIDATOR_PATH)
assert SPEC is not None and SPEC.loader is not None
worker_contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker_contract)


def fixture(name: str) -> dict[str, object]:
    path = ROOT / "workers" / "runpod-comfyui" / "contract" / "fixtures" / name
    return json.loads(path.read_text(encoding="utf-8"))


class WorkerComfyUIPocTests(unittest.TestCase):
    def test_repository_contract_is_self_consistent(self) -> None:
        worker_contract.validate_repository(ROOT)

    def test_response_schema_rejects_unsafe_object_keys(self) -> None:
        schema_path = (
            ROOT / "workers" / "runpod-comfyui" / "contract" / "response.schema.json"
        )
        response_schema = json.loads(schema_path.read_text(encoding="utf-8"))
        pattern = response_schema["$defs"]["output"]["properties"]["object_key"][
            "pattern"
        ]
        self.assertIsNotNone(re.fullmatch(pattern, "outputs/attempt.mp4"))
        for invalid in ("/absolute.mp4", "outputs//video.mp4", "../video.mp4"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(re.fullmatch(pattern, invalid))

    def test_user_skipped_real_poc_is_explicitly_unvalidated(self) -> None:
        path = ROOT / "workers" / "runpod-comfyui" / "poc-baseline.json"
        baseline = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(baseline["adoption_status"], "CONDITIONAL")
        self.assertEqual(baseline["poc_status"], "SKIPPED_BY_USER_UNVALIDATED")
        self.assertFalse(baseline["skip_record"]["real_runpod_poc_executed"])
        real_poc = baseline["real_poc"]
        self.assertEqual(real_poc["decision"], "SKIPPED_UNVALIDATED")
        self.assertEqual(real_poc["runs"], [])
        for field in (
            "image_digest",
            "cold_start_ms",
            "runtime_ms",
            "cost_minor",
            "currency",
        ):
            self.assertIsNone(real_poc[field])

    def test_request_rejects_every_unapproved_execution_surface(self) -> None:
        base = fixture("request-16x9.json")
        forbidden = {
            "workflow": {},
            "nodes": [],
            "model": "other.safetensors",
            "code": "print('owned')",
            "url": "https://example.invalid/input.png",
        }
        for field, value in forbidden.items():
            with (
                self.subTest(field=field),
                self.assertRaises(worker_contract.ContractError),
            ):
                candidate = copy.deepcopy(base)
                candidate[field] = value
                worker_contract.validate_request(candidate)

    def test_request_rejects_url_instead_of_signed_claim(self) -> None:
        candidate = fixture("request-9x16.json")
        candidate["input_claim"] = "https://example.invalid/input.png"
        with self.assertRaises(worker_contract.ContractError):
            worker_contract.validate_request(candidate)

    def test_request_rejects_unapproved_resolution_duration_and_aspect(self) -> None:
        base = fixture("request-16x9.json")
        for field, value in (
            ("resolution", "1080p"),
            ("duration_ms", 10000),
            ("aspect_ratio", "1:1"),
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(worker_contract.ContractError),
            ):
                candidate = copy.deepcopy(base)
                candidate[field] = value
                worker_contract.validate_request(candidate)

    def test_production_dockerfile_is_pinned_and_manager_free(self) -> None:
        dockerfile = (ROOT / "workers" / "runpod-comfyui" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("ARG WORKER_COMFYUI_IMAGE\n", dockerfile)
        self.assertIn("@sha256:[0-9a-f]{64}", dockerfile)
        self.assertNotIn("runpod/worker-comfyui:5.8.7", dockerfile)
        self.assertIn("COMFY_CLI_VERSION=1.18.0", dockerfile)
        self.assertIn(worker_contract.WORKER_COMFYUI_COMMIT, dockerfile)
        self.assertIn(worker_contract.COMFYUI_COMMIT, dockerfile)
        self.assertNotIn(":latest", dockerfile)
        self.assertNotIn("EXPOSE 8188", dockerfile)
        production = dockerfile.split(" AS production", maxsplit=1)[1]
        self.assertNotIn("pip install", production)
        self.assertIn("rm -rf", production)
        self.assertIn("ComfyUI-Manager", production)
        self.assertIn('CMD ["/opt/video-factory/locked-start.sh"]', production)
        locked_start = (
            ROOT / "workers" / "runpod-comfyui" / "locked-start.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("unset PUBLIC_KEY", locked_start)
        self.assertIn("export SERVE_API_LOCALLY=false", locked_start)
        self.assertNotIn("--listen", locked_start)


if __name__ == "__main__":
    unittest.main()
