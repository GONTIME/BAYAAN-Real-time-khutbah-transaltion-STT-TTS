"""TTS queue tests (BAYAAN_MASTER_SPEC.md §149 bounded, §80/§88 stale drop)."""

from __future__ import annotations

import time

from bayaan_server.config import TtsSettings
from bayaan_server.domain.enums import ContentType, SafetyDecision
from bayaan_server.domain.tts import TtsCandidate
from bayaan_server.pipeline.tts_queue import TtsJob, TtsQueue


def _candidate(language: str = "en") -> TtsCandidate:
    return TtsCandidate(
        text="salaam",
        language=language,
        content_type=ContentType.GENERAL,
        safety_decision=SafetyDecision.ALLOW,
        validation_decision=SafetyDecision.ALLOW,
        translation_confidence=0.99,
        age_ms=0,
    )


def _job(sequence: int, now_ms: float, language: str = "en") -> TtsJob:
    return TtsJob(
        room_id="11111111-1111-1111-1111-111111111111",
        sequence=sequence,
        created_monotonic_ms=now_ms,
        candidate=_candidate(language),
    )


def test_fifo_order() -> None:
    q = TtsQueue(TtsSettings(queue_maxsize=8))
    now = time.monotonic() * 1000
    q.enqueue(_job(0, now))
    q.enqueue(_job(1, now))
    assert len(q) == 2
    assert q.dequeue(now).sequence == 0
    assert q.dequeue(now).sequence == 1


def test_queue_is_bounded_and_drops_newest_when_full() -> None:
    # §149: never unbounded. A full queue drops the incoming (newest) job so a
    # burst cannot grow memory or stall the live imam audio.
    q = TtsQueue(TtsSettings(queue_maxsize=2))
    now = time.monotonic() * 1000
    assert q.enqueue(_job(0, now)) is True
    assert q.enqueue(_job(1, now)) is True
    assert q.enqueue(_job(2, now)) is False  # full -> newest dropped
    assert q.dropped_full == 1
    assert len(q) == 2
    # The two oldest survive, in order.
    assert q.dequeue(now).sequence == 0
    assert q.dequeue(now).sequence == 1


def test_stale_jobs_are_dropped_at_dequeue() -> None:
    # §80/§88: a job that aged past max_staleness_ms is discarded, not spoken.
    tts = TtsSettings(queue_maxsize=8, max_staleness_ms=1000)
    q = TtsQueue(tts)
    now = time.monotonic() * 1000
    q.enqueue(_job(0, now))          # fresh
    q.enqueue(_job(1, now - 5000))   # already stale at enqueue
    # Dequeue at a time where job 0 is still fresh.
    first = q.dequeue(now + 100)
    assert first is not None and first.sequence == 0
    # Job 1 is stale -> dropped, dequeue returns None.
    assert q.dequeue(now + 100) is None
    assert q.dropped_stale == 1


def test_stale_jobs_ahead_of_fresh_are_skipped() -> None:
    # A stale job at the head must not block a fresh job behind it.
    tts = TtsSettings(queue_maxsize=8, max_staleness_ms=1000)
    q = TtsQueue(tts)
    now = time.monotonic() * 1000
    q.enqueue(_job(0, now - 9000))  # stale
    q.enqueue(_job(1, now))          # fresh
    job = q.dequeue(now)
    assert job is not None and job.sequence == 1
    assert q.dropped_stale == 1


def test_empty_dequeue_returns_none() -> None:
    q = TtsQueue(TtsSettings())
    assert q.dequeue(time.monotonic() * 1000) is None


def test_clear_empties_queue() -> None:
    q = TtsQueue(TtsSettings(queue_maxsize=8))
    now = time.monotonic() * 1000
    q.enqueue(_job(0, now))
    q.clear()
    assert len(q) == 0
    assert q.dequeue(now) is None
