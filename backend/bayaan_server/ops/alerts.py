"""Alert bus (BAYAAN_MASTER_SPEC.md §195 Alerts, §111 watchdog, §179 ops).

Alerts surface degraded state to the operator (structured log + metrics). A
real endpoint (webhook/pager) can be attached via ``add_handler``; the
configured ``AlertsSettings.webhook_url`` is a documented placeholder until a
concrete endpoint is chosen (ADR-0007). Alerts are never a safety mechanism:
they report, they never gate captions.
"""

from __future__ import annotations

from typing import Any, Callable

from bayaan_server.config import AlertsSettings
from bayaan_server.logging_setup import get_logger
from bayaan_server.metrics import MetricsRegistry

logger = get_logger(__name__)

SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2}

AlertHandler = Callable[[str, str, str, dict], None]


class AlertBus:
    """Structured, severity-gated alert channel with pluggable sinks."""

    def __init__(
        self,
        settings: AlertsSettings,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._metrics = metrics
        self._handlers: list[AlertHandler] = []

    def add_handler(self, handler: AlertHandler) -> None:
        """Attach a sink, e.g. a webhook client. Called synchronously, never
        awaited - a slow sink must buffer/queue itself (§107 non-blocking)."""
        self._handlers.append(handler)

    def emit(
        self,
        code: str,
        severity: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """Emit one alert. ``data`` travels as the structured payload."""
        if not self._settings.enabled:
            return
        if SEVERITY_ORDER.get(severity, 1) < SEVERITY_ORDER.get(
            self._settings.min_severity, 1
        ):
            return

        payload: dict[str, Any] = dict(extra or {})
        if data:
            payload["data"] = data

        event = {"event": f"alert_{code}", "data": payload}
        if severity == "error":
            logger.error(f"alert {code}: {message}", extra=event)
        elif severity == "warning":
            logger.warning(f"alert {code}: {message}", extra=event)
        else:
            logger.info(f"alert {code}: {message}", extra=event)

        if self._metrics is not None:
            self._metrics.incr(f"bayaan_alert_{code}_total")

        for handler in self._handlers:
            try:
                handler(code, severity, message, payload)
            except Exception:  # noqa: BLE001 - a broken sink must not break ops
                logger.exception(
                    "alert handler error",
                    extra={"event": "alert_handler_error", "data": {"code": code}},
                )