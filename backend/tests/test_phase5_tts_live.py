"""Phase 5 live integration tests (§194 TTS adapter/queue/gate/audio client,
§80, §95, §201).

End-to-end over the real WebSocket + ingest pipeline with the silent-PCM mock
TTS adapter: an §80-approved, high-confidence caption is turned into an
``audio`` broadcast event, while a blocked/unsafe caption is NEVER spoken.
TTS is off by default (§201) and must be explicitly enabled per test.
"""

from __future__ import annotations

import io
import struct
import wave

from bayaan_server.config import (
    DatabaseSettings,
    ProviderSettings,
    SafetySettings,
    Settings,
)
from fastapi.testclient import TestClient

from .conftest import make_client

QURAN_TEXT = "﴿ان مع العسر يسرا﴾"


def make_wav(speech_seconds: float = 0.6) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 8000)
        n = int(16000 * speech_seconds)
        w.writeframes(b"".join(struct.pack("<h", 3000) for _ in range(n)))
        w.writeframes(b"\x00\x00" * 16000)
    return buf.getvalue()


def _setup_live(client: TestClient) -> tuple[dict, dict]:
    room = client.post("/api/v1/rooms", json={}).json()
    session = client.post(
        "/api/v1/sessions",
        json={"room_id": room["id"], "active_languages": ["it", "en"]},
    ).json()
    client.post(f"/api/v1/sessions/{session['id']}/status", json={"status": "READY"})
    client.post(f"/api/v1/sessions/{session['id']}/status", json={"status": "LIVE"})
    return session, room


def _token(client: TestClient, code: str) -> str:
    return client.get(f"/api/v1/rooms/{code}").json()["visitor_token"]


def _ingest_and_collect(client: TestClient, session_id: str, code: str,
                        lang: str, want_audio: bool) -> list[dict]:
    """Ingest one clip and return WS frames until we see the audio event (if
    ``want_audio``) or the first caption (for the "never spoken" cases)."""
    token = _token(client, code)
    events: list[dict] = []
    with client.websocket_connect(
        f"/ws/rooms/{code}?lang={lang}&token={token}"
    ) as ws:
        assert ws.receive_json()["type"] == "hello"
        client.post(
            f"/api/v1/sessions/{session_id}/ingest",
            files={"file": ("clip.wav", make_wav(), "audio/wav")},
        )
        for _ in range(6):
            frame = ws.receive_json()
            events.append(frame)
            if frame.get("type") == "audio":
                break
            if not want_audio and frame.get("type") == "caption":
                break
    return events


def test_tts_off_by_default_never_emits_audio(tmp_path) -> None:
    """§201 text-first: with TTS locked (default) no audio event is ever sent,
    even for a high-confidence caption that passes every text gate."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/t0.db"),
            safety=SafetySettings(tts_enabled=False),
            providers=ProviderSettings(
                mock_asr_text="مرحبا بكم في مسجدنا",
                mock_asr_confidence=0.99,
                mock_translation_confidence=0.99,
            ),
        )
    ) as client:
        session, room = _setup_live(client)
        events = _ingest_and_collect(
            client, session["id"], room["code"], "en", want_audio=False
        )
        assert all(e.get("type") != "audio" for e in events)


def test_approved_general_caption_is_spoken(tmp_path) -> None:
    """§194/§80: with TTS enabled, an approved high-confidence general caption
    is synthesized and broadcast as an ``audio`` event carrying silent PCM."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/t1.db"),
            safety=SafetySettings(tts_enabled=True),
            providers=ProviderSettings(
                mock_asr_text="مرحبا بكم في مسجدنا",
                mock_asr_confidence=0.99,
                mock_translation_confidence=0.99,
            ),
        )
    ) as client:
        session, room = _setup_live(client)
        # Two target languages (it, en) -> two audio events; collect a few
        # frames so we can see the en one regardless of publish order.
        code = room["code"]
        token = _token(client, code)
        events: list[dict] = []
        with client.websocket_connect(
            f"/ws/rooms/{code}?lang=en&token={token}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            client.post(
                f"/api/v1/sessions/{session['id']}/ingest",
                files={"file": ("clip.wav", make_wav(), "audio/wav")},
            )
            for _ in range(6):
                events.append(ws.receive_json())
                if len([e for e in events if e.get("type") == "audio"]) >= 2:
                    break
        audio = [e for e in events if e.get("type") == "audio"]
        assert len(audio) >= 1
        en_audio = [a for a in audio if a["language"] == "en"]
        assert len(en_audio) == 1
        assert en_audio[0]["audio_format"] == "pcm_s16le"
        assert en_audio[0]["sample_rate"] > 0
        assert "audio_b64" in en_audio[0]


def test_unverified_quran_is_never_spoken(tmp_path) -> None:
    """§80/§206: a Quran line with no approved reference is blocked for text
    AND never spoken - no audio event is emitted for it."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/t2.db"),
            safety=SafetySettings(tts_enabled=True),
            providers=ProviderSettings(
                mock_asr_text=QURAN_TEXT,
                mock_asr_confidence=0.99,
                mock_translation_confidence=0.99,
            ),
        )
    ) as client:
        session, room = _setup_live(client)
        events = _ingest_and_collect(
            client, session["id"], room["code"], "en", want_audio=False
        )
        caption = next(e for e in events if e.get("type") == "caption")
        assert caption["targets"]["en"]["state"] == "unavailable"
        assert caption["targets"]["en"]["speak"] is False
        # §194 acceptance: unsafe output never reaches TTS.
        assert all(e.get("type") != "audio" for e in events)


def test_low_confidence_caption_is_never_spoken(tmp_path) -> None:
    """§95/§167: a caption that fails the text gate is shown as fallback and is
    never spoken (speak flag False, no audio event)."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/t3.db"),
            safety=SafetySettings(tts_enabled=True),
            providers=ProviderSettings(
                mock_asr_text="نص عام", mock_asr_confidence=0.0
            ),
        )
    ) as client:
        session, room = _setup_live(client)
        events = _ingest_and_collect(
            client, session["id"], room["code"], "en", want_audio=False
        )
        caption = next(e for e in events if e.get("type") == "caption")
        assert caption["targets"]["en"]["state"] == "unavailable"
        assert caption["targets"]["en"]["speak"] is False
        assert all(e.get("type") != "audio" for e in events)
