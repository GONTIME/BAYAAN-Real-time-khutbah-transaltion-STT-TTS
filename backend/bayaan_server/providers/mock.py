"""Mock providers (§183 contracts-first; §54/§87/§195 Phase 6 failover).

Mocks are deterministic, safe, and clearly marked so nothing fake can be
mistaken for real output (§0 rule 8: fail safe, never guess).

``build_providers`` returns a failover-wrapped ``ProviderSet`` (§87/§54): the
app depends only on the provider protocols and the runner chain delivers
primary -> fallback failover, per-provider circuit breakers and SAFE_UNAVAILABLE
when every provider in a chain is exhausted.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bayaan_server.config import Settings
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.providers.errors import ProviderOutageError
from .interfaces import AsrResult, TtsResult, TranslationResult

MOCK_PROVIDER_NAME = "mock"


class MockASRProvider:
    """Echoes the caller-provided transcript; confidence configurable in tests."""

    def __init__(
        self,
        settings: Settings,
        confidence: float | None = None,
        fail_always: bool | None = None,
    ) -> None:
        self._settings = settings
        self._confidence = (
            confidence
            if confidence is not None
            else settings.providers.mock_asr_confidence
        )
        # §195: failure injection is configuration, never business logic.
        self._fail_always = (
            settings.providers.mock_asr_fail_always
            if fail_always is None
            else fail_always
        )

    async def transcribe(self, audio: bytes, source_language: str) -> AsrResult:
        if self._fail_always:
            raise ProviderOutageError(MOCK_PROVIDER_NAME, "simulated ASR outage (§195)")
        # audio content is intentionally NOT interpreted: a mock must not
        # pretend to understand speech. The configured override text exists
        # only so the prepared-sermon retrieval path (§78) can be exercised
        # end-to-end (Phase 3 acceptance); empty in production.
        scripted = self._settings.providers.mock_asr_text
        return AsrResult(
            text=scripted if scripted else "[MOCK-ASR-UNAVAILABLE]",
            language=source_language,
            confidence=self._confidence,
        )


class MockTranslationProvider:
    """Wraps the source text with an explicit marker; no invented content."""

    def __init__(
        self,
        settings: Settings,
        confidence: float | None = None,
        fail_always: bool | None = None,
    ) -> None:
        self._settings = settings
        self._confidence = (
            confidence
            if confidence is not None
            else settings.providers.mock_translation_confidence
        )
        # §195: failure injection is configuration, never business logic.
        self._fail_always = (
            settings.providers.mock_translation_fail_always
            if fail_always is None
            else fail_always
        )

    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> TranslationResult:
        if self._fail_always:
            raise ProviderOutageError(MOCK_PROVIDER_NAME, "simulated translation outage (§195)")
        return TranslationResult(
            text=f"[MOCK-TRANSLATION:{target_language}] {text}",
            source_language=source_language,
            target_language=target_language,
            confidence=self._confidence,
            provider=MOCK_PROVIDER_NAME,
            model="mock-model",
            prompt_version="pv0",
            glossary_version="gv0",
            retrieval_version="rv0",
            created_at=datetime.now(timezone.utc).isoformat(),
        )


class MockTTSProvider:
    """Deterministic silent-PCM adapter (§194). Never produces real speech.

    Emits 16-bit mono silence whose length is a bounded heuristic of the text
    (roughly words * a per-word duration, capped by ``max_audio_seconds``), so
    the audio client can be exercised end-to-end without a TTS backend. The
    bytes are unmistakably silence (all zero) - a mock must not pretend to
    speak (§0 rule 8). Real adapters implement the same ``synthesize`` port.
    """

    # Rough speaking-rate heuristic (§185: tunable, not a magic inline value).
    _MS_PER_CHAR = 55.0

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    async def synthesize(self, text: str, target_language: str) -> TtsResult:
        sample_rate = 24000
        max_seconds = 12.0
        if self._settings is not None:
            sample_rate = self._settings.tts.sample_rate
            max_seconds = self._settings.tts.max_audio_seconds
        words = max(1, len(text.split()))
        # ~55ms/char is too coarse; approximate from characters but keep a
        # sensible per-utterance floor and the configured cap.
        est_ms = min(len(text) * self._MS_PER_CHAR, max_seconds * 1000.0)
        est_ms = max(est_ms, words * 120.0)
        duration_ms = int(min(est_ms, max_seconds * 1000.0))
        n_samples = int(sample_rate * duration_ms / 1000)
        audio = b"\x00\x00" * n_samples  # 16-bit signed silence
        return TtsResult(
            audio=audio,
            audio_format="pcm_s16le",
            voice="mock-voice",
            sample_rate=sample_rate,
            duration_ms=duration_ms,
        )


# Re-export build_providers from registry for backwards compatibility
def build_providers(*args, **kwargs):
    from bayaan_server.providers.registry import build_providers as _build
    return _build(*args, **kwargs)