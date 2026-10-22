"""Live ingest pipeline (§190 ASR live integration, §55 caption broadcast).

    audio frames -> VAD utterances -> ASR -> normalization -> rule detection
                 -> persist LiveSegment
                 -> translate per target language -> safety gate
                 -> broadcast caption event to room WebSocket clients

Phase 2: target text reaches phones (acceptance: live Arabic/target text).
Alignment and validator engines do not exist yet (Phase 4 §193), so those
safety signals are None (config-flagged off), never faked (§166/§167).
Unsafe output NEVER reaches clients: blocked/fallback captions become the
public fallback message (§134) and are audited (§98, §168).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel

from bayaan_server.config import Settings
from bayaan_server.db.repository import AuditLogRepository, LiveSegmentRepository
from bayaan_server.domain.enums import AuditAction, ContentType, SafetyDecision
from bayaan_server.domain.models import ConfidenceBreakdown, LiveSegment
from bayaan_server.domain.safety import PUBLIC_FALLBACK_MESSAGE, evaluate_safety
from bayaan_server.domain.tts import TtsCandidate, allow_tts
from bayaan_server.domain.validation import ValidationVerdict
from bayaan_server.logging_setup import get_logger
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.audio.format import PcmFormat, frame_iterator
from bayaan_server.audio.vad import EnergyVAD
from bayaan_server.providers.errors import ProviderUnavailableError
from bayaan_server.providers.interfaces import ASRProvider, TranslationProvider
from bayaan_server.pipeline.aligner import AlignmentCandidate, SermonAligner
from bayaan_server.pipeline.confidence import translation_confidence
from bayaan_server.pipeline.segmenter import ArabicDiscourseSegmenter
from bayaan_server.pipeline.validators import Validator
from .classifier import RuleContentDetector
from .normalizer import ArabicNormalizerV1

logger = get_logger(__name__)

# Live-streaming backpressure: how many completed VAD utterances may wait in the
# consumer queue before the producer is allowed to stall. This decouples the
# ingest WebSocket receive loop (which must keep draining in real time) from the
# slow per-utterance pipeline (ASR + per-language translation + safety + TTS),
# while still bounding work (§149 bounded resources): at ~4 utterances the
# stream waits rather than growing without limit.
_MAX_PENDING_UTTERANCES = 4


class IngestResult(BaseModel):
    session_id: uuid.UUID
    utterances: int
    segments: list[LiveSegment]
    audio_seconds: float


class LiveIngestService:
    def __init__(
        self,
        settings: Settings,
        asr: ASRProvider,
        translation: TranslationProvider,
        metrics: MetricsRegistry,
        tts_service=None,
        health=None,
        alerts=None,
    ) -> None:
        self._settings = settings
        self._asr = asr
        self._translation = translation
        self._metrics = metrics
        self._broker = None
        self._tts_service = tts_service
        # §57/§195: optional runtime health + alerts wired by the API layer.
        self._health = health
        self._alerts = alerts
        self._fmt = PcmFormat(
            sample_rate=settings.audio.sample_rate,
            channels=settings.audio.channels,
        )
        self._normalizer = ArabicNormalizerV1()
        self._detector = RuleContentDetector(self._normalizer)
        self._segmenter = ArabicDiscourseSegmenter(settings.live_transcript)
        # Phase 4 engines (§193): aligner + validator. Pure logic, thresholds
        # from settings (§185). The scripture matcher is built per-session
        # against the approved scripture store (see _scripture_matcher).
        self._aligner = SermonAligner(settings.alignment)
        self._validator = Validator(settings.validation)
        self._previous_alignment_order: dict[uuid.UUID, int | None] = {}

    def set_broker(self, broker) -> None:
        """Wired by the API layer at startup (§184 explicit wiring)."""
        self._broker = broker

    def release_session(self, session_id) -> None:
        """BUG-016: drop per-session alignment state on session end."""
        self._previous_alignment_order.pop(session_id, None)

    async def ingest_pcm(
        self, session_id: uuid.UUID, session_factory, pcm: bytes
    ) -> IngestResult:
        vad = EnergyVAD(self._settings.vad, self._fmt)
        segments: list[LiveSegment] = []
        room_id: uuid.UUID | None = None
        sermon_version_id: uuid.UUID | None = None

        async with session_factory() as db:
            from bayaan_server.db.repository import LiveSessionRepository

            session_row = await LiveSessionRepository(db).get(session_id)
            room_id = session_row.room_id if session_row else None
            sermon_version_id = (
                session_row.sermon_version_id if session_row else None
            )

            repo = LiveSegmentRepository(db)
            sequence = await repo.next_sequence(session_id)

            for frame in frame_iterator(pcm, self._fmt):
                self._metrics.incr("bayaan_audio_frames_total")
                utterance = vad.feed(frame)
                if utterance is not None:
                    segment = await self._process_utterance(
                        session_id, sequence, repo, db, utterance.pcm, room_id,
                        sermon_version_id,
                    )
                    if segment is not None:
                        segments.append(segment)
                        sequence += 1

            trailing = vad.flush()
            if trailing is not None:
                segment = await self._process_utterance(
                    session_id, sequence, repo, db, trailing.pcm, room_id,
                    sermon_version_id,
                )
                if segment is not None:
                    segments.append(segment)
                    sequence += 1

            await db.commit()

        audio_seconds = self._fmt.duration_ms(len(pcm)) / 1000
        self._metrics.incr("bayaan_ingest_audio_seconds_total", audio_seconds)
        self._metrics.incr("bayaan_vad_utterances_total", len(segments))
        logger.info(
            "ingest complete",
            extra={
                "event": "ingest_complete",
                "data": {
                    "session_id": str(session_id),
                    "utterances": len(segments),
                    "audio_seconds": audio_seconds,
                },
            },
        )
        return IngestResult(
            session_id=session_id,
            utterances=len(segments),
            segments=segments,
            audio_seconds=audio_seconds,
        )

    async def ingest_stream_frames(
        self, session_id: uuid.UUID, session_factory, frames
    ) -> None:
        """Stream live PCM frames through the full pipeline (ADR-0014 PC input).

        ``frames`` is an async iterator of fixed-size int16 PCM frames in the
        configured format (20 ms mono @ 16 kHz). Both PC-input paths funnel
        through here - server-side ``DeviceAudioSource`` capture and the
        operator browser ingest WebSocket - so there is NEVER a second, weaker
        path (§0 rule 5): each VAD utterance runs the identical
        VAD -> ASR -> normalize -> §78 route -> §77 align -> persist -> §79
        validate -> §95 gate -> broadcast -> §80 TTS chain as a WAV upload.

        One persistent EnergyVAD gives clean utterance boundaries across the
        whole live stream. Each utterance is processed in its own short DB
        session (never one long-lived session across a khutbah - §149 bounded
        resources), so a failed/flushed utterance never blocks the next.

        Frame receipt and utterance processing are DECOUPLED by a small bounded
        queue: the caller pulls ``frames`` (and therefore the live WebSocket is
        drained) in real time while a previous utterance is still inside
        ASR/translation. Before this, the whole receive loop paused for the
        duration of translation and voice spoken during that window was lost
        (§216 voice-to-text gap). Utterances stay strictly sequential so
        ``next_sequence`` ordering and the BUG-018 race retry are preserved.
        """
        vad = EnergyVAD(self._settings.vad, self._fmt)
        audio_bytes = 0

        async def _drain(utterance) -> None:
            if utterance is None:
                return
            async with session_factory() as db:
                from bayaan_server.db.repository import (
                    LiveSegmentRepository,
                    LiveSessionRepository,
                )

                session_row = await LiveSessionRepository(db).get(session_id)
                if session_row is None:
                    return
                repo = LiveSegmentRepository(db)
                sequence = await repo.next_sequence(session_id)
                segment = await self._process_utterance(
                    session_id, sequence, repo, db, utterance.pcm,
                    session_row.room_id, session_row.sermon_version_id,
                )
                await db.commit()
            if segment is not None:
                self._metrics.incr("bayaan_vad_utterances_total")

        async def _consumer() -> None:
            while True:
                item = await pending.get()
                if item is None:  # end-of-stream sentinel
                    pending.task_done()
                    return
                try:
                    await _drain(item)
                finally:
                    pending.task_done()

        pending: asyncio.Queue = asyncio.Queue(maxsize=_MAX_PENDING_UTTERANCES)
        consumer = asyncio.create_task(_consumer())
        try:
            async for frame in frames:
                self._metrics.incr("bayaan_audio_frames_total")
                audio_bytes += len(frame)
                utterance = vad.feed(frame)
                if utterance is not None:
                    # Backpressure only when the consumer is several translation
                    # latencies behind; normally returns immediately so the
                    # WebSocket keeps being drained in real time (§216).
                    await pending.put(utterance)
                    # Re-raise a consumer failure so the stream stops loudly
                    # instead of running against a dead pipeline.
                    if consumer.done():
                        await consumer
            # Flush the in-progress utterance at end of stream (§190 continuity).
            trailing = vad.flush()
            if trailing is not None:
                await pending.put(trailing)
            await pending.put(None)  # sentinel: drain the queue, then stop.
            await consumer
        finally:
            if not consumer.done():
                # Abrupt disconnect: stop processing queued utterances so this
                # route returns promptly (§149 bounded resources). If the
                # consumer was mid-_drain, cancellation rolls back its DB
                # session via the async context manager.
                consumer.cancel()
            try:
                await consumer
            except (asyncio.CancelledError, Exception):
                pass
            audio_seconds = self._fmt.duration_ms(audio_bytes) / 1000
            self._metrics.incr(
                "bayaan_ingest_audio_seconds_total", audio_seconds
            )
            logger.info(
                "stream ingest complete",
                extra={
                    "event": "stream_ingest_complete",
                    "data": {
                        "session_id": str(session_id),
                        "audio_seconds": audio_seconds,
                    },
                },
            )

    async def _scripture_matcher(self, db):
        """Build a §76 matcher over the APPROVED scripture store (§42, §206).

        Only approved verses are loaded: unapproved verses are never reachable
        live (§78, §0 rule 8). Empty store => every Quran hit is UNKNOWN and
        blocks (the safe default)."""
        from bayaan_server.db.repository import ScriptureRepository
        from bayaan_server.sermon.scripture import ScriptureMatcher

        quran = await ScriptureRepository(db).list_approved("QURAN")
        hadith = await ScriptureRepository(db).list_approved("HADITH")
        return ScriptureMatcher(self._settings.scripture, quran + hadith)

    async def _alignment_confidence(
        self, session_id, normalized, sermon_version_id, db, current_index
    ) -> tuple[float | None, str | None]:
        """§77 align live text against the prepared version's local window.

        Returns (alignment_confidence, confirmed_segment_id). With no prepared
        version, alignment disabled, or no candidates, alignment is genuinely
        ABSENT: None (§166 absent != zero), so the §167 composite excludes it
        rather than treating "no sermon" as "zero alignment". A real (possibly
        low) score is returned only when alignment actually ran."""
        if sermon_version_id is None or not self._settings.alignment.enabled:
            return None, None

        from bayaan_server.db.repository import SermonRepository

        segments = await SermonRepository(db).list_segments(sermon_version_id)
        if not segments:
            return None, None
        candidates = [
            AlignmentCandidate(
                id=str(s.id), normalized_arabic=s.normalized_arabic, order=s.segment_order
            )
            for s in segments
        ]
        previous = self._previous_alignment_order.get(session_id)
        result = self._aligner.align(normalized, current_index, candidates, previous)

        decision = result.decision.value
        if decision == "CONFIRMED" and result.segment_id is not None:
            self._previous_alignment_order[session_id] = self._order_of(
                candidates, result.segment_id
            )
            self._metrics.incr("bayaan_alignment_confirmed_total")
            return result.score, result.segment_id
        if decision == "WAIT":
            self._metrics.incr("bayaan_alignment_wait_total")
        else:
            self._metrics.incr("bayaan_alignment_fallback_total")
        return result.score, None

    @staticmethod
    def _order_of(candidates: list[AlignmentCandidate], segment_id: str) -> int:
        for candidate in candidates:
            if candidate.id == segment_id:
                return candidate.order
        return 0

    async def _process_utterance(self, session_id, sequence, repo, db, pcm,
                                 room_id=None, sermon_version_id=None):
        self._metrics.incr("bayaan_asr_calls_total")
        start = time.perf_counter()
        try:
            asr_result = await self._asr.transcribe(
                pcm, self._settings.languages.source_language
            )
        except ProviderUnavailableError as exc:
            # §195/§87 SAFE_UNAVAILABLE: every ASR provider failed. §58/§178:
            # the original audio service continues; public clients see the
            # §134 fallback and the pipeline stays alive for the next frame.
            await self._asr_unavailable(
                session_id, sequence, db, room_id, exc
            )
            return None
        self._metrics.observe(
            "bayaan_asr_latency_ms", (time.perf_counter() - start) * 1000
        )
        self._heartbeat("asr")

        # Shared post-ASR path (also used by the operator "Send a test line").
        return await self._emit_transcript(
            session_id, sequence, repo, db, asr_result.text,
            asr_result.confidence, room_id=room_id,
            sermon_version_id=sermon_version_id,
        )

    async def _emit_transcript(self, session_id, sequence, repo, db, arabic,
                               asr_confidence: float | None, room_id=None,
                               sermon_version_id=None):
        """Shared path from a transcript to a broadcast caption (§190/§55).

        normalize -> §78 router (prepared/scripture) -> §77 align -> persist
        LiveSegment -> §79 validate -> §95/§167 gate -> broadcast caption ->
        §80 TTS enqueue. Both the audio ingest path (``_process_utterance``) and
        the operator "Send a test line" path (``ingest_text``) run through THIS,
        so a demo line and a real utterance go through the identical safety and
        translation machinery - there is no second, weaker path (§0 rule 5).
        """
        normalized = self._normalizer.normalize(arabic)
        if not normalized:
            self._metrics.incr("bayaan_empty_transcripts_total")
            return None

        # §78 router: exact normalized match against the PUBLISHED prepared
        # version links the segment; the approved draft (per language) wins
        # over live MT in _caption_for_language.
        prepared_segment_id = None
        if sermon_version_id is not None:
            from bayaan_server.db.repository import SermonRepository
            from bayaan_server.sermon.retrieval import PreparedRetrieval

            hit = await PreparedRetrieval(SermonRepository(db)).find_segment(
                arabic, sermon_version_id
            )
            if hit is not None:
                prepared_segment_id = hit.id
                self._metrics.incr("bayaan_prepared_match_total")

        # §77 alignment: a distinct confidence signal, never the ASR score.
        alignment_score, aligned_id = await self._alignment_confidence(
            session_id, normalized, sermon_version_id, db, sequence
        )
        # A confirmed fuzzy alignment can link a segment the exact match missed.
        if prepared_segment_id is None and aligned_id is not None:
            import uuid as _uuid

            prepared_segment_id = _uuid.UUID(aligned_id)

        content_type = self._detector.detect(normalized)
        segment = LiveSegment(
            session_id=session_id,
            sequence=sequence,
            raw_arabic=arabic,
            normalized_arabic=normalized,
            content_type=content_type,
            source_segment_id=prepared_segment_id,
            # BUG-013: DB column is non-null; persist 0.0 as the absent
            # sentinel while the safety composite receives None (excluded).
            asr_confidence=asr_confidence if asr_confidence is not None else 0.0,
            # Persisted as 0.0 when alignment did not run (absent); the DB
            # column is non-null. The §167 composite uses the None form below
            # so absent alignment is excluded, not counted as zero (§166).
            alignment_confidence=alignment_score or 0.0,
        )
        # BUG-018: concurrent ingests can race next_sequence; the UNIQUE
        # constraint rejects the loser — retry once with a fresh sequence.
        try:
            await repo.create(segment)
        except Exception:
            await db.rollback()
            sequence = await repo.next_sequence(session_id)
            segment.sequence = sequence
            await repo.create(segment)

        if room_id is not None and self._broker is not None:
            event, speakable = await self._build_caption_event(
                session_id, sequence, normalized, content_type,
                asr_confidence, db,
                prepared_segment_id=prepared_segment_id,
                alignment_score=alignment_score,
            )
            await self._broker.publish_caption(room_id, event)
            # §194 Phase 5: hand the §80-approved candidates to the TTS service.
            # It re-gates with the real age at dequeue, so nothing unsafe/stale
            # is ever spoken (§80/§88). No-op when TTS is locked (§201).
            if self._tts_service is not None:
                for candidate in speakable:
                    await self._tts_service.enqueue_for_room(
                        room_id, sequence, candidate
                    )
        return segment

    async def ingest_text(
        self, session_id: uuid.UUID, session_factory, arabic: str,
        asr_confidence: float = 0.99,
    ) -> LiveSegment | None:
        """Operator "Send a test line" (§200): drive a scripted utterance
        through the REAL pipeline WITHOUT a microphone or an ASR backend, so
        captions + live translation appear on the room page for a demo or when
        troubleshooting. It reuses the identical normalize -> route -> translate
        -> validate -> safety-gate -> broadcast + TTS path as live audio
        (``_emit_transcript``); it does NOT bypass any safety gate (§0 rule 5).

        Requires a LIVE session, mirroring the /ingest endpoint (§190). The
        caller-supplied ``asr_confidence`` defaults high (0.99) so a demo line
        is not held back by a missing ASR score; every downstream gate still
        applies on the real translation confidence.
        """
        from bayaan_server.db.repository import (
            LiveSessionRepository,
            LiveSegmentRepository,
        )
        from bayaan_server.domain.enums import SessionStatus

        async with session_factory() as db:
            session_row = await LiveSessionRepository(db).get(session_id)
            if session_row is None:
                raise ValueError("session not found")
            if session_row.status is not SessionStatus.LIVE:
                raise ValueError(
                    "session must be LIVE to send a line (current: "
                    f"{session_row.status.value})"
                )
            room_id = session_row.room_id
            sermon_version_id = session_row.sermon_version_id
            repo = LiveSegmentRepository(db)
            chunks = self._segmenter.segment(arabic)
            if not chunks:
                chunks = [arabic]

            last_segment = None
            for chunk in chunks:
                sequence = await repo.next_sequence(session_id)
                last_segment = await self._emit_transcript(
                    session_id, sequence, repo, db, chunk, asr_confidence,
                    room_id=room_id, sermon_version_id=sermon_version_id,
                )
            await db.commit()
        return last_segment

    async def _build_caption_event(self, session_id, sequence, arabic,
                                   content_type, asr_confidence, db,
                                   prepared_segment_id=None,
                                   alignment_score=0.0):
        targets: dict[str, dict] = {}
        speakable: list[TtsCandidate] = []
        for lang in self._settings.languages.target_languages:
            target, candidate = await self._caption_for_language(
                session_id, sequence, arabic, asr_confidence, lang, db,
                content_type=content_type,
                prepared_segment_id=prepared_segment_id,
                alignment_score=alignment_score,
            )
            targets[lang] = target
            if candidate is not None:
                speakable.append(candidate)
        event = {
            "type": "caption",
            "sequence": sequence,
            "content_type": content_type.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "arabic": arabic,
            "targets": targets,
        }
        return event, speakable

    async def _caption_for_language(self, session_id, sequence, arabic,
                                    asr_confidence, lang, db,
                                    content_type=None, prepared_segment_id=None,
                                    alignment_score=0.0) -> dict:
        """§78/§86 route -> §79 validate -> §95/§134 gate one target language.

        Order (never violated):
          1. prepared approved translation (exact or §77-confirmed) wins;
          2. QURAN/HADITH route through the scripture store (approved ref, or
             block / non-attributed fallback - Quran is never machine-translated);
          3. otherwise live MT;
        then the §79 validator runs and the §95 safety/TTS gate decides what
        the public client sees (§134: unsafe -> "Translation unavailable")."""
        from bayaan_server.domain.enums import ContentType
        from bayaan_server.db.repository import GlossaryRepository, SermonRepository

        ctype = content_type or ContentType.GENERAL
        route = "general_mt"
        provider_confidence: float | None = None
        metadata: dict = {}
        text = ""

        # (1) prepared approved translation wins over any live path (§78/§1.2).
        if prepared_segment_id is not None:
            draft = await SermonRepository(db).get_approved_draft(
                prepared_segment_id, lang
            )
            if draft is not None:
                self._metrics.incr("bayaan_prepared_reuse_total")
                route = "prepared_approved"
                text = draft.text
                # Human-approved text is trusted; keep a high confidence so it
                # still passes the gate - the approved reference is what matters
                # for sacred content, not an MT score. Fixed 0.99 (never the
                # mock tuning knob, so ollama/libre chains behave the same).
                provider_confidence = max(
                    0.99,
                    self._settings.safety.translation_min_confidence,
                )

        # (2)+(3): execute the provider-backed paths. A §87 SAFE_UNAVAILABLE
        # (every provider in the translation chain failed, §195) degrades to
        # the §134 public fallback for THIS language while the pipeline keeps
        # serving every other language/reason path (§57/§58: no crash, no
        # fabricated output).
        try:
            if route == "general_mt" and ctype in (ContentType.QURAN, ContentType.HADITH):
                from bayaan_server.pipeline.translation_router import TranslationRouter

                matcher = await self._scripture_matcher(db)
                router = TranslationRouter(matcher, self._settings.scripture)
                if ctype is ContentType.QURAN:
                    decision = await router.route_quran(arabic, lang)
                else:
                    decision = await router.route_hadith(arabic, lang, self._translation)
                route = decision.route
                metadata["approved_reference"] = decision.approved_reference
                metadata["verification_level"] = decision.verification_level
                if decision.blocked:
                    blocked = await self._blocked_caption(
                        session_id, sequence, lang, "scripture_unverified", db
                    )
                    return blocked, None
                text = decision.text

            # (3) general live MT for everything else.
            if route == "general_mt":
                translation = await self._translation.translate(
                    arabic, self._settings.languages.source_language, lang
                )
                text = translation.text
                provider_confidence = translation.confidence
        except ProviderUnavailableError as exc:
            self._metrics.incr("bayaan_translation_unavailable_total")
            self._degrade("translation_unavailable")
            if self._alerts is not None:
                self._alerts.emit(
                    "translation_unavailable",
                    "error",
                    f"translation chain unavailable for language '{lang}' (§87)",
                    capability="translation",
                    language=lang,
                    error=str(exc)[:300],
                )
            await AuditLogRepository(db).record(
                AuditAction.DEGRADED,
                target_id=session_id,
                metadata={
                    "sequence": sequence,
                    "lang": lang,
                    "capability": "translation",
                    "reason": str(exc)[:200],
                },
            )
            # §134/§57: the tuple contract must hold on the outage path too -
            # callers unpack (target, tts_candidate). Returning the dict alone
            # here used to crash the whole ingest with a ValueError.
            return (
                await self._blocked_caption(
                    session_id, sequence, lang, "translation_unavailable", db
                ),
                None,
            )
        self._heartbeat("translation")

        # §79 validator chain (number/negation/entity/glossary/scripture...).
        glossary_terms = await GlossaryRepository(db).list_terms(approved_only=True)
        verdict: ValidationVerdict = self._validator.validate(
            arabic, text, lang,
            content_type=ctype.value,
            metadata=metadata,
            glossary_terms=glossary_terms,
        )
        self._metrics.incr(f"bayaan_validation_{verdict.decision.value.lower()}_total")
        for warning in verdict.warnings:
            self._metrics.incr(f"bayaan_validation_warning_{warning.code}_total")

        # §166: translation confidence is its own signal, refined by the §79
        # validation outcome (§167) - never the raw provider number.
        tconf = translation_confidence(provider_confidence, verdict)
        breakdown = ConfidenceBreakdown(
            asr_confidence=asr_confidence,
            alignment_confidence=(
                alignment_score if self._settings.safety.alignment_enabled else None
            ),
            translation_confidence=tconf,
            validation_score=(
                (1.0 - verdict.risk)
                if self._settings.safety.validation_enabled
                else None
            ),
        )
        gate = evaluate_safety(breakdown, self._settings.safety)
        self._metrics.incr("bayaan_captions_total")

        # §95/§167: a validator BLOCK or a safety non-ALLOW never broadcasts.
        if (
            verdict.decision is SafetyDecision.BLOCK
            or gate.decision is not SafetyDecision.ALLOW
        ):
            reasons = [w.code for w in verdict.warnings] + list(gate.reasons)
            blocked = await self._blocked_caption(
                session_id, sequence, lang, "|".join(reasons) or gate.decision.value, db
            )
            return blocked, None

        # §80/§95: build the TTS candidate and run the full TTS gate. TTS is a
        # strictly stricter path than text (§95): a caption may be shown while
        # being refused for speech (low confidence, sacred text without an
        # approved reference, etc). age_ms is 0 here - the segment is fresh;
        # the queue worker re-gates with the real age before speaking (§80).
        candidate = TtsCandidate(
            text=text,
            language=lang,
            content_type=ctype,
            safety_decision=gate.decision,
            validation_decision=verdict.decision,
            translation_confidence=breakdown.translation_confidence,
            age_ms=0,
            approved_reference=metadata.get("approved_reference"),
        )
        speak = allow_tts(
            candidate, self._settings.safety, self._settings.tts
        ).speak

        target = {
            "text": text,
            "state": "ok",
            "route": route,
            # §95: the §80 TTS gate, not the looser text gate, decides speech.
            "speak": speak,
        }
        return target, (candidate if speak else None)

    async def _blocked_caption(self, session_id, sequence, lang, reason, db) -> dict:
        """§134/§98/§168: unsafe output becomes the public fallback message;
        operator-facing reasons go to the audit log, never to the client."""
        self._metrics.incr("bayaan_safety_blocks_total")
        await AuditLogRepository(db).record(
            AuditAction.SAFETY_BLOCK,
            target_id=session_id,
            metadata={"sequence": sequence, "lang": lang, "reason": reason},
        )
        return {
            "text": PUBLIC_FALLBACK_MESSAGE,
            "state": "unavailable",
            "speak": False,
        }

    # -- §195 health/alerts helpers -----------------------------------------
    def _heartbeat(self, capability: str) -> None:
        """Record a fresh capability success for the §111 watchdog."""
        if self._health is not None:
            self._health.heartbeat(capability)

    def _degrade(self, reason: str) -> None:
        """§57: mark the service degraded with an explainable reason."""
        if self._health is not None:
            self._health.set_degraded(reason)

    def _unavailable_event(self, sequence: int, arabic: str) -> dict:
        """§134/§178: every target shows the public fallback for an outage."""
        targets = {
            lang: {"text": PUBLIC_FALLBACK_MESSAGE, "state": "unavailable", "speak": False}
            for lang in self._settings.languages.target_languages
        }
        return {
            "type": "caption",
            "sequence": sequence,
            "content_type": ContentType.GENERAL.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "arabic": arabic,
            "targets": targets,
        }

    async def _asr_unavailable(
        self, session_id, sequence, db, room_id, exc: ProviderUnavailableError
    ) -> None:
        """§58/§178: ASR is gone - the original audio continues, phones see the
        public fallback, the operator is alerted and the service is degraded.
        The pipeline itself keeps running: no crash, no fabricated text."""
        self._metrics.incr("bayaan_asr_unavailable_total")
        self._degrade("asr_unavailable")
        if self._alerts is not None:
            self._alerts.emit(
                "asr_unavailable",
                "error",
                f"ASR chain unavailable at sequence {sequence} (§87)",
                capability="asr",
                sequence=sequence,
                error=str(exc)[:300],
            )
        if room_id is not None and self._broker is not None:
            await AuditLogRepository(db).record(
                AuditAction.DEGRADED,
                target_id=session_id,
                metadata={"sequence": sequence, "capability": "asr",
                          "reason": str(exc)[:200]},
            )
            await self._broker.publish_caption(
                room_id, self._unavailable_event(sequence, "")
            )