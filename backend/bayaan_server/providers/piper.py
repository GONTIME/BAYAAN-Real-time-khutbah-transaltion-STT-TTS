"""Piper / Sherpa-ONNX local TTS provider (BAYAAN_MASTER_SPEC.md §194, §186).

Runs open Piper-format voices fully offline on CPU through a local ``piper``
(or sherpa-onnx) CLI:

    piper --model <voice.onnx> --config <voice.onnx.json> --output_raw

The CLI writes raw 16-bit signed little-endian mono PCM to stdout. Voices are
per target language (config), so a mosque picks a voice per language with no
vendor, no key, no internet and no GPU. Hardware footprint is tiny (CPU-only,
well under 0.5 GB RAM, ~20-60 MB per voice).

Failure mirrors every other adapter (§87/§186): a missing binary or voice file
raises ``ProviderUnavailableError``, so the failover runner reports
SAFE_UNAVAILABLE and the room falls back to §201 text-first captions instead of
hard-failing. ``min_timeout_ms`` gives the pipeline a budget floor so a cold
model load is never mistaken for an outage (§87 §185).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bayaan_server.config import Settings
from bayaan_server.providers.errors import ProviderUnavailableError
from .interfaces import TtsResult

PIPER_PROVIDER_NAME = "piper"

# Piper emits raw 16-bit signed little-endian PCM. The voice's onnx.json
# declares the real sample rate; this is the fallback when the config is absent.
DEFAULT_SAMPLE_RATE = 22050

# Target-language -> config field holding that language's voice name.
_LANG_VOICE_FIELDS = {
    "ar": "piper_voice_ar",
    "it": "piper_voice_it",
    "en": "piper_voice_en",
}

# §87 §185: never let the failover budget cut off a cold piper model load.
MIN_TIMEOUT_MS = 30_000


async def _run_piper(exec_path: str, args: list[str], stdin: str) -> bytes:
    """Execute the piper CLI; returns stdout bytes (raw PCM). Raises on failure."""
    proc = await asyncio.create_subprocess_exec(
        exec_path,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode("utf-8")), timeout=MIN_TIMEOUT_MS)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise ProviderUnavailableError("tts", exc) from exc
    if proc.returncode != 0:
        tail = (err or b"").decode("utf-8", "replace")[:200]
        raise ProviderUnavailableError(
            "tts", RuntimeError(f"piper exit {proc.returncode}: {tail}")
        )
    return out


class PiperTTSProvider:
    """§194: synthesize speech with Piper-format voices via the local CLI."""

    min_timeout_ms = MIN_TIMEOUT_MS

    def __init__(self, settings: Settings, *, runner=None) -> None:
        self._settings = settings
        # Injectable async runner for hermetic tests:
        # runner(exec_path, args, stdin) -> bytes. Defaults to the real CLI.
        self._runner = runner or _run_piper

    # -- voice resolution (also used by the §184 preflight probe) ----------
    def voice_for(self, language: str) -> str:
        """The configured voice name for a target language ('' if none)."""
        field = _LANG_VOICE_FIELDS.get(language)
        if field is None:
            return ""
        return getattr(self._settings.providers, field)

    def resolve_paths(self, voice: str) -> tuple[Path, Path | None]:
        """Resolve (model.onnx, model.onnx.json) for a voice, honouring the
        configured voice dir or falling back to the working directory."""
        cfg = self._settings.providers
        base = Path(cfg.piper_voice_dir) if cfg.piper_voice_dir else Path(".")
        model = base / f"{voice}.onnx"
        config = base / f"{voice}.onnx.json"
        return model, (config if config.is_file() else None)

    def _sample_rate_for(self, model: Path, config: Path | None) -> int:
        if config is not None:
            try:
                data = json.loads(config.read_text(encoding="utf-8"))
                rate = int(data.get("audio", {}).get("sample_rate", 0))
                if rate > 0:
                    return rate
            except Exception:  # noqa: BLE001 - bad/missing config -> fall back
                pass
        return DEFAULT_SAMPLE_RATE

    async def synthesize(self, text: str, target_language: str) -> TtsResult:
        providers = self._settings.providers
        exec_path = providers.piper_exec
        voice = self.voice_for(target_language)
        if not voice:
            raise ProviderUnavailableError(
                "tts",
                RuntimeError(f"no piper voice configured for language '{target_language}'"),
            )
        model, config = self.resolve_paths(voice)
        if not model.is_file():
            raise ProviderUnavailableError(
                "tts", RuntimeError(f"piper voice model not found: {model}")
            )
        args = ["--model", str(model)]
        if config is not None:
            args += ["--config", str(config)]
        args.append("--output_raw")
        out = await self._runner(exec_path, args, text)
        if not out:
            raise ProviderUnavailableError("tts", RuntimeError("piper produced no audio"))
        sample_rate = self._sample_rate_for(model, config)
        duration_ms = int(len(out) / 2 / sample_rate * 1000)
        return TtsResult(
            audio=out,
            audio_format="pcm_s16le",
            voice=voice,
            sample_rate=sample_rate,
            duration_ms=duration_ms,
        )
