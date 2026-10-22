"""Energy-based Voice Activity Detection (BAYAAN_MASTER_SPEC.md §190 VAD).

Emits complete utterances (PCM byte strings). A more sophisticated VAD
(e.g. webrtcvad) can replace this behind the same contract later - the
pipeline depends on this module's API, not its internals.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from bayaan_server.config import VadSettings
from .format import PcmFormat, pcm_rms


@dataclass
class Utterance:
    pcm: bytes
    duration_ms: int


class EnergyVAD:
    """Frame-synchronous VAD with pre-speech padding and max-length cut.

    Design notes:
    - speech close: `silence_ms_to_close` of silence ends an utterance;
      trailing silence beyond the close point is trimmed.
    - blips below `min_utterance_ms` are discarded (not sent to ASR).
    - long continuous speech is cut at `max_utterance_ms` so ASR latency
      stays bounded (§149 bounded resources).
    """

    def __init__(self, settings: VadSettings, fmt: PcmFormat) -> None:
        self._settings = settings
        self._fmt = fmt
        self._in_speech = False
        self._buffer: list[bytes] = []
        self._silence_frames = 0
        pad_frames = settings.pre_speech_pad_ms // fmt.frame_ms
        self._pad: deque[bytes] = deque(maxlen=pad_frames)

    def feed(self, frame: bytes) -> Utterance | None:
        if len(frame) != self._fmt.frame_bytes:
            raise ValueError(
                f"frame size mismatch: expected {self._fmt.frame_bytes}, got {len(frame)}"
            )
        rms = pcm_rms(frame)
        if rms >= self._settings.rms_speech_threshold:
            if not self._in_speech:
                self._in_speech = True
                self._buffer.extend(self._pad)
                self._pad.clear()
            self._buffer.append(frame)
            self._silence_frames = 0
            if self._duration_ms() >= self._settings.max_utterance_ms:
                return self._close()
            return None

        if self._in_speech:
            self._buffer.append(frame)
            self._silence_frames += 1
            if self._silence_ms() >= self._settings.silence_ms_to_close:
                return self._close()
            return None

        if self._pad.maxlen:
            self._pad.append(frame)
        return None

    def flush(self) -> Utterance | None:
        """Close any open utterance at end of stream."""
        if self._in_speech:
            return self._close()
        return None

    def _duration_ms(self) -> int:
        return self._fmt.duration_ms(len(self._buffer) * self._fmt.frame_bytes)

    def _silence_ms(self) -> int:
        return self._silence_frames * self._fmt.frame_ms

    def _close(self) -> Utterance | None:
        speech = self._buffer[: len(self._buffer) - self._silence_frames]
        self._buffer.clear()
        self._in_speech = False
        self._silence_frames = 0
        duration = self._fmt.duration_ms(len(speech) * self._fmt.frame_bytes)
        if duration < self._settings.min_utterance_ms:
            return None
        return Utterance(pcm=b"".join(speech), duration_ms=duration)