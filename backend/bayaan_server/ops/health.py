"""Runtime service-health tracker (§57 degraded modes, §58 offline, §111).

Tracks capability heartbeats and the set of degradation reasons; the mode is
derived from the reasons so FULL is restored as soon as no reason remains.
Reasons are added by the failover layer / pipeline on SAFE_UNAVAILABLE and by
the watchdog on stale heartbeats; they are cleared when a capability produces
a fresh heartbeat again or the watchdog observes recovery.
"""

from __future__ import annotations

from bayaan_server.config import Settings
from bayaan_server.domain.health import (
    ServiceMode,
    default_clock,
    degraded_mode_for_reason,
)
from bayaan_server.logging_setup import get_logger
from bayaan_server.metrics import MetricsRegistry
from .alerts import AlertBus

logger = get_logger(__name__)


class ServiceHealth:
    """Mutable runtime health of the provider-facing pipeline."""

    def __init__(
        self,
        settings: Settings,
        alerts: AlertBus | None = None,
        metrics: MetricsRegistry | None = None,
        now_ms=None,
    ) -> None:
        self._settings = settings
        self._alerts = alerts
        self._metrics = metrics
        self._now_ms = now_ms or default_clock
        self._reasons: set[str] = set()
        self._last_success_ms: dict[str, float] = {}

    # -- heartbeats -------------------------------------------------------
    def heartbeat(self, capability: str) -> None:
        """Record that ``capability`` produced a fresh success. Also clears the
        degradation reasons caused by an outage of that capability."""
        self._last_success_ms[capability] = self._now_ms()
        cleared = {
            reason
            for reason in self._reasons
            if reason == f"watchdog_{capability}_heartbeat"
            or reason.startswith(f"{capability}_")
        }
        self._reasons.difference_update(cleared)
        if cleared:
            self._note_recovery(cleared)

    def stale_ms(self, capability: str) -> float | None:
        """Milliseconds since the capability's last success heartbeat, or None
        if it has never produced one (booted but unused - §110 boot != live)."""
        last = self._last_success_ms.get(capability)
        if last is None:
            return None
        return self._now_ms() - last

    # -- degradation ------------------------------------------------------
    def set_degraded(self, reason: str) -> None:
        """§57: enter a degraded state with an explainable reason."""
        if reason in self._reasons:
            return
        self._reasons.add(reason)
        if self._metrics is not None:
            self._metrics.incr("bayaan_degraded_total")
            self._metrics.set_gauge("bayaan_degraded_mode", 1.0)
        if self._alerts is not None:
            self._alerts.emit(
                "service_degraded",
                "warning",
                f"service degraded: {reason} (mode={self.mode.value})",
                reason=reason,
                mode=self.mode.value,
            )
            # §57: degraded_mode_reason is recorded, never implied.
            logger.warning(
                "degraded_mode_reason",
                extra={"event": "degraded_mode_reason", "data": {"reason": reason}},
            )

    def clear_reason(self, reason: str) -> None:
        if reason not in self._reasons:
            return
        self._reasons.discard(reason)
        self._note_recovery({reason})

    def _note_recovery(self, cleared: set[str]) -> None:
        if self._metrics is not None:
            self._metrics.incr("bayaan_recovered_total")
            if not self._reasons:
                self._metrics.set_gauge("bayaan_degraded_mode", 0.0)
        if self._alerts is not None and not self._reasons:
            self._alerts.emit(
                "service_recovered",
                "info",
                f"service recovered to {self.mode.value} (cleared: {sorted(cleared)})",
                mode=self.mode.value,
            )

    # -- read-only views ---------------------------------------------------
    @property
    def mode(self) -> ServiceMode:
        """§57: worst active reason decides the mode (Arabic-only > text-only)."""
        for reason in self._reasons:
            if degraded_mode_for_reason(reason) is ServiceMode.ARABIC_ONLY:
                return ServiceMode.ARABIC_ONLY
        for reason in self._reasons:
            if degraded_mode_for_reason(reason) is ServiceMode.TEXT_ONLY:
                return ServiceMode.TEXT_ONLY
        return ServiceMode.FULL

    @property
    def degraded_reason(self) -> str | None:
        """§57 degraded_mode_reason: stable, sorted, human-readable."""
        if not self._reasons:
            return None
        return "|".join(sorted(self._reasons))

    def snapshot(self) -> dict:
        return {
            "mode": self.mode.value,
            "degraded_reason": self.degraded_reason,
            "last_success_ms": dict(self._last_success_ms),
        }