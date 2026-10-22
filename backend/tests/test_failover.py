"""Phase 6 failover tests (§87 fallback chain, §54 circuit breaker in the runner).

Uses the real ``FailoverRunner`` + stub providers so the failover state machine
is exercised exactly as the app runs it - without any network.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from bayaan_server.config import FailoverSettings, Settings
from bayaan_server.domain.health import CircuitBreaker, CircuitState
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.ops.alerts import AlertBus
from bayaan_server.providers.errors import ProviderOutageError, ProviderUnavailableError
from bayaan_server.providers.failover import (
    FailoverASRProvider,
    FailoverRunner,
    ProviderRoute,
)
from bayaan_server.providers.interfaces import AsrResult


class HealthyASR:
    """Always succeeds; counts calls for "fallback must not be touched"."""

    def __init__(self, text: str = "healthy") -> None:
        self.text = text
        self.calls = 0

    async def transcribe(self, audio: bytes, source_language: str) -> AsrResult:
        self.calls += 1
        return AsrResult(text=self.text, language=source_language, confidence=0.9)


class FailingASR:
    """Always fails with a typed ProviderOutageError (§87 simulated outage)."""

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, audio: bytes, source_language: str) -> AsrResult:
        self.calls += 1
        raise ProviderOutageError("failing", "simulated outage")


class FlappingASR:
    """Fails ``failures_left`` times then succeeds - drives half-open recovery."""

    def __init__(self, failures_left: int) -> None:
        self.failures_left = failures_left
        self.calls = 0

    async def transcribe(self, audio: bytes, source_language: str) -> AsrResult:
        self.calls += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            raise ProviderOutageError("flapping", "simulated outage")
        return AsrResult(text="recovered", language=source_language, confidence=0.9)


class SlowASR:
    """Sleeps far beyond any test budget so wait_for must cut it off."""

    async def transcribe(self, audio: bytes, source_language: str) -> AsrResult:
        await asyncio.sleep(10.0)
        return AsrResult(text="slow", language=source_language, confidence=0.9)


def make_runner(
    settings: Settings,
    providers: list,
    metrics: MetricsRegistry | None = None,
    alerts: AlertBus | None = None,
    thresholds: int = 1,
    cooldown_ms: int = 60_000,
) -> FailoverRunner:
    """Build an ``asr`` runner over the given provider objects."""
    alerts = alerts or AlertBus(settings.alerts)
    metrics = metrics or MetricsRegistry()
    routes = [
        ProviderRoute(f"p{index}", provider, CircuitBreaker(thresholds, cooldown_ms))
        for index, provider in enumerate(providers)
    ]
    return FailoverRunner("asr", routes, settings, alerts, metrics)


def test_primary_success_does_not_touch_fallback() -> None:
    settings = Settings()
    metrics = MetricsRegistry()
    primary, fallback = HealthyASR("a"), HealthyASR("b")
    runner = make_runner(settings, [primary, fallback], metrics=metrics)
    result = asyncio.run(FailoverASRProvider(runner).transcribe(b"\x00", "ar"))

    assert result.text == "a"
    assert primary.calls == 1
    assert fallback.calls == 0
    assert metrics.snapshot()["counters"].get("bayaan_provider_switch_total", 0) == 0


def test_primary_failure_fails_over_to_fallback() -> None:
    """§87: primary down -> fallback serves the request -> PROVIDER_SWITCHED."""
    settings = Settings()
    metrics = MetricsRegistry()
    alerts = AlertBus(settings.alerts)
    emitted: list[str] = []
    alerts.add_handler(lambda code, sev, msg, data: emitted.append(code))

    primary, fallback = FailingASR(), HealthyASR()
    runner = make_runner(settings, [primary, fallback], metrics=metrics, alerts=alerts)
    result = asyncio.run(FailoverASRProvider(runner).transcribe(b"\x00", "ar"))

    assert result.text == "healthy"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert "provider_switched" in emitted
    assert runner.routes[0].breaker.state is CircuitState.OPEN
    counter = metrics.snapshot()["counters"]
    assert counter.get("bayaan_provider_switch_total", 0) == 1
    assert counter.get("bayaan_provider_failure_asr_p0_total", 0) == 1


def test_all_providers_down_raises_safe_unavailable_no_hammering() -> None:
    """§87: whole chain failed -> SAFE_UNAVAILABLE; OPEN circuits mean the next
    request does not re-hammer the dead chain (§54 no unlimited retries)."""
    settings = Settings()
    metrics = MetricsRegistry()
    a, b = FailingASR(), FailingASR()
    runner = make_runner(settings, [a, b], metrics=metrics)
    wrapper = FailoverASRProvider(runner)

    with pytest.raises(ProviderUnavailableError) as exc:
        asyncio.run(wrapper.transcribe(b"\x00", "ar"))
    assert exc.value.capability == "asr"

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(wrapper.transcribe(b"\x00", "ar"))
    assert a.calls == 1 and b.calls == 1  # second call tried nothing
    counter = metrics.snapshot()["counters"]
    assert counter.get("bayaan_provider_unavailable_total", 0) == 2


def test_circuit_opens_and_blocks_until_cooldown() -> None:
    """§54: a hammered provider is skipped while OPEN, even with a working
    fallback, until the cool-down admits a HALF_OPEN probe."""
    settings = Settings()
    metrics = MetricsRegistry()
    failing, healthy = FailingASR(), HealthyASR()
    runner = make_runner(settings, [failing, healthy], metrics=metrics, thresholds=2)

    wrapper = FailoverASRProvider(runner)
    assert asyncio.run(wrapper.transcribe(b"\x00", "ar")).text == "healthy"
    assert asyncio.run(wrapper.transcribe(b"\x00", "ar")).text == "healthy"
    # Two failures -> circuit OPEN -> the dead primary is no longer probed.
    assert failing.calls == 2
    assert runner.routes[0].breaker.state is CircuitState.OPEN
    assert asyncio.run(wrapper.transcribe(b"\x00", "ar")).text == "healthy"
    assert failing.calls == 2  # skipped, not hammered
    assert metrics.snapshot()["counters"].get("bayaan_circuit_open_total", 0) == 1


def test_half_open_probe_recovers_the_primary() -> None:
    """§54: after the cool-down the HALF_OPEN probe hits a recovered provider
    and the circuit closes again."""
    from bayaan_server.domain.health import default_clock

    class Clock:
        def __init__(self) -> None:
            self.now = default_clock()

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    settings = Settings()
    flaky = FlappingASR(failures_left=1)
    metrics = MetricsRegistry()
    breaker = CircuitBreaker(1, 1000, now_ms=clock)
    runner = FailoverRunner(
        "asr",
        [ProviderRoute("flaky", flaky, breaker)],
        settings,
        AlertBus(settings.alerts),
        metrics,
    )
    wrapper = FailoverASRProvider(runner)
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(wrapper.transcribe(b"\x00", "ar"))  # opens the circuit

    clock.now += 1001  # cool-down elapsed -> HALF_OPEN probe -> recovered
    result = asyncio.run(wrapper.transcribe(b"\x00", "ar"))
    assert result.text == "recovered"
    assert breaker.state is CircuitState.CLOSED
    assert flaky.calls == 2


def test_timeouts_apply_per_attempt_budget() -> None:
    """§87 budgets: primary gets the full budget, fallbacks the short one; if
    both exceed it, SAFE_UNAVAILABLE - the call never hangs (bounded time)."""
    settings = Settings(
        failover=FailoverSettings(request_timeout_ms=250, fallback_timeout_ms=100)
    )
    metrics = MetricsRegistry()
    runner = make_runner(settings, [SlowASR(), SlowASR()], metrics=metrics)

    start = time.monotonic()
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(FailoverASRProvider(runner).transcribe(b"\x00", "ar"))
    elapsed_ms = (time.monotonic() - start) * 1000
    # ~250ms of budget vs the 20s both providers would sleep.
    assert elapsed_ms < 3000
    assert metrics.snapshot()["counters"].get("bayaan_provider_failures_total", 0) >= 2


def test_runner_snapshot_reports_chain_state() -> None:
    settings = Settings()
    runner = make_runner(settings, [HealthyASR()])
    assert runner.snapshot() == [{"provider": "p0", "state": "closed"}]

    runner.reset()
    assert runner.routes[0].breaker.state is CircuitState.CLOSED