"""Room WebSocket broker (BAYAAN_MASTER_SPEC.md §55, §135, §188).

Per-room connection registry with caption history replay. Sends are
fire-and-broadcast: dead connections are dropped, never allowed to stall
the live pipeline (§107 reliability).
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Set

from fastapi import WebSocket

from bayaan_server.config import Settings
from bayaan_server.logging_setup import get_logger
from bayaan_server.metrics import MetricsRegistry

logger = get_logger(__name__)

# §80/§88: how many of the most recent spoken `audio` events to buffer per room
# for replay to (re)connecting clients. Kept small and deliberately SEPARATE from
# the caption history ring so large base64 payloads stay bounded and never bloat
# the caption replay. Real voiced lines are 0.5-2s each, so ~8 entries covers the
# client-side staleness window (8s): a phone that re-connects mid-line still gets
# the current line's audio, while anything older is dropped by the client (§88).
AUDIO_HISTORY_SIZE = 8


@dataclass(eq=False)  # identity-based: instances must be set-members
class Connection:
    websocket: WebSocket
    language: str
    address: str
    session_id: uuid.UUID | None = None


class RoomBroker:
    def __init__(self, settings: Settings, metrics: MetricsRegistry) -> None:
        self._settings = settings
        self._metrics = metrics
        self._rooms: Dict[uuid.UUID, Set[Connection]] = defaultdict(set)
        self._history: Dict[uuid.UUID, Deque[dict]] = defaultdict(
            lambda: deque(maxlen=settings.realtime.history_size)
        )
        # §80/§88: bounded per-room ring of the most recent spoken audio events,
        # replayed to a (re)connecting client so a phone that drops/joins the WS
        # mid-line still hears the current spoken line (captions already replay;
        # audio now does too, within the client-side staleness window). Stored as
        # (born_epoch_ms, event) so stale entries can be pruned by age; the ring
        # deliberately SURVIVES the room emptying (unlike caption history, BUG-017)
        # because the critical case is a single phone that drops and reconnects.
        self._audio_history: Dict[uuid.UUID, Deque[tuple[float, dict]]] = defaultdict(
            lambda: deque(maxlen=AUDIO_HISTORY_SIZE)
        )
        self._peaks: Dict[uuid.UUID, int] = defaultdict(int)

    def _audio_staleness_ms(self) -> float:
        """Client + server agree on the audio staleness window (§80/§88)."""
        return self._settings.tts.max_staleness_ms

    def subscribe(self, room_id: uuid.UUID, conn: Connection) -> None:
        self._rooms[room_id].add(conn)
        self._metrics.incr("bayaan_ws_connections_total")
        self._metrics.set_gauge("bayaan_ws_clients_current", self.client_count())
        self._metrics.set_gauge(f"bayaan_ws_clients_{conn.language}", len(self._rooms[room_id]))
        self._peaks[room_id] = max(self._peaks[room_id], len(self._rooms[room_id]))
        self._metrics.set_gauge("bayaan_ws_peak_clients", max(self._peaks.values()))

    def unsubscribe(self, room_id: uuid.UUID, conn: Connection) -> None:
        self._rooms[room_id].discard(conn)
        if not self._rooms[room_id]:
            del self._rooms[room_id]
            # BUG-017: drop caption replay history with the room so idle rooms
            # leave no residue; keep peaks (operational high-water mark). The
            # AUDIO ring is intentionally kept (age-pruned, bounded) so a phone
            # that re-connects after a brief full disconnect still gets the
            # current spoken line - the audio ring cannot be replayed later once
            # gone, while captions can be re-fetched from the session if needed.
            self._history.pop(room_id, None)
        self._metrics.set_gauge("bayaan_ws_clients_current", self.client_count())
        # BUG-017: per-language gauges must reflect GLOBAL counts, not the
        # just-left room's size. Recompute across all rooms.
        counts = self.language_counts()
        for language, n in counts.items():
            self._metrics.set_gauge(f"bayaan_ws_clients_{language}", n)

    def client_count(self) -> int:
        return sum(len(conns) for conns in self._rooms.values())

    def language_counts(self) -> dict[str, int]:
        """§135 privacy-friendly analytics: language_count only, no identity."""
        counts: Dict[str, int] = defaultdict(int)
        for conns in self._rooms.values():
            for conn in conns:
                counts[conn.language] += 1
        return dict(counts)

    async def publish(self, room_id: uuid.UUID, event: dict) -> None:
        connections = list(self._rooms.get(room_id, ()))
        for conn in connections:
            try:
                await conn.websocket.send_json(event)
            except Exception:  # noqa: BLE001 - dead client must not stall us
                logger.info(
                    "dropping dead websocket client",
                    extra={"event": "ws_drop", "data": {"address": conn.address}},
                )
                self.unsubscribe(room_id, conn)

    async def publish_caption(self, room_id: uuid.UUID, event: dict) -> None:
        self._history[room_id].append(event)
        await self.publish(room_id, event)

    def _born_ms(self, event: dict, now_ms: float) -> float:
        """Epoch-ms birth time of an audio event (falls back to now)."""
        created_at = event.get("created_at")
        if isinstance(created_at, str) and created_at:
            try:
                from datetime import datetime

                return datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp() * 1000.0
            except ValueError:
                pass
        return now_ms

    def _prune_audio(self, room_id: uuid.UUID, now_ms: float) -> None:
        ring = self._audio_history.get(room_id)
        if not ring:
            return
        stale_before = now_ms - self._audio_staleness_ms()
        while ring and ring[0][0] < stale_before:
            ring.popleft()
        if not ring:
            # No fresh audio left: drop the ring entirely so abandoned rooms
            # leave no residue beyond the bounded ring itself.
            self._audio_history.pop(room_id, None)

    def publish_audio(self, room_id: uuid.UUID, event: dict) -> None:
        """Record one spoken-audio event for replay to (re)connecting clients.

        §80/§88: the ring is bounded (AUDIO_HISTORY_SIZE) and age-pruned to the
        client-side staleness window, separate from the caption ring so large
        base64 payloads never bloat caption replay. ``recent_audio`` replays it
        to a (re)connecting client; the live broadcast uses ``publish``.
        """
        now_ms = time.time() * 1000.0
        self._prune_audio(room_id, now_ms)
        self._audio_history[room_id].append((self._born_ms(event, now_ms), event))

    def recent_audio(self, room_id: uuid.UUID) -> list[dict]:
        """Fresh spoken-audio events (oldest first) for connect replay.

        Entries outside the staleness window are pruned (never sent; the room
        client would stale-drop them anyway, §88). Never blocks, never allocates
        beyond the bounded ring.
        """
        now_ms = time.time() * 1000.0
        self._prune_audio(room_id, now_ms)
        return [event for _, event in self._audio_history.get(room_id, ())]

    def history(self, room_id: uuid.UUID) -> list[dict]:
        return list(self._history.get(room_id, ()))