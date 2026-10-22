"""§87 provider failover + §54 circuit-breaker orchestration.

Implements the §87 pseudocode: the primary provider gets the full time budget,
each fallback gets a short one; a failing provider is marked unhealthy in its
circuit breaker, a successful fallback emits PROVIDER_SWITCHED, and only when
every provider in the chain is exhausted does the runner raise
``ProviderUnavailableError`` (SAFE_UNAVAILABLE). Circuits are per provider
(§54) so a broken primary is never hammered across requests.

Business logic depends only on the provider protocols (§183/§186); concrete
providers are injected by the registry (``providers/mock.build_providers``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from typing import Any, Callable

from bayaan_server.config import Settings
from bayaan_server.domain.health import CircuitBreaker, CircuitState, NowMs
from bayaan_server.logging_setup import get_logger
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.ops.alerts import AlertBus
from bayaan_server.providers.errors import ProviderUnavailableError
from bayaan_server.providers.interfaces import ASRProvider, TTSProvider, TranslationProvider

logger = get_logger(__name__)

STATE_GAUGE = {
    CircuitState.CLOSED: 0.0,
    CircuitState.HALF_OPEN: 1.0,
    CircuitState.OPEN: 2.0,
}


def _metric_safe(name: str) -> str:
    """Prometheus metric names only allow [A-Za-z0-9_]; a route named
    'mock-failing' must not break the /metrics exposition."""
    return re.sub(r"[^0-9a-zA-Z_]", "_", name)


@dataclasses.dataclass
class ProviderRoute:
    """One provider in a chain, guarded by its own §54 circuit breaker."""

    name: str
    provider: Any
    breaker: CircuitBreaker


class FailoverRunner:
    """Runs one capability's calls across its provider chain (§87)."""

    def __init__(
        self,
        capability: str,
        routes: list[ProviderRoute],
        settings: Settings,
        alerts: AlertBus,
        metrics: MetricsRegistry,
    ) -> None:
        self._capability = capability
        self._routes = routes
        self._settings = settings
        self._alerts = alerts
        self._metrics = metrics

    @property
    def routes(self) -> list[ProviderRoute]:
        return self._routes

    def reset(self) -> None:
        """§111 soft-reboot: close every circuit so the next request probes."""
        for route in self._routes:
            route.breaker.reset()

    def snapshot(self) -> list[dict]:
        return [
            {"provider": route.name, "state": route.breaker.state.value}
            for route in self._routes
        ]

    async def call(self, invoke: Callable[[Any], Any], timeout_ms: int | None = None) -> Any:
        """§87: primary (full budget) then fallbacks (short budget) in order.

        ``invoke(provider)`` must return an awaitable. The first route whose
        circuit admits the attempt and whose call succeeds wins; otherwise
        SAFE_UNAVAILABLE (``ProviderUnavailableError``) is raised.

        ``timeout_ms`` overrides the settings budget for this call (§185: slow
        adapters such as a local LLM declare their own timeout in provider
        settings; the runner must not cut them off with the fast MT default).
        When omitted, the §87 primary/short budgets apply as before.
        """
        last_error: Exception | None = None
        attempt = 0
        for route in self._routes:
            if not route.breaker.begin_attempt():
                continue  # OPEN (in cool-down) or probe already in flight
            if timeout_ms is not None:
                budget_ms = timeout_ms
            else:
                budget_ms = (
                    self._settings.failover.request_timeout_ms
                    if attempt == 0
                    else self._settings.failover.fallback_timeout_ms
                )
            # §87/§185: a slow-by-design adapter declares its own floor
            # (``min_timeout_ms``). The failover budget must never cut a call
            # off BEFORE the adapter's own timeout - a local LLM loading its
            # weights on the first call would otherwise be reported as an
            # outage and the chain would fail over for no reason. The floor
            # only ever EXTENDS the budget, never shortens it.
            floor_ms = getattr(route.provider, "min_timeout_ms", None)
            if floor_ms:
                budget_ms = max(budget_ms, int(floor_ms))
            attempt += 1
            try:
                result = await asyncio.wait_for(
                    invoke(route.provider), budget_ms / 1000.0
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider errors are arbitrary
                last_error = exc
                route.breaker.record_failure()
                safe_name = _metric_safe(route.name)
                self._metrics.incr(
                    f"bayaan_provider_failure_{self._capability}_{safe_name}_total"
                )
                self._metrics.incr("bayaan_provider_failures_total")
                state = route.breaker.state
                self._metrics.set_gauge(
                    f"bayaan_provider_state_{self._capability}_{safe_name}",
                    STATE_GAUGE[state],
                )
                self._alerts.emit(
                    "provider_failure",
                    "warning",
                    f"{self._capability} provider '{route.name}' call failed",
                    capability=self._capability,
                    provider=route.name,
                    error=str(exc)[:300],
                )
                if state is CircuitState.OPEN:
                    self._metrics.incr("bayaan_circuit_open_total")
                    self._alerts.emit(
                        "circuit_open",
                        "warning",
                        f"{self._capability} circuit for '{route.name}' is OPEN (§54)",
                        capability=self._capability,
                        provider=route.name,
                    )
                continue

            # Success - the provider answered.
            was_broken = route.breaker.state is not CircuitState.CLOSED
            route.breaker.record_success()
            safe_name = _metric_safe(route.name)
            self._metrics.incr(
                f"bayaan_provider_success_{self._capability}_{safe_name}_total"
            )
            self._metrics.set_gauge(
                f"bayaan_provider_state_{self._capability}_{safe_name}", 0.0
            )
            if attempt > 1:
                self._metrics.incr("bayaan_provider_switch_total")
                self._alerts.emit(
                    "provider_switched",
                    "warning",
                    f"{self._capability} served by fallback '{route.name}' (§87 PROVIDER_SWITCHED)",
                    capability=self._capability,
                    provider=route.name,
                )
            if was_broken:
                self._alerts.emit(
                    "provider_recovered",
                    "info",
                    f"{self._capability} provider '{route.name}' recovered (CLOSED)",
                    capability=self._capability,
                    provider=route.name,
                )
            return result

        # §87: SAFE_UNAVAILABLE - nothing in the chain could serve the call.
        self._metrics.incr("bayaan_provider_unavailable_total")
        self._alerts.emit(
            "provider_unavailable",
            "error",
            f"{self._capability} unavailable: every provider in the chain failed (§87)",
            capability=self._capability,
            last_error=str(last_error)[:300] if last_error else None,
        )
        raise ProviderUnavailableError(self._capability, last_error)


class FailoverASRProvider:
    """§87-protected ASR provider (same port as any ASR adapter)."""

    def __init__(self, runner: FailoverRunner) -> None:
        self._runner = runner

    async def transcribe(self, audio: bytes, source_language: str):
        return await self._runner.call(
            lambda provider: provider.transcribe(audio, source_language)
        )


class FailoverTranslationProvider:
    """§87-protected translation provider (same port as any MT adapter)."""

    def __init__(self, runner: FailoverRunner) -> None:
        self._runner = runner

    async def translate(self, text: str, source_language: str, target_language: str):
        return await self._runner.call(
            lambda provider: provider.translate(text, source_language, target_language)
        )


class FailoverTTSProvider:
    """§87-protected TTS provider (same port as any TTS adapter)."""

    def __init__(self, runner: FailoverRunner) -> None:
        self._runner = runner

    async def synthesize(self, text: str, target_language: str):
        return await self._runner.call(
            lambda provider: provider.synthesize(text, target_language)
        )


def build_failover_runners(
    settings: Settings,
    alerts: AlertBus,
    metrics: MetricsRegistry,
    factories: dict[str, dict[str, Callable[[Settings], Any]]],
    now_ms: NowMs | None = None,
) -> dict[str, FailoverRunner]:
    """Build one failover runner per capability from the provider registry.

    ``factories`` maps capability -> provider name -> factory(settings) and is
    supplied by ``providers/mock.build_providers`` (no coupling here). The
    configured chain (primary + fallback, §188) selects the factories; an
    empty/'disabled' entry selects no provider.
    """
    cfg = settings.providers
    chains = {
        "asr": (cfg.asr, cfg.asr_fallback),
        "translation": (cfg.translation, cfg.translation_fallback),
        "tts": (cfg.tts, cfg.tts_fallback),
    }
    runners: dict[str, FailoverRunner] = {}
    for capability, (primary, fallback) in chains.items():
        names = [name for name in (primary, fallback) if name and name != "disabled"]
        routes: list[ProviderRoute] = []
        for name in names:
            factory = factories.get(capability, {}).get(name)
            if factory is None:
                raise ValueError(f"unknown {capability} provider '{name}'")
            routes.append(
                ProviderRoute(
                    name=name,
                    provider=factory(settings),
                    breaker=CircuitBreaker(
                        settings.failover.failure_threshold,
                        settings.failover.cooldown_ms,
                        now_ms=now_ms,
                    ),
                )
            )
        runners[capability] = FailoverRunner(capability, routes, settings, alerts, metrics)
    return runners


@dataclasses.dataclass
class ProviderSet:
    """§63 provider registry: the ports the app depends on + the runners it reports."""

    asr: ASRProvider
    translation: TranslationProvider
    tts: TTSProvider
    runners: dict[str, FailoverRunner]