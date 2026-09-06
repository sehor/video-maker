from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from app.provider import VideoProvider


class ProviderNotConfiguredError(LookupError):
    pass


class ProviderRegistry:
    """Immutable allowlist of configured Provider adapters."""

    def __init__(self, providers: Mapping[str, VideoProvider]) -> None:
        if not providers:
            raise ValueError("provider registry must not be empty")
        for code in providers:
            if not code or not code.replace("-", "").replace("_", "").isalnum():
                raise ValueError(f"invalid provider code: {code!r}")
        self._providers = MappingProxyType(dict(providers))

    @property
    def codes(self) -> frozenset[str]:
        return frozenset(self._providers)

    def get(self, provider_code: str) -> VideoProvider:
        try:
            return self._providers[provider_code]
        except KeyError as exc:
            raise ProviderNotConfiguredError(
                f"provider is not configured: {provider_code}"
            ) from exc
