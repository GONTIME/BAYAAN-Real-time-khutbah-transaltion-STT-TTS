"""TTS service tests (BAYAAN_MASTER_SPEC.md §194 Phase 5, §80, §95, §201).

The acceptance criterion for Phase 5 is "unsafe outputs never reach TTS".
These tests prove it with a spy provider: any call to ``synthesize`` is an
unsafe leak, so the spy records every call and the tests assert it is only
ever called for §80-approved, non-stale captions - and never when TTS is off.
"""

from __future__ import annotations

import asyncio
import uuid

from bayaan_server.config import SafetySettings, Settings, TtsSettings
from bayaan_server.domain.enums import ContentType, SafetyDecision
from bayaan_server.domain.tts import TtsCandidate
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.pipeline.tts_service import TtsService
from bayaan_server.providers.interfaces import TtsResult

ROOM = uuid.uuid4()


class SpyTTSProvider:
    """Records every synthesize call; the §80 gate must prevent unsafe ones."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text: str, target_language: str) -> TtsResult:
        self.calls.append((text, target_language))
        return TtsResult(
            audio=b"\x00\x00",
            audio_format="pcm_s16le",
            voice="spy",
            sample_rate=24000,
            duration_ms=1,
        )


class FakeBroker:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, room_id, event: dict) -> None:
        self.events.append(event)


def _candidate(**overrides) -> TtsCandidate:
    base = dict(
        text="assalaamu alaikum",
        language="en",
        content_type=ContentType.GENERAL,
        safety_decision=SafetyDecision.ALLOW,
        validation_decision=SafetyDecision.ALLOW,
        translation_confidence=0.99,
        age_ms=0,
        approved_reference=None,
    )
    base.update(overrides)
    return TtsCandidate(**base)


def _service(tts_enabled: bool, **tts_overrides) -> tuple[TtsService, SpyTTSProvider, FakeBroker]:
    settings = Settings(
        safety=SafetySettings(tts_enabled=tts_enabled),
        tts=TtsSettings(**tts_overrides),
    )
    provider = SpyTTSProvider()
    broker = FakeBroker()
    service = TtsService(settings, provider, broker, MetricsRegistry())
    return service, provider, broker


def test_disabled_service_is_inert() -> None:
    # §201 text-first: with TTS off, nothing is enqueued and nothing speaks.
    service, provider, _ = _service(tts_enabled=False)
    accepted = asyncio.run(service.enqueue_for_room(ROOM, 0, _candidate()))
    assert accepted is False
    assert len(service.queue) == 0
    assert provider.calls == []


def test_approved_caption_is_synthesized_and_broadcast() -> None:
    async def scenario() -> None:
        service, provider, broker = _service(tts_enabled=True)
        accepted = await service.enqueue_for_room(ROOM, 3, _candidate())
        assert accepted is True
        service.start()
        for _ in range(100):
            if provider.calls:
                break
            await asyncio.sleep(0.02)
        await service.stop()
        assert provider.calls == [("assalaamu alaikum", "en")]
        assert len(broker.events) == 1
        assert broker.events[0]["type"] == "audio"
        assert broker.events[0]["sequence"] == 3
        assert broker.events[0]["language"] == "en"

    asyncio.run(scenario())


def test_unsafe_caption_never_reaches_provider() -> None:
    # §80/§95: a low-confidence caption is refused at enqueue; the spy never
    # sees it. This is the acceptance criterion in one assertion.
    service, provider, _ = _service(tts_enabled=True)
    accepted = asyncio.run(
        service.enqueue_for_room(
            ROOM, 0, _candidate(translation_confidence=0.10)
        )
    )
    assert accepted is False
    assert len(service.queue) == 0
    assert provider.calls == []


def test_sacred_unapproved_never_reaches_provider() -> None:
    service, provider, _ = _service(tts_enabled=True)
    accepted = asyncio.run(
        service.enqueue_for_room(
            ROOM, 0, _candidate(content_type=ContentType.QURAN)
        )
    )
    assert accepted is False
    assert provider.calls == []


def test_stale_job_is_dropped_before_synthesis() -> None:
    # §80/§88: a job enqueued fresh but that ages past the window while waiting
    # is dropped by the worker's re-gate, never spoken.
    async def scenario() -> None:
        from bayaan_server.pipeline.tts_queue import TtsJob
        import time

        service, provider, _ = _service(tts_enabled=True, max_staleness_ms=1)
        job = TtsJob(
            room_id=str(ROOM),
            sequence=0,
            created_monotonic_ms=time.monotonic() * 1000 - 10_000,  # 10s old
            candidate=_candidate(),
        )
        service.queue.enqueue(job)
        service.start()
        for _ in range(100):
            if service.queue.dropped_stale:
                break
            await asyncio.sleep(0.02)
        await service.stop()
        assert provider.calls == []  # never spoken
        assert service.queue.dropped_stale >= 1

    asyncio.run(scenario())


def test_full_queue_drops_newest_and_never_speaks_it() -> None:
    service, provider, _ = _service(tts_enabled=True, queue_maxsize=1)
    first = asyncio.run(service.enqueue_for_room(ROOM, 0, _candidate(text="first")))
    second = asyncio.run(service.enqueue_for_room(ROOM, 1, _candidate(text="second")))
    assert first is True
    assert second is False  # full -> newest dropped
    assert service.queue.dropped_full == 1
