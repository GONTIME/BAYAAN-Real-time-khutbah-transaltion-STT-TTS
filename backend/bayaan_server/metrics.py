"""Lightweight metrics registry (BAYAAN_MASTER_SPEC.md §190 metrics).

Counters + latency observations rendered in Prometheus text exposition.
No third-party dependency; can be swapped for prometheus-client later
behind the same interface.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque


class MetricsRegistry:
    def __init__(self, max_observations: int = 1000) -> None:
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._observations: dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=max_observations)
        )

    def incr(self, name: str, value: float = 1.0) -> None:
        self._counters[name] += value

    def observe(self, name: str, value: float) -> None:
        self._observations[name].append(value)

    def set_gauge(self, name: str, value: float) -> None:
        self._gauges[name] = value

    def snapshot(self) -> dict:
        return {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "observations": {k: len(v) for k, v in self._observations.items()},
        }

    def render(self) -> str:
        lines: list[str] = []
        for name, value in sorted(self._counters.items()):
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")
        for name, value in sorted(self._gauges.items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        for name, values in sorted(self._observations.items()):
            lines.append(f"# TYPE {name} summary")
            ordered = sorted(values)
            p50 = ordered[len(ordered) // 2] if ordered else 0.0
            p95 = ordered[int(len(ordered) * 0.95)] if ordered else 0.0
            lines.append(f'{name}_count {len(ordered)}')
            lines.append(f'{name}{{quantile="0.5"}} {p50:.3f}')
            lines.append(f'{name}{{quantile="0.95"}} {p95:.3f}')
        return "\n".join(lines) + ("\n" if lines else "")