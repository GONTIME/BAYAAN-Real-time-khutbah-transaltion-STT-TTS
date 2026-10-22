"""Phase 6 ops tests: §195 alerts (severity-gated, pluggable sink) and §111
watchdog (heartbeat staleness -> degrade + soft provider-layer reboot)."""

from __future__ import annotations

from bayaan_server.config import AlertsSettings, FailoverSettings, Settings
from bayaan_server.domain.health import CircuitState
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.ops.alerts import AlertBus
from bayaan_server.ops.health import ServiceHealth
from bayaan_server.ops.watchdog import WatchdogService


class RecordingRunner:
    """Duck-typed runner: records reset() calls and exposes snapshot()."""

    def __init__(self) -> None:
        self.resets = 0
        self.state = CircuitState.OPEN

    def reset(self) -> None:
        self.resets += 1
        self.state = CircuitState.CLOSED

    def snapshot(self) -> list[dict]:
        return [{"provider": "mock", "state": self.state.value}]


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# §195 AlertBus
# ---------------------------------------------------------------------------


def test_alert_bus_dispatches_to_handlers() -> None:
    settings = Settings(alerts=AlertsSettings(enabled=True, min_severity="info"))
    bus = AlertBus(settings.alerts, MetricsRegistry())
    seen: list[tuple] = []
    bus.add_handler(lambda code, sev, msg, data: seen.append((code, sev, msg, data)))
    bus.emit("provider_failure", "warning", "boom", capability="asr")

    assert len(seen) == 1
    code, sev, msg, data = seen[0]
    assert code == "provider_failure"
    assert sev == "warning"
    assert data["capability"] == "asr"


def test_alert_bus_applies_severity_floor() -> None:
    # min_severity=warning -> "info" alerts dropped, "warning" flow through.
    settings = Settings(alerts=AlertsSettings(enabled=True, min_severity="warning"))
    bus = AlertBus(settings.alerts)
    seen: list[str] = []
    bus.add_handler(lambda code, sev, msg, data: seen.append(f"{sev}:{code}"))

    bus.emit("provider_recovered", "info", "fine")
    bus.emit("circuit_open", "warning", "boom")
    assert seen == ["warning:circuit_open"]


def test_alert_bus_can_be_disabled() -> None:
    bus = AlertBus(Settings(alerts=AlertsSettings(enabled=False)).alerts)
    seen: list[str] = []
    bus.add_handler(lambda code, sev, msg, data: seen.append(code))
    bus.emit("circuit_open", "error", "boom")
    assert seen == []


def test_alert_bus_broken_handler_does_not_break_emit() -> None:
    settings = Settings(alerts=AlertsSettings(enabled=True, min_severity="info"))
    bus = AlertBus(settings.alerts, MetricsRegistry())
    called: list[str] = []

    def broken(code, sev, msg, data) -> None:
        called.append(code)
        raise RuntimeError("sink bug")

    bus.add_handler(broken)
    bus.emit("provider_failure", "warning", "boom")  # must not raise
    assert called == ["provider_failure"]


# ---------------------------------------------------------------------------
# §111 WatchdogService
# ---------------------------------------------------------------------------


def _make_watchdog(settings, health, runners=None, min_severity="warning"):
    metrics = MetricsRegistry()
    alerts = AlertBus(
        Settings(
            alerts=AlertsSettings(enabled=True, min_severity=min_severity)
        ).alerts
    )
    watchdog = WatchdogService(settings, health, alerts, runners or {}, metrics)
    return watchdog, metrics, alerts


def test_watchdog_idle_no_heartbeat_is_not_watched() -> None:
    """§110/§111: boot but no use -> no heartbeat -> the watchdog stays quiet."""
    clock = FakeClock()
    settings = Settings(
        failover=FailoverSettings(
            heartbeat_timeout_ms=1000, watchdog_interval_ms=100
        )
    )
    health = ServiceHealth(settings, metrics=MetricsRegistry(), now_ms=clock)
    watchdog, metrics, _ = _make_watchdog(settings, health)
    watchdog.tick()
    assert metrics.snapshot()["counters"].get("bayaan_watchdog_trips_total", 0) == 0
    assert health.mode.value == "full"


def test_watchdog_trips_on_stale_heartbeat_and_resets_runners() -> None:
    """§111: stale heartbeat -> degrade + alert + soft reboot of that chain;
    never reopens sessions (nothing session-related is touched here)."""
    clock = FakeClock()
    settings = Settings(
        failover=FailoverSettings(
            heartbeat_timeout_ms=1000, watchdog_interval_ms=100
        )
    )
    emitted: list[str] = []
    health = ServiceHealth(settings, metrics=MetricsRegistry(), now_ms=clock)
    runner = RecordingRunner()
    watchdog, metrics, alerts = _make_watchdog(settings, health, {"asr": runner})
    alerts.add_handler(lambda code, sev, msg, data: emitted.append(code))

    # A heartbeat arrives, then goes silent past the threshold.
    health.heartbeat("asr")
    clock.now += 500  # fresh
    watchdog.tick()
    assert metrics.snapshot()["counters"].get("bayaan_watchdog_trips_total", 0) == 0

    clock.now += 600  # now stale (1100ms > 1000ms)
    watchdog.tick()
    counters = metrics.snapshot()["counters"]
    assert counters.get("bayaan_watchdog_trips_total", 0) == 1
    assert health.mode.value == "arabic_only"  # §57: asr outage -> Arabic-only
    assert "watchdog_asr_heartbeat" in (health.degraded_reason or "")
    assert "watchdog_stale_heartbeat" in emitted
    assert runner.resets == 1  # §111 soft reboot happened
    assert runner.state is CircuitState.CLOSED


def test_watchdog_recovers_when_heartbeats_resume() -> None:
    clock = FakeClock()
    settings = Settings(
        failover=FailoverSettings(
            heartbeat_timeout_ms=1000, watchdog_interval_ms=100
        )
    )
    emitted: list[str] = []
    health = ServiceHealth(settings, metrics=MetricsRegistry(), now_ms=clock)
    runner = RecordingRunner()
    watchdog, metrics, alerts = _make_watchdog(
        settings, health, {"asr": runner}, min_severity="info"
    )
    alerts.add_handler(lambda code, sev, msg, data: emitted.append(code))

    health.heartbeat("asr")
    clock.now += 1100
    watchdog.tick()
    assert metrics.snapshot()["counters"].get("bayaan_watchdog_trips_total", 0) == 1

    # The pipeline is alive again: a fresh heartbeat clears the reason.
    clock.now += 100
    health.heartbeat("asr")
    watchdog.tick()
    assert "watchdog_recovered" in emitted
    assert health.mode.value == "full"
    assert health.degraded_reason is None
    assert "asr" not in watchdog.tripped


def test_watchdog_trip_is_idempotent_while_still_stale() -> None:
    clock = FakeClock()
    settings = Settings(
        failover=FailoverSettings(
            heartbeat_timeout_ms=1000, watchdog_interval_ms=100
        )
    )
    health = ServiceHealth(settings, metrics=MetricsRegistry(), now_ms=clock)
    runner = RecordingRunner()
    watchdog, metrics, _ = _make_watchdog(settings, health, {"asr": runner})

    health.heartbeat("asr")
    clock.now += 2000
    watchdog.tick()
    watchdog.tick()  # still stale -> no second trip, no second reset storm
    counters = metrics.snapshot()["counters"]
    assert counters.get("bayaan_watchdog_trips_total", 0) == 1
    assert runner.resets == 1