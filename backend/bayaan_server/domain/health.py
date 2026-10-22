"""Operational health domain rules (§54 circuit breaker, §57 degraded modes,
§111 service watchdog).

Pure, clock-injectable state machines - no I/O, no settings import. Thresholds
are always passed in from configuration (§185: no magic values in code).
"""

from __future__ import annotations

import enum
import time
from typing import Callable

# A monotonic-milliseconds clock (injectable for deterministic tests).
NowMs = Callable[[], float]


def default_clock() -> float:
    return time.monotonic() * 1000.0


class CircuitState(str, enum.Enum):
    """§54 circuit-breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """§54: CLOSED -> (repeated failures) -> OPEN -> (cool-down) -> HALF_OPEN
    -> (success) -> CLOSED.

    ``begin_attempt`` refuses once OPEN (no retry storm); HALF_OPEN admits
    exactly one probe so a recovered provider is re-adopted quickly while a
    still-broken one is tripped again - §54: never unlimited retries.
    """

    def __init__(
        self,
        failure_threshold: int,
        cooldown_ms: int,
        now_ms: NowMs | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_ms < 0:
            raise ValueError("cooldown_ms must be >= 0")
        self._threshold = failure_threshold
        self._cooldown_ms = cooldown_ms
        self._now_ms = now_ms or default_clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at_ms = 0.0
        self._probe_in_use = False

    @property
    def state(self) -> CircuitState:
        # Lazy OPEN -> HALF_OPEN transition once the cool-down has elapsed.
        if (
            self._state is CircuitState.OPEN
            and self._now_ms() - self._opened_at_ms >= self._cooldown_ms
        ):
            self._state = CircuitState.HALF_OPEN
            self._probe_in_use = False
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def begin_attempt(self) -> bool:
        """Reserve the right to call the provider now. False => leave it alone."""
        current = self.state
        if current is CircuitState.CLOSED:
            return True
        if current is CircuitState.HALF_OPEN:
            if self._probe_in_use:
                return False
            self._probe_in_use = True
            return True
        return False

    def record_success(self) -> None:
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._probe_in_use = False

    def record_failure(self) -> None:
        self._probe_in_use = False
        if self._state is CircuitState.HALF_OPEN:
            # §54: a failed HALF_OPEN probe re-opens immediately, restarting
            # the cool-down - one probe per period, no probe hammering.
            self._state = CircuitState.OPEN
            self._opened_at_ms = self._now_ms()
            self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._state = CircuitState.OPEN
            self._opened_at_ms = self._now_ms()
            self._consecutive_failures = 0

    def reset(self) -> None:
        """§111 watchdog soft-reboot: close the circuit so the next request
        probes the provider again. Bounded - it never touches sessions."""
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at_ms = 0.0
        self._probe_in_use = False


class ServiceMode(str, enum.Enum):
    """§57 degraded modes. FULL is the only healthy state."""

    FULL = "full"
    TEXT_ONLY = "text_only"          # captions without speech (TTS down)
    PREPARED_ONLY = "prepared_only"  # ASR + prepared alignment + approved text
    ARABIC_ONLY = "arabic_only"      # original audio + Arabic transcript when safe (§178)


def degraded_mode_for_reason(reason: str) -> ServiceMode:
    """§57/§178: pick a degraded mode from a degradation reason code.

    An ASR or translation outage leaves only the original Arabic service;
    a TTS outage degrades from full to text-only. Unknown reasons default
    to text-only (never claim a mode more capable than we can guarantee).
    """
    if reason.startswith(
        ("asr_", "translation_", "watchdog_asr", "watchdog_translation")
    ):
        return ServiceMode.ARABIC_ONLY
    if reason.startswith(("tts_", "watchdog_tts")):
        return ServiceMode.TEXT_ONLY
    return ServiceMode.TEXT_ONLY