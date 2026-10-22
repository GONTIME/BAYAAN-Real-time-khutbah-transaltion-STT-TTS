"""§111 service watchdog.

Polls capability heartbeats: when a capability that has been used goes silent
for longer than ``heartbeat_timeout_ms``, the watchdog marks the service
degraded (§57 with ``degraded_mode_reason``), alerts the operator, and performs
the in-process equivalent of §111's "restart service": it soft-reboots that
capability's provider chain by closing every circuit breaker so the next
request probes again.

Two hard §111 guarantees:
- the watchdog NEVER reopens a live session (sessions/status.ts already forbids
  LIVE -> LIVE; sessions.py §111 comment) and never broadcasts anything;
- a capability that has never produced a heartbeat (booted but unused, §110)
  is not watched, so an idle operator console cannot false-alarm.
"""

from __future__ import annotations

import asyncio

from bayaan_server.config import Settings
from bayaan_server.logging_setup import get_logger
from bayaan_server.metrics import MetricsRegistry
from .alerts import AlertBus
from .health import ServiceHealth

logger = get_logger(__name__)


class WatchdogService:
    def __init__(
        self,
        settings: Settings,
        health: ServiceHealth,
        alerts: AlertBus,
        runners: dict[str, object],
        metrics: MetricsRegistry,
    ) -> None:
        self._settings = settings
        self._health = health
        self._alerts = alerts
        self._runners = runners  # capability -> FailoverRunner
        self._metrics = metrics
        self._interval_s = settings.failover.watchdog_interval_ms / 1000.0
        self._timeout_ms = settings.failover.heartbeat_timeout_ms
        self._tripped: set[str] = set()
        self._task: asyncio.Task | None = None
        self._closed = False

    @property
    def tripped(self) -> set[str]:
        """Capabilities currently under a watchdog trip (test/ops observability)."""
        return set(self._tripped)

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._interval_s)
            self.tick()

    def tick(self) -> None:
        """One watchdog pass. Public + synchronous so tests drive it directly."""
        self._metrics.incr("bayaan_watchdog_checks_total")
        for capability in ("asr", "translation", "tts"):
            stale_ms = self._health.stale_ms(capability)
            if stale_ms is None:
                continue  # never used yet -> nothing to watch (§110)
            if stale_ms > self._timeout_ms:
                self._trip(capability, stale_ms)
            else:
                self._recover(capability)

    def _trip(self, capability: str, stale_ms: float) -> None:
        if capability in self._tripped:
            return
        self._tripped.add(capability)
        self._metrics.incr("bayaan_watchdog_trips_total")
        reason = f"watchdog_{capability}_heartbeat"
        self._health.set_degraded(reason)
        # §111 "restart the (provider) service": close every circuit so the
        # next request probes again. Bounded - never touches sessions.
        runner = self._runners.get(capability)
        if runner is not None:
            runner.reset()
        self._alerts.emit(
            "watchdog_stale_heartbeat",
            "warning",
            f"{capability} heartbeat stale ({stale_ms:.0f}ms > {self._timeout_ms}ms); "
            f"provider circuits reset (§111); operator notified",
            capability=capability,
            stale_ms=int(stale_ms),
            threshold_ms=self._timeout_ms,
        )

    def _recover(self, capability: str) -> None:
        if capability not in self._tripped:
            return
        self._tripped.discard(capability)
        self._health.clear_reason(f"watchdog_{capability}_heartbeat")
        self._alerts.emit(
            "watchdog_recovered",
            "info",
            f"{capability} heartbeat is fresh again (§111 recovery)",
            capability=capability,
        )