"""Audio sources (BAYAAN_MASTER_SPEC.md §190 audio capture).

§186 applied to capture: no hidden I/O - every source is an explicit,
configurable adapter. DeviceAudioSource is the real mixer path; it lives
behind an optional dependency so tests and CI never require audio hardware.
"""

from __future__ import annotations

import io
import queue
import wave
from typing import AsyncIterator, Protocol

from bayaan_server.config import Settings
from .format import PcmFormat


class AudioSource(Protocol):
    def frames(self) -> AsyncIterator[bytes]:
        """Yield fixed-size int16 PCM frames in the configured format."""
        ...


class AudioFormatError(Exception):
    """Raised when input audio does not match the configured format (§184:
    never silently resample or reinterpret)."""


class FileAudioSource:
    """Reads a WAV file (mixer recording / golden dataset) as a frame stream."""

    def __init__(self, wav_bytes: bytes, settings: Settings) -> None:
        self._settings = settings
        self._wav_bytes = wav_bytes

    async def frames(self) -> AsyncIterator[bytes]:
        fmt = PcmFormat(
            sample_rate=self._settings.audio.sample_rate,
            channels=self._settings.audio.channels,
        )
        pcm = parse_wav(self._wav_bytes, fmt)
        size = fmt.frame_bytes
        for offset in range(0, len(pcm) - size + 1, size):
            yield pcm[offset : offset + size]


class DeviceAudioSource:
    """Captures from the configured input device (digital mixer line-out).

    Requires the optional `sounddevice` extra. Failures surface as
    AudioFormatError/ImportError - preflight (§184) checks this first.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._fmt = PcmFormat(
            sample_rate=settings.audio.sample_rate,
            channels=settings.audio.channels,
        )

    async def frames(self) -> AsyncIterator[bytes]:
        try:
            import sounddevice as sd  # optional dependency
        except ImportError as exc:  # pragma: no cover - depends on env
            raise AudioFormatError(
                "sounddevice is not installed; install bayaan-server[audio] "
                "or use FileAudioSource"
            ) from exc

        frames_q: queue.Queue[bytes] = queue.Queue()

        def callback(indata, frames, time_info, status) -> None:  # type: ignore[no-untyped-def]
            frames_q.put(bytes(indata))

        with sd.RawInputStream(
            samplerate=self._fmt.sample_rate,
            blocksize=self._fmt.frame_samples,
            channels=self._fmt.channels,
            dtype="int16",
            callback=callback,
        ):
            while True:
                try:
                    data = await _get_async(frames_q)
                except queue.Empty:
                    continue
                yield data


async def _get_async(q: queue.Queue) -> bytes:
    import asyncio

    return await asyncio.to_thread(q.get, True, 0.2)


def parse_wav(wav_bytes: bytes, fmt: PcmFormat) -> bytes:
    """Validate WAV format against configuration and return raw PCM.

    §184: a mismatch is a loud error, never a silent conversion.
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sample_rate = w.getframerate()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        if sample_rate != fmt.sample_rate or channels != fmt.channels or sampwidth != 2:
            raise AudioFormatError(
                f"WAV format mismatch: got {sample_rate}Hz/{channels}ch/"
                f"{sampwidth * 8}bit, expected {fmt.sample_rate}Hz/"
                f"{fmt.channels}ch/16bit"
            )
        return w.readframes(w.getnframes())