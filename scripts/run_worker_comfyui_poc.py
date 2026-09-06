from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "workers" / "runpod-comfyui" / "contract" / "validate.py"
WORKFLOW_PATH = ROOT / "workflows" / "manifests" / "fast_wan_i2v_720_v1"
RUNPOD_API_ORIGIN = "https://api.runpod.ai"
RUNPOD_API_PREFIX = f"{RUNPOD_API_ORIGIN}/v2"
CONFIRMATION = "ISSUE-14-PAID-POC"
MAX_HTTP_BYTES = 1_000_000
MAX_CONFIG_BYTES = 1_000_000
MAX_MEDIA_BYTES = 512 * 1024 * 1024
MAX_COST_LIMIT_USD = Decimal("25")
MIN_EXECUTION_SECONDS = 30
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"})
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")


SPEC = importlib.util.spec_from_file_location("worker_contract", CONTRACT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("worker contract validator could not be loaded")
worker_contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker_contract)


class PocError(RuntimeError):
    pass


class ConfigurationError(PocError):
    pass


class CostLimitError(PocError):
    pass


class ProviderError(PocError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_decimal(
    name: str, raw: str | None, *, maximum: Decimal | None = None
) -> Decimal:
    if raw is None or not raw.strip():
        raise ConfigurationError(f"{name} is required")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigurationError(f"{name} must be a decimal number") from exc
    if not value.is_finite() or value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} exceeds the safety maximum")
    return value


def require_id(name: str, value: str | None) -> str:
    if value is None or not ID_PATTERN.fullmatch(value):
        raise ConfigurationError(f"{name} is missing or invalid")
    return value


def require_file(
    name: str, value: str | None, *, maximum_bytes: int = MAX_CONFIG_BYTES
) -> Path:
    if value is None:
        raise ConfigurationError(f"{name} is required")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ConfigurationError(f"{name} does not reference a file")
    if path.stat().st_size > maximum_bytes:
        raise ConfigurationError(f"{name} exceeds the safety size limit")
    return path


def require_outside_repository(name: str, path: Path) -> Path:
    if path.is_relative_to(ROOT):
        raise ConfigurationError(f"{name} must remain outside the repository")
    return path


def registry_host(image: str) -> str:
    first = image.split("/", maxsplit=1)[0]
    return (
        first if "." in first or ":" in first or first == "localhost" else "docker.io"
    )


