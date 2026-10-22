"""Phase 4 live integration tests (§77 alignment, §79 validation, §78/§86
religious routing, §134 screen safety).

The mock ASR is scripted via `mock_asr_text` (ADR-0004 hook) so we can feed a
Quran-marked utterance through the real pipeline without a speech backend:
an unverified Quran line must never broadcast (public fallback only), while an
approved scripture reference surfaces the approved translation.
"""

from __future__ import annotations

import io
import struct
import wave

from bayaan_server.config import DatabaseSettings, ProviderSettings, Settings
from fastapi.testclient import TestClient

from .conftest import make_client

# A Quran-marked line: the rule detector classifies it QURAN via ornate brackets.
QURAN_TEXT = "﴿ان مع العسر يسرا﴾"


def make_wav(speech_seconds: float = 0.6) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 8000)  # 0.5s silence
        n = int(16000 * speech_seconds)
        w.writeframes(b"".join(struct.pack("<h", 3000) for _ in range(n)))
        w.writeframes(b"\x00\x00" * 16000)  # 1s silence
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


def _ingest_and_read(client: TestClient, session_id: str, code: str, lang: str) -> dict:
    token = _token(client, code)
    with client.websocket_connect(
        f"/ws/rooms/{code}?lang={lang}&token={token}"
    ) as ws:
        assert ws.receive_json()["type"] == "hello"
        client.post(
            f"/api/v1/sessions/{session_id}/ingest",
            files={"file": ("clip.wav", make_wav(), "audio/wav")},
        )
        return ws.receive_json()


def test_unverified_quran_is_blocked_public_fallback(tmp_path) -> None:
    """§78/§134/§206: Quran text with no approved reference must not broadcast -
    the public client sees 'Translation unavailable', never machine translation."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/q.db"),
            providers=ProviderSettings(
                mock_asr_text=QURAN_TEXT,
                mock_asr_confidence=0.99,
                mock_translation_confidence=0.99,
            ),
        )
    ) as client:
        session, room = _setup_live(client)
        caption = _ingest_and_read(client, session["id"], room["code"], "en")
        assert caption["type"] == "caption"
        assert caption["content_type"] == "QURAN"
        # §134: the public client sees the fallback message only.
        assert caption["targets"]["en"]["state"] == "unavailable"
        assert caption["targets"]["en"]["text"] == "Translation unavailable"
        assert "MOCK-TRANSLATION" not in caption["targets"]["en"]["text"]
        # §95: a blocked caption is never spoken.
        assert caption["targets"]["en"]["speak"] is False


def test_approved_quran_reference_surfaces_approved_translation(tmp_path) -> None:
    """§78/§206: once an approved Quran reference exists, the live pipeline
    broadcasts the APPROVED translation (never machine translation)."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/q2.db"),
            providers=ProviderSettings(
                mock_asr_text=QURAN_TEXT,
                mock_asr_confidence=0.99,
                mock_translation_confidence=0.99,
            ),
        )
    ) as client:
        created = client.post(
            "/api/v1/scripture",
            json={
                "kind": "QURAN",
                "key": "94:6",
                "arabic": QURAN_TEXT,
                "translations": {"en": "with hardship comes ease"},
            },
        )
        assert created.status_code == 201, created.text
        approved = client.post(f"/api/v1/scripture/{created.json()['id']}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["approved"] is True

        session, room = _setup_live(client)
        caption = _ingest_and_read(client, session["id"], room["code"], "en")
        assert caption["content_type"] == "QURAN"
        assert caption["targets"]["en"]["state"] == "ok"
        assert caption["targets"]["en"]["text"] == "with hardship comes ease"
        assert caption["targets"]["en"]["route"] == "scripture_verified"


def test_general_text_still_broadcasts_with_route(tmp_path) -> None:
    """Regression: ordinary speech still translates and now carries a route tag
    plus a §95 speak flag."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/q3.db"),
            providers=ProviderSettings(
                mock_asr_text="مرحبا بكم في مسجدنا",
                mock_asr_confidence=0.99,
                mock_translation_confidence=0.99,
            ),
        )
    ) as client:
        session, room = _setup_live(client)
        caption = _ingest_and_read(client, session["id"], room["code"], "en")
        assert caption["content_type"] == "GENERAL"
        assert caption["targets"]["en"]["state"] == "ok"
        assert caption["targets"]["en"]["route"] == "general_mt"
        assert "MOCK-TRANSLATION:en" in caption["targets"]["en"]["text"]


def test_low_confidence_still_blocks(tmp_path) -> None:
    """§95/§167 regression: zero mock confidence still yields the public
    fallback and never auto-broadcasts."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/q4.db"),
            providers=ProviderSettings(mock_asr_text="نص عام", mock_asr_confidence=0.0),
        )
    ) as client:
        session, room = _setup_live(client)
        caption = _ingest_and_read(client, session["id"], room["code"], "en")
        assert caption["targets"]["en"]["state"] == "unavailable"
