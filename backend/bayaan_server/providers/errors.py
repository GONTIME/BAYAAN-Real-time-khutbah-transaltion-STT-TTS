"""Typed provider failure events (BAYAAN_MASTER_SPEC.md §87, §54).

Business logic must never depend on adapter internals; these two exceptions are
the ONLY provider failure signals it may catch:

- ``ProviderOutageError``       — one provider call failed (network, timeout,
                                  HTTP 5xx, simulated outage). Internal to the
                                  failover layer.
- ``ProviderUnavailableError``  — every provider in a chain failed (§87). The
                                  caller treats this as SAFE_UNAVAILABLE and
                                  falls back (§57 degraded, §92/§134 public
                                  fallback) instead of failing the service.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for provider-layer failures."""


class ProviderOutageError(ProviderError):
    """A single provider call failed. Carries the provider name for alerts."""

    def __init__(self, provider: str, message: str = "provider call failed") -> None:
        self.provider = provider
        super().__init__(f"{provider}: {message}")


class ProviderUnavailableError(ProviderError):
    """Every provider in a chain failed (§87 SAFE_UNAVAILABLE)."""

    def __init__(
        self, capability: str, last_error: Exception | None = None
    ) -> None:
        self.capability = capability
        self.last_error = last_error
        tail = f" (last error: {last_error})" if last_error else ""
        super().__init__(f"no provider available for '{capability}'{tail}")