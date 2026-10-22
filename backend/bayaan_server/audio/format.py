"""PCM audio frame math (BAYAAN_MASTER_SPEC.md §190).

Pure-Python implementation (no numpy) sufficient for 16 kHz mono frames;
int16 PCM is the canonical internal format.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass


@dataclass(frozen=True)
class PcmFormat:
    sample_rate: int
    channels: int
    frame_ms: int = 20

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * self.channels * 2  # int16

    def duration_ms(self, byte_count: int) -> int:
        bytes_per_sample_frame = self.channels * 2
        return int(byte_count / (self.sample_rate * bytes_per_sample_frame) * 1000)


def pcm_rms(chunk: bytes) -> float:
    """Root-mean-square of int16 samples in one chunk (mono/stereo-aware)."""
    samples = array("h", chunk[: len(chunk) // 2 * 2])
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return math.sqrt(total / len(samples))


def frame_iterator(pcm: bytes, fmt: PcmFormat):
    """Yield fixed-size frames; the trailing partial frame is dropped
    (it is shorter than frame_ms and would distort VAD timing)."""
    size = fmt.frame_bytes
    for offset in range(0, len(pcm) - size + 1, size):
        yield pcm[offset : offset + size]