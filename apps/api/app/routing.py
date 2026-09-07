from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from types import MappingProxyType

from app.config import get_settings
from app.provider import MockVideoProvider
from app.provider_registry import ProviderRegistry
from app.simulators import DeterministicRunPodSimulator

MOCK_ROUTE_VERSION = "mock_video_v1"
SIMULATED_RUNPOD_ROUTE_VERSION = "runpod_simulated_v1"
FIXED_WORKFLOW_ID = "fast_wan_i2v_720_v1"
MAX_PROVIDER_OUTPUT_BYTES = 512 * 1024 * 1024


class RouteUnavailableError(LookupError):
    pass


class RouteDisabledError(RouteUnavailableError):
    pass


class RouteInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RouteVersion:
    """An immutable routing decision; enablement lives outside the version."""

    key: str
    family: str
    version: int
    provider_code: str
    workflow_id: str
    resolutions: frozenset[str]
    durations_ms: frozenset[int]
    aspect_ratios: frozenset[str]
    requires_input_claim: bool

    @property
    def candidate_id(self) -> uuid.UUID:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"video-maker:route:{self.key}")

    @property
    def provider_endpoint_id(self) -> uuid.UUID:
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"video-maker:provider-endpoint:{self.provider_code}:{self.key}",
        )

    def require_supported(
        self,
        *,
        resolution: str,
        duration_ms: int,
        aspect_ratio: str,
        has_input: bool,
    ) -> None:
        if resolution not in self.resolutions:
            raise RouteInputError("route does not support the requested resolution")
        if duration_ms not in self.durations_ms:
            raise RouteInputError("route does not support the requested duration")
        if aspect_ratio not in self.aspect_ratios:
            raise RouteInputError("route does not support the requested aspect ratio")
        if self.requires_input_claim and not has_input:
            raise RouteInputError("route requires one system-owned reference asset")


MOCK_ROUTE = RouteVersion(
    key=MOCK_ROUTE_VERSION,
    family="mock_video",
    version=1,
    provider_code="mock",
    workflow_id="mock:v1",
    resolutions=frozenset({"720P"}),
    durations_ms=frozenset(range(1_000, 10_001, 1_000)),
    aspect_ratios=frozenset({"16:9", "9:16"}),
    requires_input_claim=False,
)

SIMULATED_RUNPOD_ROUTE = RouteVersion(
    key=SIMULATED_RUNPOD_ROUTE_VERSION,
    family="fast_wan_i2v_720",
    version=1,
    provider_code="runpod-simulator",
    workflow_id=FIXED_WORKFLOW_ID,
    resolutions=frozenset({"720P"}),
    durations_ms=frozenset({5_000}),
    aspect_ratios=frozenset({"16:9", "9:16"}),
    requires_input_claim=True,
)


class RouteRegistry:
    """Controlled route catalog plus an immediate, per-route kill switch."""

    def __init__(
        self,
        routes: Iterable[RouteVersion],
        *,
        active_key: str,
        enabled: bool,
    ) -> None:
        route_values = tuple(routes)
        indexed = {route.key: route for route in route_values}
        if len(indexed) == 0:
            raise ValueError("route registry must not be empty")
        if len(indexed) != len(route_values):
            raise ValueError("route version keys must be unique")
        if active_key not in indexed:
            raise ValueError("active route version is not registered")
        self._routes = MappingProxyType(indexed)
        self._enabled = {key: enabled for key in indexed}
        self._active_key = active_key
        self._lock = threading.RLock()

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self._routes)

    @property
    def active_key(self) -> str:
        with self._lock:
            return self._active_key

    def activate(self, route_key: str) -> None:
        if route_key not in self._routes:
            raise RouteUnavailableError(f"route version is not registered: {route_key}")
        with self._lock:
            self._active_key = route_key

    def set_enabled(self, route_key: str, enabled: bool) -> None:
        if route_key not in self._routes:
            raise RouteUnavailableError(f"route version is not registered: {route_key}")
        with self._lock:
            self._enabled[route_key] = enabled

    def get(self, route_key: str) -> RouteVersion:
        try:
            return self._routes[route_key]
        except KeyError as exc:
            raise RouteUnavailableError(
                f"route version is not registered: {route_key}"
            ) from exc

    def active(self) -> RouteVersion:
        with self._lock:
            route_key = self._active_key
            if not self._enabled[route_key]:
                raise RouteDisabledError(f"route is disabled: {route_key}")
            return self._routes[route_key]

    def enabled_route(self, route_key: str) -> RouteVersion:
        with self._lock:
            route = self.get(route_key)
            if not self._enabled[route_key]:
                raise RouteDisabledError(f"route is disabled: {route_key}")
            return route

    def by_candidate_id(self, candidate_id: uuid.UUID) -> RouteVersion:
        for route in self._routes.values():
            if route.candidate_id == candidate_id:
                return route
        raise RouteUnavailableError(f"route candidate is not registered: {candidate_id}")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if _b64encode(decoded) != value:
        raise ValueError("non-canonical callback claim")
    return decoded


