"""Whisper ASR adapter for OpenAI, Groq, and OpenAI-compatible endpoints (§63).

Receives raw PCM 16kHz mono audio, packages it into a WAV file in-memory,
and calls the transcriptions API endpoint with language="ar".
"""

from __future__ import annotations

import io
import wave
import httpx

from bayaan_server.config import Settings
from bayaan_server.logging_setup import get_logger
from bayaan_server.providers.errors import ProviderOutageError
from bayaan_server.providers.interfaces import AsrResult

logger = get_logger(__name__)
PROVIDER_NAME = "whisper"
DEFAULT_WHISPER_PROMPT = (
    "خطبة جمعة باللغة العربية الفصحى، تحتوي على آيات قرآنية وأحاديث نبوية شريفة "
    "وعبارات مثل: الحمد لله، صلى الله عليه وسلم، رضي الله عنه، أما بعد، عباد الله."
)


class WhisperASRProvider:
    """ASR adapter sending audio to OpenAI / Groq / Whisper-compatible API."""

    def __init__(self, settings: Settings) -> None:
        self.name = PROVIDER_NAME
        self._settings = settings
        cfg = settings.providers
        self._endpoint = cfg.whisper_endpoint.rstrip("/")
        self._api_key = cfg.whisper_api_key.strip()
        self._model = cfg.whisper_model
        self._timeout = cfg.whisper_timeout_seconds

    async def transcribe(self, audio: bytes, source_language: str = "ar") -> AsrResult:
        if not audio:
            return AsrResult(text="", language=source_language, confidence=1.0)

        # Build in-memory WAV file from raw PCM frames (16kHz, 16-bit, mono)
        wav_buf = io.BytesIO()
        channels = self._settings.audio.channels
        sample_rate = self._settings.audio.sample_rate
        sample_width = 2  # 16-bit

        try:
            with wave.open(wav_buf, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(sample_width)
                wf.setframerate(sample_rate)
                wf.writeframes(audio)
            wav_bytes = wav_buf.getvalue()
        except Exception as exc:
            logger.error("Failed to package PCM to WAV: %s", exc)
            raise ProviderOutageError(PROVIDER_NAME, f"audio packaging error: {exc}") from exc

        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        files = {
            "file": ("audio.wav", wav_bytes, "audio/wav"),
        }
        data = {
            "model": self._model,
            "language": source_language,
            "prompt": DEFAULT_WHISPER_PROMPT,
            "temperature": "0.0",
            "response_format": "json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._endpoint,
                    headers=headers,
                    files=files,
                    data=data,
                )
                if resp.status_code != 200:
                    err_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    logger.warning("Whisper ASR returned error: %s", err_msg)
                    raise ProviderOutageError(PROVIDER_NAME, err_msg)

                body = resp.json()
                text = body.get("text", "").strip()
                # BUG-013: the transcription API returns no confidence;
                # never fabricate 0.95. Absent (None) is excluded from the
                # composite, not counted as quality.
                return AsrResult(
                    text=text,
                    language=source_language,
                    confidence=None,
                )
        except httpx.RequestError as exc:
            logger.warning("Whisper ASR connection failed: %s", exc)
            raise ProviderOutageError(PROVIDER_NAME, f"connection failed: {exc}") from exc
