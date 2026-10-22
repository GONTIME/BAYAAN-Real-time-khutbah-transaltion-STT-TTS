"""TTS queue (BAYAAN_MASTER_SPEC.md §194 Phase 5, §149, §80, §88).

A bounded FIFO of TTS jobs. Two safety properties are enforced here:

- Bounded (§149): the queue never grows without limit. When full, the NEWEST
  job is dropped (never the in-flight one) so a burst cannot stall the live
  imam audio; dropping the freshest line is preferable to unbounded memory.
- Stale-audio dropping (§80/§88): a job whose source is older than
  ``max_staleness_ms`` is discarded before it is ever synthesized. Speaking a
  line the imam finished seconds ago is worse than silence, and re-gating at
  dequeue time catches audio that aged while waiting behind earlier jobs.

Pure stdlib (``collections.deque`` + a lock-free single-consumer design);
synthesis itself is async and lives in ``TtsService``. This module only owns
ordering, bounding and staleness - the parts that must be correct for the
acceptance criterion "unsafe outputs never reach TTS".
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from bayaan_server.config import TtsSettings
from bayaan_server.domain.tts import TtsCandidate


@dataclass
class TtsJob:
    """One queued speech request for a (room, language, caption).

    ``candidate`` is the §80 gate input captured at enqueue time; the worker
    re-runs the gate at dequeue with the *current* age (``dataclasses.replace``)
    so audio that aged in the queue is dropped before it is ever spoken.
    """

    room_id: str
    sequence: int
    created_monotonic_ms: float
    candidate: TtsCandidate
    # Operator-facing gate reasons captured at enqueue time (audit trail §168);
    # the job is only enqueued when the §80 gate allowed it.
    meta: dict = field(default_factory=dict)

    def age_ms(self, now_monotonic_ms: float) -> float:
        return max(0.0, now_monotonic_ms - self.created_monotonic_ms)


class TtsQueue:
    """Bounded, staleness-aware FIFO (§149, §80)."""

    def __init__(self, settings: TtsSettings) -> None:
        self._settings = settings
        self._items: deque[TtsJob] = deque(maxlen=settings.queue_maxsize)
        self._lock = threading.Lock()
        self._dropped_full = 0
        self._dropped_stale = 0

    @property
    def maxsize(self) -> int:
        return self._settings.queue_maxsize

    def __len__(self) -> int:
        return len(self._items)

    @property
    def dropped_full(self) -> int:
        return self._dropped_full

    @property
    def dropped_stale(self) -> int:
        return self._dropped_stale

    def enqueue(self, job: TtsJob) -> bool:
        """Append a job; return False if the queue is full (newest dropped).

        §149: a full queue drops the incoming job rather than blocking the
        live pipeline or growing memory without bound.
        """
        with self._lock:
            if len(self._items) >= self._settings.queue_maxsize:
                self._dropped_full += 1
                return False
            self._items.append(job)
            return True

    def dequeue(self, now_monotonic_ms: float) -> TtsJob | None:
        """Pop the next non-stale job, discarding any stale jobs ahead of it.

        Returns None when the queue is empty or the only remaining jobs are
        stale (they are dropped here, §80). Stale jobs are counted so the
        operator can see how often late audio was suppressed.
        """
        with self._lock:
            while self._items:
                job = self._items.popleft()
                if job.age_ms(now_monotonic_ms) <= self._settings.max_staleness_ms:
                    return job
                self._dropped_stale += 1
            return None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