def docker_config_has_auth(image: str, home: Path | None = None) -> bool:
    config_path = (home or Path.home()) / ".docker" / "config.json"
    if not config_path.is_file():
        return False
    try:
        auths = json.loads(config_path.read_text(encoding="utf-8")).get("auths", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
    host = registry_host(image)
    aliases = {host, f"https://{host}", f"https://{host}/v1/"}
    return any(alias in auths and bool(auths[alias]) for alias in aliases)


def environment_presence(env: Mapping[str, str]) -> dict[str, bool]:
    image = env.get("POC_REGISTRY_IMAGE", "")
    env_auth = bool(env.get("POC_REGISTRY_USERNAME")) and bool(
        env.get("POC_REGISTRY_TOKEN")
    )
    config_auth = bool(image) and docker_config_has_auth(image)
    return {
        "runpod_api_key": bool(env.get("RUNPOD_API_KEY")),
        "runpod_endpoint_id": bool(env.get("RUNPOD_ENDPOINT_ID")),
        "cost_limit": bool(env.get("POC_COST_LIMIT_USD")),
        "cost_rate": bool(env.get("POC_COST_PER_SECOND_USD")),
        "registry_image": bool(image),
        "registry_auth": env_auth or config_auth,
        "image_digest": bool(env.get("POC_IMAGE_DIGEST")),
        "request_16x9": bool(env.get("POC_REQUEST_16X9")),
        "request_9x16": bool(env.get("POC_REQUEST_9X16")),
        "media_dir": bool(env.get("POC_MEDIA_DIR")),
        "model_hashes": bool(env.get("POC_MODEL_SHA256_JSON")),
    }


@dataclass(frozen=True, slots=True)
class PocConfig:
    api_key: str
    endpoint_id: str
    registry_image: str
    image_digest: str
    cost_limit_usd: Decimal
    cost_per_second_usd: Decimal
    cancellation_reserve_seconds: int
    poll_seconds: float
    request_paths: tuple[Path, Path]
    media_dir: Path
    model_hashes: dict[str, str]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PocConfig:
        api_key = env.get("RUNPOD_API_KEY")
        if not api_key:
            raise ConfigurationError("RUNPOD_API_KEY is required")
        endpoint_id = require_id("RUNPOD_ENDPOINT_ID", env.get("RUNPOD_ENDPOINT_ID"))
        image = env.get("POC_REGISTRY_IMAGE", "")
        if not IMAGE_PATTERN.fullmatch(image) or image.endswith(":latest"):
            raise ConfigurationError(
                "POC_REGISTRY_IMAGE must be a non-latest tagged image"
            )
        env_auth = bool(env.get("POC_REGISTRY_USERNAME")) and bool(
            env.get("POC_REGISTRY_TOKEN")
        )
        if not env_auth and not docker_config_has_auth(image):
            raise ConfigurationError("isolated registry authentication is required")
        digest = env.get("POC_IMAGE_DIGEST", "")
        if not SHA256_PATTERN.fullmatch(digest):
            raise ConfigurationError(
                "POC_IMAGE_DIGEST must be an immutable sha256 digest"
            )
        limit = parse_decimal(
            "POC_COST_LIMIT_USD",
            env.get("POC_COST_LIMIT_USD"),
            maximum=MAX_COST_LIMIT_USD,
        )
        rate = parse_decimal(
            "POC_COST_PER_SECOND_USD", env.get("POC_COST_PER_SECOND_USD")
        )
        reserve_raw = env.get("POC_CANCEL_RESERVE_SECONDS", "15")
        try:
            reserve = int(reserve_raw)
        except ValueError as exc:
            raise ConfigurationError(
                "POC_CANCEL_RESERVE_SECONDS must be an integer"
            ) from exc
        if not 5 <= reserve <= 60:
            raise ConfigurationError(
                "POC_CANCEL_RESERVE_SECONDS must be between 5 and 60"
            )
        poll_raw = env.get("POC_POLL_SECONDS", "5")
        try:
            poll_seconds = float(poll_raw)
        except ValueError as exc:
            raise ConfigurationError("POC_POLL_SECONDS must be numeric") from exc
        if not 1 <= poll_seconds <= 30:
            raise ConfigurationError("POC_POLL_SECONDS must be between 1 and 30")
        request_paths = (
            require_outside_repository(
                "POC_REQUEST_16X9",
                require_file("POC_REQUEST_16X9", env.get("POC_REQUEST_16X9")),
            ),
            require_outside_repository(
                "POC_REQUEST_9X16",
                require_file("POC_REQUEST_9X16", env.get("POC_REQUEST_9X16")),
            ),
        )
        media_dir_raw = env.get("POC_MEDIA_DIR")
        if not media_dir_raw:
            raise ConfigurationError("POC_MEDIA_DIR is required")
        media_dir = Path(media_dir_raw).expanduser().resolve()
        if not media_dir.is_dir():
            raise ConfigurationError(
                "POC_MEDIA_DIR must be an existing private directory"
            )
        require_outside_repository("POC_MEDIA_DIR", media_dir)
        model_hash_path = require_outside_repository(
            "POC_MODEL_SHA256_JSON",
            require_file("POC_MODEL_SHA256_JSON", env.get("POC_MODEL_SHA256_JSON")),
        )
        model_hashes = json.loads(model_hash_path.read_text(encoding="utf-8"))
        if not isinstance(model_hashes, dict) or not model_hashes:
            raise ConfigurationError(
                "POC_MODEL_SHA256_JSON must contain a non-empty object"
            )
        if any(
            not isinstance(name, str)
            or not isinstance(digest_value, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest_value)
            for name, digest_value in model_hashes.items()
        ):
            raise ConfigurationError("POC_MODEL_SHA256_JSON contains an invalid digest")
        return cls(
            api_key=api_key,
            endpoint_id=endpoint_id,
            registry_image=image,
            image_digest=digest,
            cost_limit_usd=limit,
            cost_per_second_usd=rate,
            cancellation_reserve_seconds=reserve,
            poll_seconds=poll_seconds,
            request_paths=request_paths,
            media_dir=media_dir,
            model_hashes=model_hashes,
        )

    def execution_timeout_ms(self, remaining_cases: int, spent_usd: Decimal) -> int:
        remaining = self.cost_limit_usd - spent_usd
        if remaining <= 0:
            raise CostLimitError("POC cost limit exhausted")
        per_case = remaining / Decimal(remaining_cases)
        seconds = (
            int(
                (per_case / self.cost_per_second_usd).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            - self.cancellation_reserve_seconds
        )
        if seconds < MIN_EXECUTION_SECONDS:
            raise CostLimitError(
                "remaining cost budget cannot fund the minimum safe execution window"
            )
        return seconds * 1000


@dataclass(frozen=True, slots=True)
class CleanupConfig:
    api_key: str
    endpoint_id: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> CleanupConfig:
        api_key = env.get("RUNPOD_API_KEY")
        if not api_key:
            raise ConfigurationError("RUNPOD_API_KEY is required")
        return cls(
            api_key=api_key,
            endpoint_id=require_id("RUNPOD_ENDPOINT_ID", env.get("RUNPOD_ENDPOINT_ID")),
        )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise ProviderError("RunPod API redirect was rejected")


class ProviderClient(Protocol):
    def submit(self, request: dict[str, Any], execution_timeout_ms: int) -> str: ...

    def status(self, job_id: str) -> dict[str, Any]: ...

    def cancel(self, job_id: str) -> None: ...


class RunPodClient:
    def __init__(
        self, api_key: str, endpoint_id: str, *, timeout_seconds: int = 30
    ) -> None:
        self._api_key = api_key
        self._endpoint_id = require_id("RUNPOD_ENDPOINT_ID", endpoint_id)
        self._timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(NoRedirect())

    def _url(self, suffix: str) -> str:
        return f"{RUNPOD_API_PREFIX}/{self._endpoint_id}/{suffix}"

    def _request(
        self, method: str, suffix: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = (
            None
            if payload is None
            else json.dumps(payload, separators=(",", ":")).encode()
        )
        request = urllib.request.Request(
            self._url(suffix),
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_HTTP_BYTES + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError("RunPod API request failed") from exc
        if len(raw) > MAX_HTTP_BYTES:
            raise ProviderError("RunPod API response exceeded the safety limit")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("RunPod API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ProviderError("RunPod API returned an invalid object")
        return value

    def submit(self, request: dict[str, Any], execution_timeout_ms: int) -> str:
        result = self._request(
            "POST",
            "run",
            {"input": request, "policy": {"executionTimeout": execution_timeout_ms}},
        )
        return require_id("provider job id", result.get("id"))

    def status(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"status/{require_id('provider job id', job_id)}")

    def cancel(self, job_id: str) -> None:
        self._request("POST", f"cancel/{require_id('provider job id', job_id)}", {})


def validate_media(path: Path, aspect_ratio: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Check declarations and file size only; generation stays on the rented worker."""
    expected = (1280, 720) if aspect_ratio == "16:9" else (720, 1280)
    size_bytes = path.stat().st_size
    if not 0 < size_bytes <= MAX_MEDIA_BYTES:
        raise PocError("POC output is empty or exceeds the size limit")
    if (metadata.get("media_type") != "video/mp4"
            or metadata.get("codec") != "h264"
            or type(metadata.get("width")) is not int
            or type(metadata.get("height")) is not int
            or (metadata["width"], metadata["height"]) != expected):
        raise PocError("POC output violates the fixed 720p metadata policy")
    duration = metadata.get("duration_ms")
    fps = metadata.get("fps")
    if type(duration) is not int or not 4750 <= duration <= 5250:
        raise PocError("POC output duration is outside the five-second tolerance")
    if type(fps) not in {int, float} or not 0 < fps <= 240:
        raise PocError("POC output frame rate is invalid")
    return {
        "validation_method": "provider_metadata",
        "container": "mp4",
        "codec": metadata["codec"],
        "duration_seconds": duration / 1000,
        "width": metadata["width"],
        "height": metadata["height"],
        "fps": fps,
    }


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def require_private_evidence_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise ConfigurationError("POC evidence must remain outside the repository")
    return path


def new_evidence(config: PocConfig) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "issue": 14,
        "status": "RUNNING",
        "started_at": utc_now(),
        "finished_at": None,
        "authorization": {
            "cost_limit_usd": str(config.cost_limit_usd),
            "cost_rate_usd_per_second": str(config.cost_per_second_usd),
            "paid_poc_confirmed": True,
        },
        "supply_chain": {
            "registry_image": config.registry_image,
            "image_digest": config.image_digest,
            "worker_commit": worker_contract.WORKER_COMFYUI_COMMIT,
            "comfyui_commit": worker_contract.COMFYUI_COMMIT,
            "comfy_cli": worker_contract.COMFY_CLI_VERSION,
            "workflow_sha256": json.loads(
                (WORKFLOW_PATH / "manifest.json").read_text(encoding="utf-8")
            )["workflow_sha256"],
            "model_sha256": config.model_hashes,
        },
        "runs": [],
        "failures": [],
        "cost": {"estimated_usd": "0", "source": "CONFIGURED_UPPER_BOUND_RATE"},
        "cleanup": {
            "cancel_attempts": 0,
            "cancel_failures": 0,
            "active_jobs_remaining": 0,
        },
        "rollback": {"triggered": False, "status": "NOT_REQUIRED", "actions": []},
        "decision": None,
    }


def load_request(path: Path, expected_aspect: str) -> dict[str, Any]:
    request = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ConfigurationError("POC request fixture must contain an object")
    worker_contract.validate_request(request)
    if request["aspect_ratio"] != expected_aspect:
        raise ConfigurationError(
            "POC request aspect ratio does not match its assigned case"
        )
    checked_fixtures = (
        ROOT / "workers" / "runpod-comfyui" / "contract" / "fixtures"
    ).resolve()
    if path.resolve().is_relative_to(checked_fixtures):
        raise ConfigurationError(
            "checked-in synthetic claims cannot be used for a paid POC"
        )
    return request


def run_paid_poc(
    config: PocConfig,
    evidence_path: Path,
    *,
    client: ProviderClient | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    media_validator: Callable[[Path, str, dict[str, Any]], dict[str, Any]] = validate_media,
) -> dict[str, Any]:
    provider = client or RunPodClient(config.api_key, config.endpoint_id)
    evidence = new_evidence(config)
    active_jobs: set[str] = set()
    spent = Decimal("0")
    cases = (("16:9", config.request_paths[0]), ("9:16", config.request_paths[1]))
    atomic_write_json(evidence_path, evidence)
    try:
        for index, (aspect, request_path) in enumerate(cases):
            request = load_request(request_path, aspect)
            timeout_ms = config.execution_timeout_ms(len(cases) - index, spent)
            run: dict[str, Any] = {
                "attempt_id": request["attempt_id"],
                "aspect_ratio": aspect,
                "execution_timeout_ms": timeout_ms,
                "provider_job_id": None,
                "status": "SUBMITTING",
                "gpu": None,
                "queue_ms": None,
                "cold_start_ms": None,
                "runtime_ms": None,
                "billable_ms": None,
                "estimated_cost_usd": None,
                "media_validation": None,
            }
            evidence["runs"].append(run)
            atomic_write_json(evidence_path, evidence)
            job_id = provider.submit(request, timeout_ms)
            active_jobs.add(job_id)
            run["provider_job_id"] = job_id
            run["status"] = "SUBMITTED"
            started = clock()
            while True:
                status_result = provider.status(job_id)
                status = status_result.get("status")
                if status not in TERMINAL_STATUSES | {"IN_QUEUE", "IN_PROGRESS"}:
                    raise ProviderError("RunPod returned an unknown status")
                run["status"] = status
                elapsed_ms = int((clock() - started) * 1000)
                if status in TERMINAL_STATUSES:
                    break
                if elapsed_ms >= timeout_ms:
                    raise CostLimitError(
                        "provider job exceeded its cost-bounded execution window"
                    )
                sleep(config.poll_seconds)
            active_jobs.discard(job_id)
            billable_ms = int(status_result.get("executionTime") or elapsed_ms)
            queue_ms = int(status_result.get("delayTime") or 0)
            cost = (Decimal(billable_ms) / Decimal(1000)) * config.cost_per_second_usd
            spent += cost
            run.update(
                {
                    "queue_ms": queue_ms,
                    "runtime_ms": int(status_result.get("executionTime") or elapsed_ms),
                    "billable_ms": billable_ms,
                    "estimated_cost_usd": str(cost),
                }
            )
            evidence["cost"]["estimated_usd"] = str(spent)
            if spent > config.cost_limit_usd:
                raise CostLimitError("provider timing exceeded the approved cost limit")
            if status != "COMPLETED":
                raise ProviderError(f"RunPod job ended with {status}")
            output = status_result.get("output")
            if not isinstance(output, dict):
                raise ProviderError(
                    "RunPod completion did not contain a Worker Contract response"
                )
            workflow_sha256 = evidence["supply_chain"]["workflow_sha256"]
            worker_contract.validate_response(output, workflow_sha256)
            run["gpu"] = output["metrics"]["gpu"]
            run["cold_start_ms"] = output["metrics"]["cold_start_ms"]
            media_candidate = config.media_dir / f"{request['attempt_id']}.mp4"
            if not media_candidate.is_file() or media_candidate.is_symlink():
                raise PocError(
                    "claim collector did not materialize the expected private MP4"
                )
            media_path = media_candidate.resolve()
            if media_path.parent != config.media_dir:
                raise PocError(
                    "claim collector output escaped the private media directory"
                )
            run["media_validation"] = media_validator(media_path, aspect, output["output"])
            run["status"] = "VALIDATED"
            atomic_write_json(evidence_path, evidence)
        evidence["status"] = "SUCCEEDED"
        evidence["decision"] = "PENDING_HUMAN_REVIEW"
    except Exception as exc:
        evidence["status"] = "FAILED"
        evidence["rollback"]["triggered"] = True
        evidence["rollback"]["status"] = "RUNNING"
        evidence["failures"].append({"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        for job_id in sorted(active_jobs):
            evidence["cleanup"]["cancel_attempts"] += 1
            try:
                provider.cancel(job_id)
                evidence["rollback"]["actions"].append(
                    {"action": "CANCEL_PROVIDER_JOB", "result": "SUCCEEDED"}
                )
            except Exception:
                evidence["cleanup"]["cancel_failures"] += 1
                evidence["rollback"]["actions"].append(
                    {"action": "CANCEL_PROVIDER_JOB", "result": "FAILED"}
                )
        evidence["cleanup"]["active_jobs_remaining"] = evidence["cleanup"][
            "cancel_failures"
        ]
        if evidence["rollback"]["triggered"]:
            evidence["rollback"]["status"] = (
                "COMPLETED"
                if evidence["cleanup"]["active_jobs_remaining"] == 0
                else "INCOMPLETE"
            )
        evidence["finished_at"] = utc_now()
        atomic_write_json(evidence_path, evidence)
    return evidence


def cleanup_from_evidence(
    config: PocConfig | CleanupConfig,
    evidence_path: Path,
    client: ProviderClient | None = None,
) -> int:
    provider = client or RunPodClient(config.api_key, config.endpoint_id)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    cancelled = 0
    failures = 0
    evidence.setdefault(
        "rollback", {"triggered": True, "status": "RUNNING", "actions": []}
    )
    evidence["rollback"]["triggered"] = True
    evidence["rollback"]["status"] = "RUNNING"
    for run in evidence.get("runs", []):
        job_id = run.get("provider_job_id")
        if job_id and run.get("status") not in TERMINAL_STATUSES | {"VALIDATED"}:
            evidence["cleanup"]["cancel_attempts"] += 1
            try:
                provider.cancel(require_id("provider job id", job_id))
                cancelled += 1
                run["status"] = "CANCELLED_BY_CLEANUP"
                evidence["rollback"]["actions"].append(
                    {"action": "CANCEL_PROVIDER_JOB", "result": "SUCCEEDED"}
                )
            except Exception:
                failures += 1
                evidence["cleanup"]["cancel_failures"] += 1
                evidence["rollback"]["actions"].append(
                    {"action": "CANCEL_PROVIDER_JOB", "result": "FAILED"}
                )
    evidence["cleanup"]["active_jobs_remaining"] = failures
    evidence["rollback"]["status"] = "COMPLETED" if failures == 0 else "INCOMPLETE"
    evidence["finished_at"] = utc_now()
    atomic_write_json(evidence_path, evidence)
    if failures:
        raise ProviderError("one or more recorded provider jobs could not be cancelled")
    return cancelled


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Issue #14 fail-closed RunPod POC harness"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "preflight", help="report credential and authorization presence only"
    )
    execute = subparsers.add_parser(
        "execute", help="run both paid five-second 720p cases"
    )
    execute.add_argument("--confirm-paid-poc", required=True)
    execute.add_argument("--evidence", type=Path, required=True)
    cleanup = subparsers.add_parser(
        "cleanup", help="cancel unfinished jobs recorded in evidence"
    )
    cleanup.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "preflight":
        print(json.dumps(environment_presence(os.environ), sort_keys=True))
        return 0
    try:
        if args.command == "execute":
            config = PocConfig.from_env(os.environ)
            if args.confirm_paid_poc != CONFIRMATION:
                raise ConfigurationError("paid POC confirmation is invalid")
            run_paid_poc(config, require_private_evidence_path(args.evidence))
            return 0
        cleanup_config = CleanupConfig.from_env(os.environ)
        cancelled = cleanup_from_evidence(
            cleanup_config, require_private_evidence_path(args.evidence)
        )
        print(json.dumps({"cancelled_jobs": cancelled}))
        return 0
    except PocError as exc:
        print(f"POC blocked or failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
