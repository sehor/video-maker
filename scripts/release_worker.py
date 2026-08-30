from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROUTE_ID = "fast_wan_i2v_720_v1"
OCI_REFERENCE_PATTERN = re.compile(
    r"^(?P<name>[^@\s]+)@(?P<digest>sha256:[a-f0-9]{64})$"
)
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
FIXED_MODELS = frozenset(
    {
        "wan2.1_i2v_14B_fp8_e4m3fn.safetensors",
        "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "wan_2.1_vae.safetensors",
    }
)
APPROVED_MODEL_STATUSES = frozenset({"APPROVED", "APPROVED_PRODUCTION"})
FIXED_COMFY_LOCK_VALUES = {
    "worker_comfyui_version": "5.8.7",
    "worker_comfyui_commit": "a1981e99b1f5a7201f387653420ad1f275b97d0a",
    "comfyui_version": "0.29.0",
    "comfyui_commit": "a8c44f9b2a0678ac4082e3529a3f43db7472acfe",
    "comfy_cli_version": "1.18.0",
    "comfyui": "a8c44f9b2a0678ac4082e3529a3f43db7472acfe",
    "file_custom_nodes": "[]",
    "git_custom_nodes": "{}",
}
NOTICE_MARKERS = frozenset(
    {
        "a1981e99b1f5a7201f387653420ad1f275b97d0a",
        "a8c44f9b2a0678ac4082e3529a3f43db7472acfe",
        "6a5e9d772453f27b0778b14e8a4b1e25ac5f949e",
        "FFmpeg",
    }
)
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class ReleaseGateError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseGateError(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise ReleaseGateError(f"JSON artifact must contain an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseGateError(f"cannot hash artifact: {path}") from exc
    return digest.hexdigest()


def sha256_normalized_text(path: Path) -> str:
    try:
        payload = path.read_bytes().replace(b"\r\n", b"\n")
    except OSError as exc:
        raise ReleaseGateError(f"cannot hash text artifact: {path}") from exc
    return hashlib.sha256(payload).hexdigest()


def parse_oci_reference(value: str) -> tuple[str, str]:
    match = OCI_REFERENCE_PATTERN.fullmatch(value)
    if match is None or ":latest" in match.group("name").lower():
        raise ReleaseGateError(
            "OCI image must use an immutable sha256 digest, never latest"
        )
    return match.group("name"), match.group("digest")


def validate_spdx_sbom(path: Path) -> None:
    sbom = load_json(path)
    if not str(sbom.get("spdxVersion", "")).startswith("SPDX-2."):
        raise ReleaseGateError("SBOM must use SPDX JSON")
    if not isinstance(sbom.get("documentNamespace"), str):
        raise ReleaseGateError("SBOM document namespace is missing")
    packages = sbom.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ReleaseGateError("SBOM must contain at least one package")
    if any(not isinstance(item, dict) or not item.get("name") for item in packages):
        raise ReleaseGateError("SBOM contains an invalid package entry")


def verify_registry_digest(
    image_reference: str,
    *,
    runner: CommandRunner = subprocess.run,
) -> str:
    _, expected_digest = parse_oci_reference(image_reference)
    try:
        result = runner(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                image_reference,
                "--format",
                "{{json .Manifest.Digest}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseGateError("unable to inspect OCI registry digest") from exc
    try:
        observed_digest = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseGateError("OCI registry returned an invalid digest") from exc
    if result.returncode != 0 or observed_digest != expected_digest:
        raise ReleaseGateError("OCI registry did not confirm the requested digest")
    return expected_digest


def generate_sbom(
    image_reference: str,
    output: Path,
    *,
    syft_binary: str = "syft",
    runner: CommandRunner = subprocess.run,
) -> None:
    parse_oci_reference(image_reference)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = runner(
            [syft_binary, image_reference, "-o", f"spdx-json={output}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseGateError("unable to generate SPDX SBOM") from exc
    if result.returncode != 0:
        raise ReleaseGateError("SBOM generator failed")
    validate_spdx_sbom(output)


def _model_registry(root: Path) -> dict[str, dict[str, str]]:
    path = root / "docs" / "licenses" / "models.csv"
    try:
        with path.open(encoding="utf-8", newline="") as source:
            rows = {
                row["name"]: row for row in csv.DictReader(source) if row.get("name")
            }
    except (OSError, KeyError) as exc:
        raise ReleaseGateError("model license registry is invalid") from exc
    return rows


def approved_model_hashes(root: Path) -> dict[str, str]:
    rows = _model_registry(root)
    if set(rows) != FIXED_MODELS:
        raise ReleaseGateError(
            "model registry must contain exactly the fixed workflow models"
        )
    hashes: dict[str, str] = {}
    required_fields = {
        "version",
        "source",
        "license",
        "commercial_saas",
        "notice_obligations",
        "reviewed_at",
        "owner",
    }
    for name, row in rows.items():
        digest = row.get("sha256", "")
        if not SHA256_PATTERN.fullmatch(digest):
            raise ReleaseGateError(f"model digest is not verified: {name}")
        if row.get("status", "").upper() not in APPROVED_MODEL_STATUSES:
            raise ReleaseGateError(f"model license is not approved: {name}")
        if any(not row.get(field, "").strip() for field in required_fields):
            raise ReleaseGateError(f"model license record is incomplete: {name}")
        hashes[name] = digest
    return dict(sorted(hashes.items()))


def validate_custom_node_lock(root: Path) -> str:
    lock_path = root / "workers" / "runpod-comfyui" / "comfy-lock.yaml"
    try:
        lock_text = lock_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseGateError("custom-node lock is missing") from exc
    if "latest" in lock_text.lower():
        raise ReleaseGateError("custom-node lock must not contain latest")
    values: dict[str, list[str]] = {}
    for raw_line in lock_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", maxsplit=1)
        values.setdefault(key, []).append(value.strip())
    for key, expected in FIXED_COMFY_LOCK_VALUES.items():
        if values.get(key) != [expected]:
            raise ReleaseGateError(f"custom-node lock field is not pinned: {key}")

    registry_path = root / "docs" / "licenses" / "custom-nodes.csv"
    try:
        with registry_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            rows = list(reader)
            fieldnames = set(reader.fieldnames or ())
    except OSError as exc:
        raise ReleaseGateError("custom-node license registry is missing") from exc
    if not {"name", "repository", "commit", "sha256", "license", "status"}.issubset(
        fieldnames
    ):
        raise ReleaseGateError("custom-node license registry is invalid")
    if rows:
        raise ReleaseGateError("the fixed release does not approve external custom nodes")
    return sha256_normalized_text(lock_path)


def validate_license_notice(root: Path) -> str:
    notice_path = root / "workers" / "runpod-comfyui" / "THIRD_PARTY_NOTICES.md"
    try:
        notice = notice_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseGateError("worker license notice is missing") from exc
    if any(marker not in notice for marker in NOTICE_MARKERS):
        raise ReleaseGateError("worker license notice does not cover the locked release")
    return sha256_normalized_text(notice_path)


def validate_benchmark(path: Path, *, require_real_gpu: bool) -> None:
    benchmark = load_json(path)
    if benchmark.get("route_id") != ROUTE_ID or benchmark.get("status") != "PASSED":
        raise ReleaseGateError("benchmark smoke did not pass for the fixed route")
    cases = benchmark.get("cases")
    required_cases = {
        "request-16x9",
        "request-9x16",
        "response-success",
        "response-failure",
    }
    if (
        not isinstance(cases, list)
        or {
            item.get("name")
            for item in cases
            if isinstance(item, dict) and item.get("status") == "PASSED"
        }
        != required_cases
    ):
        raise ReleaseGateError("benchmark smoke fixtures are incomplete")
    real_gpu = benchmark.get("real_gpu_benchmark")
    if require_real_gpu and (
        not isinstance(real_gpu, dict)
        or real_gpu.get("executed") is not True
        or real_gpu.get("status") != "PASSED"
        or not real_gpu.get("evidence")
    ):
        raise ReleaseGateError("real GPU benchmark evidence is required for release")


def repository_preflight(root: Path) -> list[str]:
    blockers: list[str] = []
    worker_root = root / "workers" / "runpod-comfyui"
    baseline = load_json(worker_root / "poc-baseline.json")
    real_poc = baseline.get("real_poc", {})
    if (
        baseline.get("adoption_status") != "ADOPTED"
        or baseline.get("poc_status") != "PASSED"
        or not isinstance(real_poc, dict)
        or real_poc.get("decision") not in {"DIRECT_CONFIG", "THIN_ADAPTER"}
    ):
        blockers.append("REAL_RUNPOD_POC_NOT_PASSED")
    if not isinstance(real_poc, dict) or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", str(real_poc.get("image_digest", ""))
    ):
        blockers.append("IMAGE_DIGEST_NOT_VERIFIED")
    try:
        approved_model_hashes(root)
    except ReleaseGateError:
        blockers.append("MODEL_HASHES_AND_LICENSES_NOT_VERIFIED")
    try:
        validate_custom_node_lock(root)
    except ReleaseGateError:
        blockers.append("CUSTOM_NODE_LOCK_NOT_VERIFIED")
    try:
        validate_license_notice(root)
    except ReleaseGateError:
        blockers.append("LICENSE_NOTICE_NOT_VERIFIED")
    try:
        validate_benchmark(worker_root / "benchmark-smoke.json", require_real_gpu=False)
    except ReleaseGateError:
        blockers.append("FIXTURE_BENCHMARK_SMOKE_NOT_PASSED")

    dockerfile = (worker_root / "Dockerfile").read_text(encoding="utf-8")
    production = dockerfile.split(" AS production", maxsplit=1)[-1]
    if any(
        marker in production
        for marker in (
            "EXPOSE 8188",
            "SERVE_API_LOCALLY=true",
            "comfy node install",
            "comfy model download",
            "pip install",
        )
    ):
        blockers.append("PRODUCTION_RUNTIME_OR_PUBLIC_COMFYUI_SURFACE_PRESENT")
    if "rm -rf" not in production or "ComfyUI-Manager" not in production:
        blockers.append("COMFYUI_MANAGER_REMOVAL_NOT_ENFORCED")
    locked_start = (worker_root / "locked-start.sh").read_text(encoding="utf-8")
    if (
        "unset PUBLIC_KEY" not in locked_start
        or "export SERVE_API_LOCALLY=false" not in locked_start
        or "--listen" in locked_start
        or 'CMD ["/opt/video-factory/locked-start.sh"]' not in production
    ):
        blockers.append("PUBLIC_COMFYUI_OR_SSH_STARTUP_NOT_LOCKED")
    return sorted(set(blockers))


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as target:
            json.dump(value, target, ensure_ascii=False, indent=2, sort_keys=True)
            target.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def record_release(
    root: Path,
    *,
    image_reference: str,
    rollback_reference: str,
    sbom_path: Path,
    benchmark_path: Path,
    output_path: Path,
    runner: CommandRunner = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    blockers = repository_preflight(root)
    if blockers:
        raise ReleaseGateError(f"worker release is blocked: {', '.join(blockers)}")
    _, image_digest = parse_oci_reference(image_reference)
    _, rollback_digest = parse_oci_reference(rollback_reference)
    if image_digest == rollback_digest:
        raise ReleaseGateError(
            "rollback image must be a distinct previously accepted digest"
        )
    verify_registry_digest(image_reference, runner=runner)
    verify_registry_digest(rollback_reference, runner=runner)
    validate_spdx_sbom(sbom_path)
    validate_benchmark(benchmark_path, require_real_gpu=True)
    model_hashes = approved_model_hashes(root)
    custom_node_lock_sha256 = validate_custom_node_lock(root)
    license_notice_sha256 = validate_license_notice(root)

    workflow_manifest = load_json(
        root / "workflows" / "manifests" / ROUTE_ID / "manifest.json"
    )
    workflow_path = (
        root
        / "workflows"
        / "manifests"
        / ROUTE_ID
        / str(workflow_manifest["workflow_file"])
    )
    workflow_sha256 = sha256_normalized_text(workflow_path)
    if workflow_sha256 != workflow_manifest.get("workflow_sha256"):
        raise ReleaseGateError("workflow file does not match its immutable manifest")

    released_at = now().astimezone(UTC).isoformat()
    record: dict[str, Any] = {
        "schema_version": 1,
        "route_id": ROUTE_ID,
        "status": "ACCEPTED",
        "released_at": released_at,
        "image": {
            "reference": image_reference,
            "digest": image_digest,
            "registry_verified_at": released_at,
        },
        "rollback_image": {
            "reference": rollback_reference,
            "digest": rollback_digest,
            "registry_verified_at": released_at,
        },
        "provenance": {
            "worker_comfyui_version": "5.8.7",
            "worker_comfyui_commit": "a1981e99b1f5a7201f387653420ad1f275b97d0a",
            "comfyui_version": "0.29.0",
            "comfyui_commit": "a8c44f9b2a0678ac4082e3529a3f43db7472acfe",
            "comfy_cli_version": "1.18.0",
            "workflow_id": ROUTE_ID,
            "workflow_sha256": workflow_sha256,
            "model_sha256": model_hashes,
            "custom_node_lock_sha256": custom_node_lock_sha256,
            "custom_node_commits": {},
        },
        "artifacts": {
            "sbom_path": str(sbom_path),
            "sbom_sha256": sha256_file(sbom_path),
            "license_notice_path": "workers/runpod-comfyui/THIRD_PARTY_NOTICES.md",
            "license_notice_sha256": license_notice_sha256,
            "benchmark_path": str(benchmark_path),
            "benchmark_sha256": sha256_file(benchmark_path),
        },
        "security": {
            "comfyui_manager_present": False,
            "public_comfyui_port": False,
            "runtime_install_enabled": False,
            "unsigned_webhook_enabled": False,
            "arbitrary_execution_fields": False,
        },
        "blockers": [],
    }
    _atomic_json_write(output_path, record)
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Immutable Worker release gate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--root", type=Path, default=Path.cwd())

    sbom = subparsers.add_parser("generate-sbom")
    sbom.add_argument("--image", required=True)
    sbom.add_argument("--output", required=True, type=Path)
    sbom.add_argument("--syft-binary", default="syft")

    record = subparsers.add_parser("record")
    record.add_argument("--root", type=Path, default=Path.cwd())
    record.add_argument("--image", required=True)
    record.add_argument("--rollback-image", required=True)
    record.add_argument("--sbom", required=True, type=Path)
    record.add_argument("--benchmark", required=True, type=Path)
    record.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "preflight":
            blockers = repository_preflight(args.root.resolve())
            print(
                json.dumps(
                    {
                        "status": "BLOCKED" if blockers else "READY",
                        "blockers": blockers,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2 if blockers else 0
        if args.command == "generate-sbom":
            generate_sbom(
                args.image, args.output.resolve(), syft_binary=args.syft_binary
            )
            return 0
        record_release(
            args.root.resolve(),
            image_reference=args.image,
            rollback_reference=args.rollback_image,
            sbom_path=args.sbom.resolve(),
            benchmark_path=args.benchmark.resolve(),
            output_path=args.output.resolve(),
        )
        return 0
    except ReleaseGateError as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
