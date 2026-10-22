"""Server-side PC audio capture (ADR-0014, BAYAAN_MASTER_SPEC.md §190).

When ``audio.capture_mode == "server_device"`` and capture is enabled, the
server itself is the audio source: this service opens the configured input
device with ``DeviceAudioSource`` (``sounddevice``) and streams its frames
through ``LiveIngestService.ingest_stream_frames`` - the SAME pipeline a WAV
upload uses (never a second, weaker path, §0 rule 5).

Capture is started/stopped with the session lifecycle (§110): the API layer
calls :meth:`CaptureService.start` when a session goes LIVE and
:meth:`CaptureService.stop` when it ends or fails, so audio only flows while a
session is actually running. The master switch ``audio.capture_enabled`` keeps
this OFF by default (the shipped spec is mixer/WAV-only); a missing
``sounddevice`` extra surfaces as an ``AudioFormatError`` that is logged and
never crashes the server (§184, §0 rule 8 - honest failure).
"""

from __future__ import annotations

import asyncio
import uuid

from bayaan_server.config import Settings
from bayaan_server.logging_setup import get_logger

logger = get_logger(__name__)


class CaptureService:
    """Runs one device-capture task per LIVE session (bounded, §149)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}

    @property
    def active_sessions(self) -> set[uuid.UUID]:
        """Sessions currently capturing from the server device (ops/test view)."""
        return set(self._tasks)

    def is_capturing(self, session_id: uuid.UUID) -> bool:
        return session_id in self._tasks

    def start(self, session_id: uuid.UUID, ingest_service, session_factory) -> None:
        """Begin device capture for ``session_id`` and feed the live pipeline.

        No-op unless ``capture_enabled`` and ``capture_mode == "server_device"``
        (§184: only the configured path activates). Idempotent - starting an
        already-capturing session does nothing.
        """
        audio = self._settings.audio
        if not audio.capture_enabled or audio.capture_mode != "server_device":
            return
        if session_id in self._tasks:
            return

        self._tasks[session_id] = asyncio.create_task(
            self._capture(session_id, ingest_service, session_factory)
        )

    def stop(self, session_id: uuid.UUID) -> None:
        """Cancel capture for one session (idempotent)."""
        task = self._tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def stop_all(self) -> None:
        """Cancel every running capture (called on app shutdown)."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _capture(self, session_id: uuid.UUID, ingest_service, session_factory) -> None:
        # Deferred import: DeviceAudioSource pulls in the optional ``sounddevice``
        # extra; importing here keeps the service importable without it (§184).
        from bayaan_server.audio.sources import AudioFormatError, DeviceAudioSource

        try:
            source = DeviceAudioSource(self._settings)
            frames = source.frames()
            try:
                await ingest_service.ingest_stream_frames(
                    session_id, session_factory, frames
                )
            finally:
                await frames.aclose()
        except asyncio.CancelledError:
            # Normal on session end / shutdown (§110): re-raise so the task ends.
            raise
        except AudioFormatError as exc:
            # §184: sounddevice missing / device unavailable. Loud log, no crash.
            logger.warning(
                "device capture unavailable",
                extra={
                    "event": "capture_unavailable",
                    "data": {"session_id": str(session_id), "error": str(exc)},
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "device capture failed",
                extra={
                    "event": "capture_error",
                    "data": {"session_id": str(session_id), "error": str(exc)},
                },
            )
