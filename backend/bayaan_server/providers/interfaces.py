"""Provider ports (BAYAAN_MASTER_SPEC.md §0 rule 4, §63, §69, §183).

Business logic depends ONLY on these interfaces. Concrete providers
(cloud or local) are adapters; all outbound network calls live inside
adapters, never in business logic (§186).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class AsrResult(BaseModel):
    """Output of one ASR call (§166: ASR confidence is its own signal).

    ``confidence`` is None when the provider reports none (BUG-013: never
    fabricate). Mirrors ``TranslationResult.confidence``.
    """

    text: str
    language: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class TranslationResult(BaseModel):
    """Translation output with mandatory provenance (§69).

    ``confidence`` is the provider's own score when it reports one, else
    ``None`` (absent != 0.0, §0 rule 8 / §166). Adapters that emit no score
    (mock/libre/ollama) leave it ``None``; the live path gates confidence
    separately (§80/§166) and the draft path is human-reviewed (§96).
    """

    text: str
    source_language: str
    target_language: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    provider: str
    model: str
    prompt_version: str
    glossary_version: str
    retrieval_version: str
    created_at: str


class TtsResult(BaseModel):
    """§194 TTS adapter output.

    ``audio`` is raw PCM bytes for the target ``audio_format``. The audio
    client needs ``sample_rate`` to play it back at the correct pitch, and
    ``duration_ms`` so it can schedule/drop playback. A provider that cannot
    produce audio MUST return empty bytes rather than fake speech (§0 rule 8).
    """

    audio: bytes
    audio_format: str
    voice: str
    sample_rate: int = 24000
    duration_ms: int = 0


@runtime_checkable
class ASRProvider(Protocol):
    async def transcribe(self, audio: bytes, source_language: str) -> AsrResult:
        """Transcribe one audio chunk."""
        ...


@runtime_checkable
class TranslationProvider(Protocol):
    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> TranslationResult:
        """Translate one normalized segment. Must never invent citations
        (§71 prompt policy is enforced inside adapters, not business logic)."""
        ...


@runtime_checkable
class TTSProvider(Protocol):
    async def synthesize(self, text: str, target_language: str) -> TtsResult:
        """Synthesize speech. Callers MUST have passed the TTS gate first
        (§95: unsafe output never becomes TTS)."""
        ...