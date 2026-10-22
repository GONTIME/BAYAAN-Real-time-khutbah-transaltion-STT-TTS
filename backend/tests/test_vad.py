"""VAD tests (§190): utterance assembly, min/max length, blip rejection."""

from __future__ import annotations

import struct

from bayaan_server.audio.format import PcmFormat
from bayaan_server.audio.vad import EnergyVAD
from bayaan_server.config import VadSettings

FMT = PcmFormat(sample_rate=16000, channels=1, frame_ms=20)


def frame(amplitude: int) -> bytes:
    return b"".join(struct.pack("<h", amplitude) for _ in range(FMT.frame_samples))


def frames(count: int, amplitude: int) -> list[bytes]:
    return [frame(amplitude) for _ in range(count)]


def feed_all(vad: EnergyVAD, fs: list[bytes]) -> list:
    out = []
    for f in fs:
        result = vad.feed(f)
        if result is not None:
            out.append(result)
    return out


def test_speech_then_silence_yields_one_utterance() -> None:
    vad = EnergyVAD(VadSettings(), FMT)
    # 200ms silence, 500ms speech (RMS=3000), 1000ms silence
    results = feed_all(vad, frames(10, 0) + frames(25, 3000) + frames(50, 0))
    assert len(results) == 1
    assert results[0].duration_ms >= VadSettings().min_utterance_ms


def test_trailing_silence_is_trimmed() -> None:
    vad = EnergyVAD(VadSettings(), FMT)
    results = feed_all(vad, frames(10, 0) + frames(25, 3000) + frames(50, 0))
    # 500ms speech + ~120ms pad, trailing 1000ms silence must be cut
    assert results[0].duration_ms < 800


def test_silence_only_yields_nothing_even_after_flush() -> None:
    vad = EnergyVAD(VadSettings(), FMT)
    assert feed_all(vad, frames(100, 0)) == []
    assert vad.flush() is None


def test_blip_below_min_utterance_is_discarded() -> None:
    vad = EnergyVAD(VadSettings(), FMT)
    # 200ms speech (< 400ms min), then silence to close
    results = feed_all(vad, frames(10, 0) + frames(10, 3000) + frames(60, 0))
    assert results == []


def test_max_utterance_cut_keeps_latency_bounded() -> None:
    vad = EnergyVAD(VadSettings(max_utterance_ms=1000), FMT)
    results = feed_all(vad, frames(200, 3000))  # 4s continuous speech
    assert len(results) >= 3  # cut roughly every 1s


def test_flush_closes_trailing_speech() -> None:
    vad = EnergyVAD(VadSettings(), FMT)
    results = feed_all(vad, frames(10, 0) + frames(25, 3000))
    assert results == []
    flushed = vad.flush()
    assert flushed is not None and flushed.duration_ms >= 400