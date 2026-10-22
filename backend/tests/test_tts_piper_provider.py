"""Piper / Sherpa-ONNX local TTS provider tests (§194, §186, ADR-0013).

The adapter runs an open Piper-format voice through a local CLI and returns
16-bit PCM. These tests keep it hermetic with a fake runner (no binary, no
network), verify the CLI invocation + PCM math, and prove the failure mode
(missing binary/voice -> ProviderUnavailableError) plus registry/preflight
wiring.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bayaan_server.audio.preflight import check_tts_engine
from bayaan_server.config import ProviderSettings, SafetySettings, Settings
from bayaan_server.providers.errors import ProviderUnavailableError
from bayaan_server.providers.piper import PiperTTSProvider
from bayaan_server.providers.registry import build_providers


class FakePiperRunner:
    """Hermetic stand-in for the piper CLI: records the invocation and returns
    a fixed chunk of 16-bit PCM so the adapter's math is exercised end to end."""

    def __init__(self, audio: bytes = b"\x00\x80" * 22050) -> None:
        self.calls: list[tuple[str, list[str], str]] = []
        self.audio = audio

    async def __call__(self, exec_path: str, args: list[str], stdin: str) -> bytes:
        self.calls.append((exec_path, args, stdin))
        return self.audio


def _settings(**provider_overrides) -> Settings:
    return Settings(providers=ProviderSettings(tts="piper", **provider_overrides))


def _write_voice(tmp_path, voice: str, sample_rate: int = 22050) -> None:
    (tmp_path / f"{voice}.onnx").write_bytes(b"fake-model")
    (tmp_path / f"{voice}.onnx.json").write_text(
        json.dumps({"audio": {"sample_rate": sample_rate}}), encoding="utf-8"
    )


def test_voice_mapping_defaults() -> None:
    p = PiperTTSProvider(_settings())
    assert p.voice_for("ar") == "ar_JO-kareem-medium"
    assert p.voice_for("it") == "it_IT-paola-medium"
    assert p.voice_for("en") == "en_US-lessac-medium"
    assert p.voice_for("xx") == ""


def test_synthesize_runs_piper_and_returns_pcm(tmp_path) -> None:
    # 22050 samples of 16-bit PCM == exactly 1s @ 22050 Hz.
    voice = "ar_JO-kareem-medium"
    _write_voice(tmp_path, voice, sample_rate=22050)
    runner = FakePiperRunner(audio=b"\x00\x80" * 22050)
    p = PiperTTSProvider(
        _settings(piper_voice_dir=str(tmp_path), piper_voice_ar=voice),
        runner=runner,
    )

    res = asyncio.run(p.synthesize("مرحبا بالعالم", "ar"))
    assert res.audio_format == "pcm_s16le"
    assert res.sample_rate == 22050
    assert res.duration_ms == 1000
    assert res.voice == voice

    exec_path, args, stdin = runner.calls[0]
    assert exec_path == "piper"
    assert "--model" in args
    assert any(str(a).endswith(f"{voice}.onnx") for a in args)
    assert "--config" in args
    assert any(str(a).endswith(f"{voice}.onnx.json") for a in args)
    assert "--output_raw" in args
    assert stdin == "مرحبا بالعالم"


def test_synthesize_reads_sample_rate_from_voice_config(tmp_path) -> None:
    voice = "it_IT-paola-medium"
    _write_voice(tmp_path, voice, sample_rate=24000)
    p = PiperTTSProvider(
        _settings(piper_voice_dir=str(tmp_path), piper_voice_it=voice),
        runner=FakePiperRunner(audio=b"\x00\x80" * 24000),  # 1s @ 24000
    )
    res = asyncio.run(p.synthesize("Ciao a tutti", "it"))
    assert res.sample_rate == 24000
    assert res.duration_ms == 1000


def test_synthesize_missing_model_raises_unavailable(tmp_path) -> None:
    p = PiperTTSProvider(
        _settings(piper_voice_dir=str(tmp_path)), runner=FakePiperRunner()
    )
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(p.synthesize("hello", "en"))


def test_synthesize_unconfigured_language_raises() -> None:
    p = PiperTTSProvider(_settings(), runner=FakePiperRunner())
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(p.synthesize("hello", "fr"))


def test_synthesize_empty_output_raises(tmp_path) -> None:
    voice = "en_US-lessac-medium"
    _write_voice(tmp_path, voice)
    p = PiperTTSProvider(
        _settings(piper_voice_dir=str(tmp_path), piper_voice_en=voice),
        runner=FakePiperRunner(audio=b""),
    )
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(p.synthesize("hello", "en"))


def test_registry_wires_piper_as_tts_route() -> None:
    provider_set = build_providers(Settings(providers=ProviderSettings(tts="piper")))
    names = [r["provider"] for r in provider_set.runners["tts"].snapshot()]
    assert names == ["piper"]


def test_registry_default_remains_mock() -> None:
    provider_set = build_providers(Settings())
    names = [r["provider"] for r in provider_set.runners["tts"].snapshot()]
    assert names == ["mock"]


# -- §184 preflight ---------------------------------------------------------


def test_preflight_tts_locked_not_probed() -> None:
    comp = check_tts_engine(Settings(safety=SafetySettings(tts_enabled=False)))
    assert comp.status == "OK"
    assert "not probed" in comp.detail


def test_preflight_piper_missing_binary_warns(tmp_path) -> None:
    settings = Settings(
        safety=SafetySettings(tts_enabled=True),
        providers=ProviderSettings(
            tts="piper",
            piper_exec="definitely-not-a-real-binary-xyz",
            piper_voice_dir=str(tmp_path),
        ),
    )
    comp = check_tts_engine(settings)
    assert comp.status == "WARN"
    assert "binary not found" in comp.detail


def test_preflight_piper_ready(tmp_path) -> None:
    import sys

    _write_voice(tmp_path, "it_IT-paola-medium")
    _write_voice(tmp_path, "en_US-lessac-medium")
    settings = Settings(
        safety=SafetySettings(tts_enabled=True),
        providers=ProviderSettings(
            tts="piper",
            # Any real executable satisfies the existence probe; python.exe
            # is guaranteed present, unlike `piper` on PATH.
            piper_exec=sys.executable,
            piper_voice_dir=str(tmp_path),
        ),
    )
    comp = check_tts_engine(settings)
    assert comp.status == "OK"
    assert "piper ready" in comp.detail