class CallbackClaimIssuer:
    """Issues short-lived claims bound to one job, attempt, and route version."""

    def __init__(
        self,
        secret: bytes,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if len(secret) < 32:
            raise ValueError("callback claim secret must contain at least 32 bytes")
        self._secret = secret
        self._clock = clock

    def issue(
        self,
        *,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        route: RouteVersion,
        expires_in: timedelta,
    ) -> str:
        if expires_in <= timedelta(0) or expires_in > timedelta(minutes=15):
            raise ValueError("callback claim TTL must be between 1 second and 15 minutes")
        payload = {
            "v": 1,
            "job_id": str(job_id),
            "attempt_id": str(attempt_id),
            "route": route.key,
            "provider": route.provider_code,
            "exp": int((self._clock() + expires_in).timestamp()),
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return f"{_b64encode(body)}.{_b64encode(signature)}"

    def verify(
        self,
        token: str,
        *,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        route: RouteVersion,
    ) -> None:
        try:
            body_value, signature_value = token.split(".", 1)
            body = _b64decode(body_value)
            signature = _b64decode(signature_value)
            expected = hmac.new(self._secret, body, hashlib.sha256).digest()
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("callback claim payload must be an object")
        except (ValueError, binascii.Error, json.JSONDecodeError) as exc:
            raise ValueError("callback claim is invalid") from exc
        if not hmac.compare_digest(signature, expected):
            raise ValueError("callback claim is invalid")
        expected_fields = {"v", "job_id", "attempt_id", "route", "provider", "exp"}
        if set(payload) != expected_fields:
            raise ValueError("callback claim is invalid")
        if (
            payload["v"] != 1
            or payload["job_id"] != str(job_id)
            or payload["attempt_id"] != str(attempt_id)
            or payload["route"] != route.key
            or payload["provider"] != route.provider_code
            or not isinstance(payload["exp"], int)
        ):
            raise ValueError("callback claim is not bound to this submission")
        if payload["exp"] <= int(self._clock().timestamp()):
            raise ValueError("callback claim has expired")


@lru_cache
def get_provider_registry() -> ProviderRegistry:
    settings = get_settings()
    providers = {
        "mock": MockVideoProvider(
            webhook_secret=settings.mock_provider_webhook_secret
        )
    }
    if settings.runpod_simulator_enabled:
        providers["runpod-simulator"] = DeterministicRunPodSimulator(
            clock=lambda: datetime.now(UTC)
        )
    return ProviderRegistry(providers)


@lru_cache
def get_route_registry() -> RouteRegistry:
    settings = get_settings()
    routes = [MOCK_ROUTE]
    if settings.runpod_simulator_enabled:
        routes.append(SIMULATED_RUNPOD_ROUTE)
    registry = RouteRegistry(
        routes,
        active_key=settings.generation_route_version,
        enabled=settings.generation_route_enabled,
    )
    missing = {route.provider_code for route in routes} - get_provider_registry().codes
    if missing:
        raise ValueError(f"routes reference unconfigured providers: {sorted(missing)}")
    return registry


@lru_cache
def get_callback_claim_issuer() -> CallbackClaimIssuer:
    settings = get_settings()
    return CallbackClaimIssuer(
        settings.provider_callback_claim_secret.get_secret_value().encode()
    )
