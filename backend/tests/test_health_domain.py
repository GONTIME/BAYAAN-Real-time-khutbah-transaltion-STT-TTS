"""Phase 6 domain tests: §54 circuit breaker and §57 degraded modes.

Pure, clock-injectable state machines - no I/O, no network. This is where the
§54 CLOSED -> OPEN -> HALF_OPEN -> CLOSED loop and the §57 degraded-mode mapping
are pinned down.
"""

from __future__ import annotations

from bayaan_server.domain.health import (
    CircuitBreaker,
    CircuitState,
    ServiceMode,
    degraded_mode_for_reason,
)
from bayaan_server.ops.health import ServiceHealth
from bayaan_server.config import Settings
from bayaan_server.metrics import MetricsRegistry


class FakeClock:
    """Deterministic monotonic-milliseconds clock for breaker tests."""

    def __init__(self, start_ms: float = 1000.0) -> None:
        self.now = start_ms

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------
# §54 circuit breaker
# --------------------------------------------------------------------------


def test_circuit_closed_allows_attempts() -> None:
    breaker = CircuitBreaker(failure_threshold=3, cooldown_ms=5000, now_ms=FakeClock())
    assert breaker.state is CircuitState.CLOSED
    assert breaker.begin_attempt() is True


def test_circuit_opens_after_threshold_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_ms=5000, now_ms=FakeClock())
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    # §54: while OPEN the circuit refuses attempts -> retries are bounded.
    assert breaker.begin_attempt() is False


def test_circuit_half_open_probe_reopens_on_failure() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_ms=1000, now_ms=clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    # No cooldown elapsed yet.
    assert breaker.begin_attempt() is False

    clock.now += 1000  # cool-down elapsed -> HALF_OPEN
    assert breaker.state is CircuitState.HALF_OPEN
    # Exactly one probe is admitted...
    assert breaker.begin_attempt() is True
    # ...and a second concurrent probe is refused while it is in flight.
    assert breaker.begin_attempt() is False
    breaker.record_failure()  # probe failed -> back to OPEN
    assert breaker.state is CircuitState.OPEN


def test_circuit_half_open_probe_recovers_to_closed() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_ms=1000, now_ms=clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.now += 1000  # -> HALF_OPEN
    assert breaker.begin_attempt() is True
    breaker.record_success()  # probe succeeded -> CLOSED
    assert breaker.state is CircuitState.CLOSED
    assert breaker.consecutive_failures == 0


def test_circuit_reset_watchdog_soft_reboot() -> None:
    """§111: watchdog reset closes an OPEN circuit for probing again."""
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_ms=60000, now_ms=clock)
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    breaker.reset()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.begin_attempt() is True
    assert breaker.consecutive_failures == 0


def test_circuit_rejects_bad_config() -> None:
    import pytest

    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0, cooldown_ms=1)
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=1, cooldown_ms=-1)


# --------------------------------------------------------------------------
# §57 degraded modes
# --------------------------------------------------------------------------


def test_degraded_mode_for_reason_mapping() -> None:
    assert degraded_mode_for_reason("asr_unavailable") is ServiceMode.ARABIC_ONLY
    assert degraded_mode_for_reason("watchdog_translation_heartbeat") is ServiceMode.ARABIC_ONLY
    assert degraded_mode_for_reason("tts_unavailable") is ServiceMode.TEXT_ONLY
    assert degraded_mode_for_reason("watchdog_tts_heartbeat") is ServiceMode.TEXT_ONLY
    assert degraded_mode_for_reason("unknown_weird") is ServiceMode.TEXT_ONLY


def test_service_health_full_by_default() -> None:
    health = ServiceHealth(Settings())
    assert health.mode is ServiceMode.FULL
    assert health.degraded_reason is None


def test_service_health_stale_unknown_until_first_heartbeat() -> None:
    """§110: booted-but-unused capabilities are NOT watched (None, not 0)."""
    health = ServiceHealth(Settings())
    assert health.stale_ms("asr") is None


def test_service_health_degrades_and_recovers() -> None:
    health = ServiceHealth(Settings(), metrics=MetricsRegistry())
    health.set_degraded("asr_unavailable")
    assert health.mode is ServiceMode.ARABIC_ONLY
    assert health.degraded_reason == "asr_unavailable"

    health.set_degraded("tts_unavailable")
    # ASR outage is worst -> stays arabic_only.
    assert health.mode is ServiceMode.ARABIC_ONLY

    # ASR becomes healthy again -> heartbeat clears the asr reason.
    health.heartbeat("asr")
    assert health.mode is ServiceMode.TEXT_ONLY
    health.heartbeat("tts")
    assert health.mode is ServiceMode.FULL
    assert health.degraded_reason is None


def test_service_health_clear_reason_direct() -> None:
    health = ServiceHealth(Settings())
    health.set_degraded("watchdog_asr_heartbeat")
    assert health.mode is ServiceMode.ARABIC_ONLY
    health.clear_reason("watchdog_asr_heartbeat")
    assert health.mode is ServiceMode.FULL