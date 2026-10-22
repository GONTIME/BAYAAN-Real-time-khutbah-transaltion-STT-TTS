"""TTS service (BAYAAN_MASTER_SPEC.md §194 Phase 5, §80, §95, §201).

Orchestrates the whole TTS path for the live room:

    caption (already safety-ALLOWed) -> §80 gate -> bounded queue
        -> worker dequeues -> RE-GATE with current age (stale drop)
        -> provider.synthesize -> broadcast an ``audio`` event to the room

Two hard guarantees make the Phase 5 acceptance ("unsafe outputs never reach
TTS") hold:

1. **Nothing is synthesized that has not passed the §80 gate.** ``enqueue``
   runs ``allow_tts`` first and refuses to queue a candidate it would block;
   the worker re-runs the gate at dequeue time with the *current* age, so a
   line that aged past ``max_staleness_ms`` in the queue is dropped, never
   spoken (§80/§88 stale-audio dropping).
2. **TTS is off by default (§201).** With ``safety.tts_enabled`` false the
   service is inert - ``enqueue`` is a no-op - so text-first is the shipped
   behaviour and audio is a deliberate operator opt-in.

The service never blocks the live caption path: ``enqueue`` only appends to a
bounded queue (§149); the async worker drains it. Provider synthesis is the
only network/CPU step and it lives behind the ``TTSProvider`` port (§186).
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from dataclasses import replace

from bayaan_server.config import Settings
from bayaan_server.domain.tts import TtsCandidate, allow_tts
from bayaan_server.logging_setup import get_logger
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.providers.errors import ProviderUnavailableError
from bayaan_server.providers.interfaces import TTSProvider
from bayaan_server.ws.broker import RoomBroker
from .tts_queue import TtsJob, TtsQueue

logger = get_logger(__name__)


class TtsService:
    """Gated, bounded, staleness-dropping TTS orchestrator (§80, §194)."""

    def __init__(
        self,
        settings: Settings,
        provider: TTSProvider,
        broker: RoomBroker,
        metrics: MetricsRegistry,
        health=None,
        alerts=None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._broker = broker
        self._metrics = metrics
        # §195 Phase 6: optional health (heartbeat) + alerts (synthesis outages).
        self._health = health
        self._alerts = alerts
        self._queue = TtsQueue(settings.tts)
        self._task: asyncio.Task | None = None
        self._closed = False

    @property
    def enabled(self) -> bool:
        """§201: TTS is text-first; the single switch is safety.tts_enabled."""
        return self._settings.safety.tts_enabled

    @property
    def queue(self) -> TtsQueue:
        return self._queue

    @staticmethod
    def _now_ms() -> float:
        return time.monotonic() * 1000.0

    async def enqueue_for_room(
        self, room_id: uuid.UUID, sequence: int, candidate: TtsCandidate
    ) -> bool:
        """Gate (§80) then enqueue for a room. True only if a job was queued.

        A disabled service or a gated-off candidate enqueues nothing (§95:
        unsafe output never reaches TTS). Gate refusals are counted for the
        operator (§168) but never surface to the public client (§134).
        """
        if not self.enabled or self._closed:
            return False

        verdict = allow_tts(candidate, self._settings.safety, self._settings.tts)
        if not verdict.speak:
            self._metrics.incr("bayaan_tts_gate_blocked_total")
            logger.info(
                "tts gate refused a candidate",
                extra={
                    "event": "tts_gate_block",
                    "data": {
                        "sequence": sequence,
                        "language": candidate.language,
                        "reasons": verdict.reasons,
                    },
                },
            )
            return False

        job = TtsJob(
            room_id=str(room_id),
            sequence=sequence,
            created_monotonic_ms=self._now_ms(),
            candidate=candidate,
            meta={"language": candidate.language, "gate_reasons": verdict.reasons},
        )
        accepted = self._queue.enqueue(job)
        if not accepted:
            self._metrics.incr("bayaan_tts_queue_full_dropped_total")
        self._metrics.set_gauge("bayaan_tts_queue_length", len(self._queue))
        return accepted

    async def _speak_one(self, job: TtsJob) -> None:
        """Re-gate with current age (§80 staleness), then synthesize+broadcast.

        The re-gate is the second, authoritative chokepoint: even if a job was
        enqueued when fresh, it is refused here if it is now stale or otherwise
        no longer passes §80. Nothing reaches the provider otherwise.
        """
        candidate = replace(job.candidate, age_ms=int(job.age_ms(self._now_ms())))
        verdict = allow_tts(candidate, self._settings.safety, self._settings.tts)
        if not verdict.speak:
            self._metrics.incr("bayaan_tts_stale_dropped_total")
            logger.info(
                "tts worker dropped a stale/unsafe job",
                extra={
                    "event": "tts_worker_drop",
                    "data": {
                        "sequence": job.sequence,
                        "language": candidate.language,
                        "reasons": verdict.reasons,
                    },
                },
            )
            return

        start = time.perf_counter()
        try:
            result = await self._provider.synthesize(candidate.text, candidate.language)
        except ProviderUnavailableError as exc:
            # §195: every TTS provider failed (§87 SAFE_UNAVAILABLE). Drop this
            # job (never speak something we could not safely synthesize) and
            # alert; the worker keeps draining - one bad chain never kills
            # later jobs.
            self._metrics.incr("bayaan_tts_provider_unavailable_total")
            if self._alerts is not None:
                self._alerts.emit(
                    "tts_unavailable",
                    "warning",
                    f"TTS chain unavailable for '{candidate.language}' (§87)",
                    capability="tts",
                    language=candidate.language,
                    error=str(exc)[:300],
                )
            return
        self._metrics.observe(
            "bayaan_tts_latency_ms", (time.perf_counter() - start) * 1000
        )
        self._metrics.incr("bayaan_tts_synthesized_total")
        if self._health is not None:
            self._health.heartbeat("tts")

        from datetime import datetime, timezone as _tz
        event = {
            "type": "audio",
            "sequence": job.sequence,
            "created_at": datetime.now(_tz.utc).isoformat(),
            "language": candidate.language,
            "content_type": candidate.content_type.value,
            "audio_format": result.audio_format,
            "sample_rate": result.sample_rate,
            "duration_ms": result.duration_ms,
            "voice": result.voice,
            # §135: only the audio bytes travel; no identity, no raw transcript.
            "audio_b64": base64.b64encode(result.audio).decode("ascii"),
        }
        await self._broker.publish(uuid.UUID(job.room_id), event)
        # §80/§88: ALSO remember the line for replay to (re)connecting clients so
        # a phone that drops/joins the WS mid-line still hears the current line's
        # audio (captions already replay; audio now does too, within the client's
        # 8s staleness window). Bounded ring; never blocks the live broadcast.
        self._broker.publish_audio(uuid.UUID(job.room_id), event)

    async def _worker(self) -> None:
        """Drain the queue until cancelled (lifespan-managed)."""
        while not self._closed:
            job = self._queue.dequeue(self._now_ms())
            if job is None:
                await asyncio.sleep(0.02)
                continue
            try:
                await self._speak_one(job)
            except Exception:  # noqa: BLE001 - one bad job must not kill audio
                self._metrics.incr("bayaan_tts_errors_total")
                logger.exception(
                    "tts worker error",
                    extra={"event": "tts_worker_error", "data": {}},
                )

    def start(self) -> None:
        """Start the background worker (only meaningful when enabled)."""
        if self._task is None and self.enabled and not self._closed:
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._queue.clear()
