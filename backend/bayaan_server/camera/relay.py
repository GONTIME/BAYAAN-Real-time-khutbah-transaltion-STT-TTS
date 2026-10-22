"""PC camera relay (ADR-0015).

The operator's browser captures its webcam via ``getUserMedia`` and pumps JPEG
frames over an operator-authenticated WebSocket (``/ws/sessions/{id}/camera``).
``CameraRelay`` keeps only the latest frame per session in memory (bounded,
§149) and serves it to any number of viewers as an ``multipart/x-mixed-replace``
MJPEG stream through ``api/routes/camera.py``.

Design notes:
- One frame per session, never a backlog: memory is O(active sessions), so it
  cannot grow with the camera frame rate (§149). A slow viewer simply sees the
  most recent frame on its next wake, never a queue.
- In-process and per-server, like the room broker (§55). A multi-worker
  deployment must pin the relay to one worker or move it to a shared store
  (documented in ADR-0015).
- ``subscribe`` is the viewer path and honours ``max_viewers_per_session`` via
  ``acquire_viewer`` (the HTTP route raises 503 when the cap is hit) so an
  unbounded fan-out cannot be created by a hostile LAN client.
- The slot is dropped when the last camera publisher disconnects or when the
  session ends (``drop`` from the session lifecycle handler), which ends all
  active MJPEG streams.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from bayaan_server.config import CameraSettings


class ViewerLimitError(RuntimeError):
    """Raised when a session's camera viewer cap is already reached."""


@dataclass
class _SessionSlot:
    # Latest complete JPEG frame (b"" until the first frame lands).
    latest: bytes = b""
    # Monotonic frame version: lets viewers skip unchanged frames cheaply.
    version: int = 0
    # Concurrent publisher markers (operator camera WebSockets).
    publishers: int = 0
    # Active viewer markers (current MJPEG HTTP consumers).
    viewers: set[object] = field(default_factory=set)
    # Wake-all signal for viewers. Set by every state transition (new frame,
    # last publisher leaving, slot drop); a viewer clears it only while holding
    # the relay lock, which makes clear/set race-free against writers. Viewers
    # wait on this instead of polling, so a frame reaches the projector the
    # moment it lands (no viewer-poll latency).
    event: asyncio.Event = field(default_factory=asyncio.Event)


class CameraRelay:
    """Latest-frame relay for the PC camera feed (ADR-0015)."""

    def __init__(self, settings: CameraSettings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._slots: dict[uuid.UUID, _SessionSlot] = {}

    @property
    def settings(self) -> CameraSettings:
        return self._settings

    async def start_stream(self, session_id: uuid.UUID) -> None:
        """Register a camera publisher for the session (WS accept)."""
        async with self._lock:
            slot = self._slots.get(session_id)
            if slot is None:
                slot = _SessionSlot()
                self._slots[session_id] = slot
            slot.publishers += 1

    async def stop_stream(self, session_id: uuid.UUID) -> None:
        """Unregister a camera publisher (WS close). The slot (and therefore
        every open MJPEG stream) is dropped when the last publisher leaves."""
        async with self._lock:
            slot = self._slots.get(session_id)
            if slot is None:
                return
            slot.publishers -= 1
            if slot.publishers <= 0:
                # Wake viewers before dropping so blocked streams end
                # immediately instead of on their next poll fallback.
                slot.event.set()
                self._slots.pop(session_id, None)

    async def write_frame(self, session_id: uuid.UUID, jpeg: bytes) -> bool:
        """Store the latest frame for a session.

        Returns False (and stores nothing) when the frame exceeds the configured
        size cap or the camera is no longer publishing — the operator client
        treats that as \"the camera stream ended\", never as an error.
        """
        if len(jpeg) > self._settings.max_frame_bytes:
            return False
        async with self._lock:
            slot = self._slots.get(session_id)
            if slot is None:
                return False
            slot.latest = jpeg
            slot.version += 1
            slot.event.set()
        return True

    async def acquire_viewer(
        self, session_id: uuid.UUID
    ) -> "asyncio.AsyncIterator[bytes] | None":
        """Reserve a viewer slot and return the frame stream for ``session_id``.

        Returns ``None`` when the per-session viewer cap is already reached.
        The returned async iterator yields each successive latest frame and
        ends when the session's camera slot is dropped.
        """
        async with self._lock:
            slot = self._slots.get(session_id)
            if slot is None:
                slot = _SessionSlot()
                self._slots[session_id] = slot
            if len(slot.viewers) >= self._settings.max_viewers_per_session:
                return None
            marker = object()
            slot.viewers.add(marker)
        return self._viewer(session_id, marker)

    async def _viewer(
        self, session_id: uuid.UUID, marker: object
    ) -> "asyncio.AsyncIterator[bytes]":
        try:
            last_version = -1
            while True:
                timeout: float | None = None
                async with self._lock:
                    slot = self._slots.get(session_id)
                    if slot is None:
                        return
                    if slot.version != last_version and slot.latest:
                        last_version = slot.version
                        frame: bytes | None = slot.latest
                    else:
                        frame = None
                        # Clear only while holding the lock: writers set the
                        # event under the same lock, so no wake-up can be lost
                        # between this clear and the wait below.
                        slot.event.clear()
                        timeout = self._settings.viewer_poll_interval_seconds
                if frame is not None:
                    yield frame
                    continue
                try:
                    # Event-driven wake: frames (and slot teardown) reach the
                    # viewer immediately. The poll interval is only a defensive
                    # fallback so a missed wake can never hang a stream.
                    await asyncio.wait_for(slot.event.wait(), timeout=timeout)
                except TimeoutError:
                    continue
        finally:
            # Deliberately lock-free: this single discard must survive task
            # cancellation (HTTP disconnect) so the viewer cap can never leak.
            # Single-loop asyncio makes the set mutation atomic between awaits.
            slot = self._slots.get(session_id)
            if slot is not None:
                slot.viewers.discard(marker)

    async def drop(self, session_id: uuid.UUID) -> None:
        """Remove a session's slot entirely (session end / cleanup). Ends any
        in-flight MJPEG streams for that session."""
        async with self._lock:
            slot = self._slots.pop(session_id, None)
            if slot is not None:
                # Wake viewers so in-flight MJPEG streams end immediately.
                slot.event.set()

    async def active_sessions(self) -> list[uuid.UUID]:
        """Sessions that currently have a relay slot (publisher or viewer)."""
        async with self._lock:
            return list(self._slots.keys())