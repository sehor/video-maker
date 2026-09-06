from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_worker.py"
SPEC = importlib.util.spec_from_file_location("release_worker", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_worker)


class WorkerReleaseGateTests(unittest.TestCase):
    def test_current_release_is_honestly_blocked_by_unvalidated_poc(self) -> None:
        blockers = release_worker.repository_preflight(ROOT)
        self.assertEqual(
            blockers,
            [
                "IMAGE_DIGEST_NOT_VERIFIED",
                "MODEL_HASHES_AND_LICENSES_NOT_VERIFIED",
                "REAL_RUNPOD_POC_NOT_PASSED",
            ],
        )

    def test_fixture_benchmark_smoke_passes_but_is_not_real_gpu_evidence(self) -> None:
        benchmark = ROOT / "workers" / "runpod-comfyui" / "benchmark-smoke.json"
        release_worker.validate_benchmark(benchmark, require_real_gpu=False)
        with self.assertRaises(release_worker.ReleaseGateError):
            release_worker.validate_benchmark(benchmark, require_real_gpu=True)

    def test_custom_nodes_and_license_notice_are_locked_for_release(self) -> None:
        custom_node_lock = release_worker.validate_custom_node_lock(ROOT)
        license_notice = release_worker.validate_license_notice(ROOT)
        self.assertRegex(custom_node_lock, r"^[a-f0-9]{64}$")
        self.assertRegex(license_notice, r"^[a-f0-9]{64}$")

    def test_oci_release_and_rollback_references_require_digests(self) -> None:
        digest = "a" * 64
        name, parsed = release_worker.parse_oci_reference(
            f"registry.example/video-factory/worker@sha256:{digest}"
        )
        self.assertEqual(name, "registry.example/video-factory/worker")
        self.assertEqual(parsed, f"sha256:{digest}")
        for invalid in (
            "registry.example/worker:latest",
            "registry.example/worker:1.0",
            f"registry.example/worker:latest@sha256:{digest}",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(release_worker.ReleaseGateError),
            ):
                release_worker.parse_oci_reference(invalid)

    def test_registry_digest_verification_uses_exact_immutable_reference(self) -> None:
        digest = f"sha256:{'b' * 64}"
        reference = f"registry.example/worker@{digest}"
        calls: list[list[str]] = []

        def runner(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=f'"{digest}"\n', stderr=""
            )

        self.assertEqual(
            release_worker.verify_registry_digest(reference, runner=runner), digest
        )
        self.assertEqual(
            calls,
            [
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    reference,
                    "--format",
                    "{{json .Manifest.Digest}}",
                ]
            ],
        )

        def ambiguous_runner(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=f'"prefix-{digest}"\n', stderr=""
            )

        with self.assertRaises(release_worker.ReleaseGateError):
            release_worker.verify_registry_digest(reference, runner=ambiguous_runner)

    def test_spdx_sbom_requires_packages_and_namespace(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".test-tmp") as temporary:
            path = Path(temporary) / "sbom.spdx.json"
            path.write_text(
                json.dumps(
                    {
                        "spdxVersion": "SPDX-2.3",
                        "documentNamespace": "https://video-factory.invalid/sbom/fixture",
                        "packages": [{"name": "worker-comfyui"}],
                    }
                ),
                encoding="utf-8",
            )
            release_worker.validate_spdx_sbom(path)
            path.write_text(
                json.dumps(
                    {
                        "spdxVersion": "SPDX-2.3",
                        "documentNamespace": "https://video-factory.invalid/sbom/fixture",
                        "packages": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(release_worker.ReleaseGateError):
                release_worker.validate_spdx_sbom(path)

    def test_release_template_records_all_unverified_gates_without_latest(self) -> None:
        path = ROOT / "workers" / "runpod-comfyui" / "release" / "release.template.json"
        template = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(template["status"], "BLOCKED_UNVALIDATED")
        self.assertFalse(template["security"]["comfyui_manager_present"])
        self.assertFalse(template["security"]["public_comfyui_port"])
        self.assertFalse(template["security"]["runtime_install_enabled"])
        self.assertEqual(
            template["provenance"]["custom_node_lock_sha256"],
            release_worker.validate_custom_node_lock(ROOT),
        )
        self.assertEqual(
            template["artifacts"]["license_notice_sha256"],
            release_worker.validate_license_notice(ROOT),
        )
        self.assertNotIn("latest", path.read_text(encoding="utf-8").lower())
        self.assertEqual(
            set(template["blockers"]),
            {
                "REAL_RUNPOD_POC_NOT_PASSED",
                "IMAGE_DIGEST_NOT_VERIFIED",
                "MODEL_HASHES_AND_LICENSES_NOT_VERIFIED",
                "SBOM_NOT_GENERATED",
                "ROLLBACK_IMAGE_NOT_VERIFIED",
            },
        )


if __name__ == "__main__":
    unittest.main()
