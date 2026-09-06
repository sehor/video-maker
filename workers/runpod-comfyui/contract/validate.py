from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

WORKER_COMFYUI_VERSION = "5.8.7"
WORKER_COMFYUI_COMMIT = "a1981e99b1f5a7201f387653420ad1f275b97d0a"
COMFYUI_COMMIT = "a8c44f9b2a0678ac4082e3529a3f43db7472acfe"
COMFY_CLI_VERSION = "1.18.0"
WORKFLOW_ID = "fast_wan_i2v_720_v1"
REQUEST_FIELDS = frozenset(
    {
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
)
RESPONSE_FIELDS = frozenset(
    {"attempt_id", "status", "output", "metrics", "versions", "error"}
)
CLAIM_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class ContractError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return value


def require_exact_fields(
    value: dict[str, Any], expected: frozenset[str], label: str
) -> None:
    actual = frozenset(value)
    if actual != expected:
        unexpected = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise ContractError(
            f"{label} fields differ; unexpected={unexpected}, missing={missing}"
        )


def require_uuid(value: object, label: str) -> None:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ContractError(f"{label} must be a UUID") from exc


def require_claim(value: object, label: str) -> None:
    if not isinstance(value, str) or not CLAIM_PATTERN.fullmatch(value):
        raise ContractError(f"{label} must be an opaque signed claim")
    if "://" in value:
        raise ContractError(f"{label} must not be a URL")


def validate_request(value: dict[str, Any]) -> None:
    require_exact_fields(value, REQUEST_FIELDS, "request")
    require_uuid(value["job_id"], "job_id")
    require_uuid(value["attempt_id"], "attempt_id")
    if value["workflow_id"] != WORKFLOW_ID:
        raise ContractError("workflow_id is not the fixed system workflow")
    if not isinstance(value["prompt"], str) or not 1 <= len(value["prompt"]) <= 2000:
        raise ContractError("prompt length must be between 1 and 2000 characters")
    if value["duration_ms"] != 5000:
        raise ContractError("only five-second generation is allowed")
    if value["aspect_ratio"] not in {"16:9", "9:16"}:
        raise ContractError("only 16:9 and 9:16 are allowed")
    if value["resolution"] != "720p":
        raise ContractError("only 720p is allowed")
    for field in ("input_claim", "output_claim", "callback_claim"):
        require_claim(value[field], field)


def require_nonnegative_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{label} must be a non-negative integer")


def validate_response(value: dict[str, Any], workflow_sha256: str) -> None:
    require_exact_fields(value, RESPONSE_FIELDS, "response")
    require_uuid(value["attempt_id"], "attempt_id")
    if value["status"] not in {"SUCCEEDED", "FAILED"}:
        raise ContractError("response status is invalid")

    if value["status"] == "SUCCEEDED":
        if value["error"] is not None or not isinstance(value["output"], dict):
            raise ContractError("successful response must contain only output")
        output = value["output"]
        require_exact_fields(
            output,
            frozenset(
                {"claim", "object_key", "media_type", "size_bytes", "sha256"}
            ),
            "output",
        )
        require_claim(output["claim"], "output.claim")
        object_key = output["object_key"]
        if (
            not isinstance(object_key, str)
            or not 1 <= len(object_key) <= 255
            or object_key.startswith("/")
            or any(part in {"", ".", ".."} for part in object_key.split("/"))
        ):
            raise ContractError("output.object_key is invalid")
        if output["media_type"] != "video/mp4":
            raise ContractError("output must be video/mp4")
        require_nonnegative_int(output["size_bytes"], "output.size_bytes")
        if output["size_bytes"] == 0 or not SHA256_PATTERN.fullmatch(
            output["sha256"]
        ):
            raise ContractError("output metadata is incomplete")
    else:
        if value["output"] is not None or not isinstance(value["error"], dict):
            raise ContractError("failed response must contain only error")
        error = value["error"]
        require_exact_fields(error, frozenset({"code", "message"}), "error")
        allowed_error_codes = {
            "INVALID_INPUT",
            "POLICY_REJECTED",
            "WORKER_INTERRUPTED",
            "MODEL_LOAD_FAILED",
            "OUT_OF_MEMORY",
            "WORKFLOW_FAILED",
            "ASSET_DOWNLOAD_FAILED",
            "OUTPUT_UPLOAD_FAILED",
            "OUTPUT_MISSING",
            "OUTPUT_CORRUPTED",
            "INTERNAL_ERROR",
        }
        if error["code"] not in allowed_error_codes:
            raise ContractError("worker error code is outside the closed contract")
        if (
            not isinstance(error["message"], str)
            or not 1 <= len(error["message"]) <= 1000
        ):
            raise ContractError("worker error message is invalid")

    metrics = value["metrics"]
    metric_fields = frozenset(
        {
            "gpu",
            "queue_ms",
            "cold_start_ms",
            "runtime_ms",
            "billable_ms",
            "cost_minor",
            "currency",
            "cost_source",
        }
    )
    require_exact_fields(metrics, metric_fields, "metrics")
    if not isinstance(metrics["gpu"], str) or not 1 <= len(metrics["gpu"]) <= 200:
        raise ContractError("metrics.gpu must identify the provider GPU")
    for field in (
        "queue_ms",
        "cold_start_ms",
        "runtime_ms",
        "billable_ms",
        "cost_minor",
    ):
        require_nonnegative_int(metrics[field], f"metrics.{field}")
    if not re.fullmatch(r"[A-Z]{3}", metrics["currency"]):
        raise ContractError("metrics.currency must be an ISO-style currency code")
    if metrics["cost_source"] not in {"ACTUAL", "ESTIMATE"}:
        raise ContractError("metrics.cost_source is invalid")

    versions = value["versions"]
    version_fields = frozenset(
        {
            "worker_comfyui",
            "worker_commit",
            "comfyui_commit",
            "comfy_cli",
            "image_digest",
            "workflow_id",
            "workflow_sha256",
            "model_sha256",
        }
    )
    require_exact_fields(versions, version_fields, "versions")
    expected_versions = {
        "worker_comfyui": WORKER_COMFYUI_VERSION,
        "worker_commit": WORKER_COMFYUI_COMMIT,
        "comfyui_commit": COMFYUI_COMMIT,
        "comfy_cli": COMFY_CLI_VERSION,
        "workflow_id": WORKFLOW_ID,
        "workflow_sha256": workflow_sha256,
    }
    for field, expected in expected_versions.items():
        if versions[field] != expected:
            raise ContractError(f"versions.{field} is not pinned to {expected}")
    model_hashes = versions["model_sha256"]
    if not isinstance(model_hashes, dict) or not model_hashes:
        raise ContractError(
            "at least one model SHA-256 is required in a success response"
        )
    if any(not SHA256_PATTERN.fullmatch(item) for item in model_hashes.values()):
        raise ContractError("model_sha256 contains an invalid digest")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", versions["image_digest"]):
        raise ContractError("versions.image_digest is invalid")


def repository_paths(root: Path) -> tuple[Path, Path, Path | None]:
    packaged_contract = root / "contract"
    if packaged_contract.is_dir():
        return packaged_contract, root / "workflow", None
    return (
        root / "workers" / "runpod-comfyui" / "contract",
        root / "workflows" / "manifests" / WORKFLOW_ID,
        root / "workers" / "runpod-comfyui" / "poc-baseline.json",
    )


def validate_repository(
    root: Path,
    *,
    worker_commit: str = WORKER_COMFYUI_COMMIT,
    comfyui_commit: str = COMFYUI_COMMIT,
) -> None:
    if worker_commit != WORKER_COMFYUI_COMMIT or comfyui_commit != COMFYUI_COMMIT:
        raise ContractError(
            "build arguments do not match the accepted upstream commits"
        )
    contract_root, workflow_root, baseline_path = repository_paths(root)
    request_schema = load_json(contract_root / "request.schema.json")
    response_schema = load_json(contract_root / "response.schema.json")
    if (
        request_schema.get("additionalProperties") is not False
        or frozenset(request_schema.get("properties", {})) != REQUEST_FIELDS
    ):
        raise ContractError(
            "request schema does not match the fail-closed request contract"
        )
    if (
        response_schema.get("additionalProperties") is not False
        or frozenset(response_schema.get("properties", {})) != RESPONSE_FIELDS
    ):
        raise ContractError(
            "response schema does not match the fail-closed response contract"
        )

    workflow_path = workflow_root / "workflow_api.json"
    manifest = load_json(workflow_root / "manifest.json")
    workflow = load_json(workflow_path)
    workflow_bytes = workflow_path.read_bytes().replace(b"\r\n", b"\n")
    workflow_sha256 = hashlib.sha256(workflow_bytes).hexdigest()
    if (
        manifest["workflow_id"] != WORKFLOW_ID
        or manifest["workflow_sha256"] != workflow_sha256
    ):
        raise ContractError(
            "workflow manifest does not match the immutable workflow file"
        )
    if manifest["dimensions"] != {
        "16:9": {"width": 1280, "height": 720},
        "9:16": {"width": 720, "height": 1280},
    }:
        raise ContractError("workflow dimensions are not the approved 720p pair")
    serialized_workflow = json.dumps(workflow, sort_keys=True)
    if "://" in serialized_workflow:
        raise ContractError("workflow must not contain a URL")
    fixed_model_files = {
        "wan2.1_i2v_14B_fp8_e4m3fn.safetensors",
        "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "wan_2.1_vae.safetensors",
    }
    if set(manifest["fixed_model_files"]) != fixed_model_files:
        raise ContractError("workflow model selection is not fixed")
    workflow_model_files = {
        item
        for node in workflow.values()
        for item in node.get("inputs", {}).values()
        if isinstance(item, str) and item.endswith(".safetensors")
    }
    if workflow_model_files != fixed_model_files:
        raise ContractError("workflow model files differ from the immutable manifest")
    if (
        serialized_workflow.count("__SYSTEM_INPUT_FILENAME__") != 1
        or serialized_workflow.count("__SYSTEM_PROMPT__") != 1
    ):
        raise ContractError("workflow contains an unexpected system binding surface")

    for fixture in sorted((contract_root / "fixtures").glob("request-*.json")):
        validate_request(load_json(fixture))
    for name in ("response-success.json", "response-failure.json"):
        validate_response(load_json(contract_root / "fixtures" / name), workflow_sha256)

    if baseline_path is not None:
        baseline = load_json(baseline_path)
        upstream = baseline["upstream"]
        expected_upstream = {
            "worker_comfyui": (WORKER_COMFYUI_VERSION, WORKER_COMFYUI_COMMIT),
            "comfyui": ("0.29.0", COMFYUI_COMMIT),
            "comfy_cli": (
                COMFY_CLI_VERSION,
                "6a5e9d772453f27b0778b14e8a4b1e25ac5f949e",
            ),
        }
        for component, (version, commit) in expected_upstream.items():
            if (
                upstream[component]["version"] != version
                or upstream[component]["commit"] != commit
            ):
                raise ContractError(
                    f"{component} baseline does not match the official tag commit"
                )
        if baseline["adoption_status"] != "CONDITIONAL":
            raise ContractError(
                "worker-comfyui must remain conditional before the real POC"
            )
        authorization = baseline["authorization"]
        real_poc = baseline["real_poc"]
        if (
            not authorization["credentials_provided"]
            or not authorization["cost_approved"]
        ):
            allowed_statuses = {
                "BLOCKED_PENDING_CREDENTIAL_AND_COST_AUTHORIZATION",
                "SKIPPED_BY_USER_UNVALIDATED",
            }
            if baseline["poc_status"] not in allowed_statuses:
                raise ContractError("unauthorized real POC cannot be marked as passed")
            if real_poc["runs"]:
                raise ContractError(
                    "real POC results cannot be claimed without authorization"
                )
            if baseline["poc_status"] == "SKIPPED_BY_USER_UNVALIDATED":
                skip_record = baseline.get("skip_record", {})
                if (
                    real_poc["decision"] != "SKIPPED_UNVALIDATED"
                    or skip_record.get("real_runpod_poc_executed") is not False
                    or skip_record.get("reason")
                    != "MISSING_CREDENTIALS_AND_COST_AUTHORIZATION"
                    or authorization.get("user_selected_skip") is not True
                ):
                    raise ContractError(
                        "skipped POC must remain explicitly unvalidated"
                    )
                unverified_fields = (
                    "image_digest",
                    "cold_start_ms",
                    "runtime_ms",
                    "cost_minor",
                    "currency",
                )
                if any(real_poc[field] is not None for field in unverified_fields):
                    raise ContractError(
                        "skipped POC cannot contain fabricated runtime evidence"
                    )
                if real_poc["source_build_verified"] or real_poc["model_sha256"]:
                    raise ContractError(
                        "skipped POC cannot contain fabricated supply-chain evidence"
                    )
            elif real_poc["decision"] is not None:
                raise ContractError("blocked POC cannot contain a decision")
        evidence_template = load_json(baseline_path.with_name("evidence-template.json"))
        if (
            evidence_template["status"]
            != "BLOCKED_PENDING_CREDENTIAL_AND_COST_AUTHORIZATION"
        ):
            raise ContractError(
                "evidence template must remain blocked before the paid POC"
            )
        if [item.get("aspect_ratio") for item in evidence_template["runs"]] != [
            "16:9",
            "9:16",
        ]:
            raise ContractError(
                "evidence template must cover both approved aspect ratios"
            )
        serialized_evidence = json.dumps(evidence_template, sort_keys=True)
        if any(
            field in serialized_evidence
            for field in ("input_claim", "output_claim", "callback_claim")
        ):
            raise ContractError("evidence template must never include storage claims")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate the fixed worker-comfyui POC contract"
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--worker-commit", default=WORKER_COMFYUI_COMMIT)
    parser.add_argument("--comfyui-commit", default=COMFYUI_COMMIT)
    args = parser.parse_args()
    validate_repository(
        args.root.resolve(),
        worker_commit=args.worker_commit,
        comfyui_commit=args.comfyui_commit,
    )


if __name__ == "__main__":
    main()
