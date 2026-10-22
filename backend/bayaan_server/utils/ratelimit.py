"""Rate limiting (BAYAAN_MASTER_SPEC.md §115).

Separate buckets per category (discovery, websocket, admin) - never one
global limit. In-memory sliding window, keyed by client address; a
multi-worker production deployment must move this to Redis (documented
in docs/adr/0003).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from bayaan_server.config import RateLimitSettings


class RateLimiter:
    def __init__(self, settings: RateLimitSettings) -> None:
        self._settings = settings
        self._events: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)

    def check(self, category: str, key: str) -> bool:
        if not self._settings.enabled:
            return True
        limit = {
            "discovery": self._settings.discovery_per_minute,
            "ws": self._settings.ws_per_minute,
            "admin": self._settings.admin_per_minute,
        }.get(category)
        if limit is None:
            raise ValueError(f"unknown rate limit category: {category}")
        now = time.monotonic()
        window = self._events[(category, key)]
        while window and now - window[0] > 60.0:
            window.popleft()
        # BUG-015: evict fully-expired idle keys so IP churn cannot grow
        # memory without bound. Opportunistic + bounded per call.
        if not window:
            try:
                del self._events[(category, key)]
            except KeyError:
                pass
            self._events[(category, key)].append(now)
            self._evict_idle(now)
            return True
        if len(window) >= limit:
            return False
        window.append(now)
        return True

    def _evict_idle(self, now: float, cap: int = 50) -> None:
        """Remove up to ``cap`` idle keys whose windows fully expired."""
        checked = 0
        for k in list(self._events.keys()):
            if checked >= cap:
                break
            checked += 1
            w = self._events.get(k)
            if w is not None and (not w or now - w[-1] > 60.0):
                try:
                    del self._events[k]
                except KeyError:
                    pass