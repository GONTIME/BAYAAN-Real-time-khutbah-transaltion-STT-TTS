"""WebSocket broadcast tests (§55, §116, §134, §191).

Acceptance (§191): phone receives live Arabic/target text.
"""

from __future__ import annotations

import io
import struct
import uuid
import wave

from bayaan_server.config import (
    DatabaseSettings,
    ProviderSettings,
    SafetySettings,
    Settings,
)
from bayaan_server.ws.broker import Connection
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDisconnect

from .conftest import make_client


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
    # Room first, then a session bound to the room's real id - the broker
    # publishes via session.room_id, so the graph must be consistent.
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


def test_phone_receives_live_captions(tmp_path) -> None:
    """§191 acceptance: live Arabic/target text over WebSocket."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ws.db"),
            providers=ProviderSettings(
                mock_asr_confidence=0.99, mock_translation_confidence=0.99
            ),
        )
    ) as client:
        session, room = _setup_live(client)
        token = _token(client, room["code"])

        with client.websocket_connect(
            f"/ws/rooms/{room['code']}?lang=it&token={token}"
        ) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["session_id"] == session["id"]
            assert hello["status"] == "LIVE"
            assert hello["languages"] == ["it", "en"]

            resp = client.post(
                f"/api/v1/sessions/{session['id']}/ingest",
                files={"file": ("clip.wav", make_wav(), "audio/wav")},
            )
            assert resp.status_code == 200

            caption = ws.receive_json()
            assert caption["type"] == "caption"
            assert caption["sequence"] == 0
            assert caption["arabic"].startswith("[MOCK")
            assert caption["targets"]["it"]["state"] == "ok"
            assert "MOCK-TRANSLATION:it" in caption["targets"]["it"]["text"]
            assert "MOCK-TRANSLATION:en" in caption["targets"]["en"]["text"]


def test_unsafe_output_shows_public_fallback_only(tmp_path) -> None:
    """§134: with zero mock confidence, captions must be the public fallback
    message - internal scores never leak to clients."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ws2.db"),
            providers=ProviderSettings(mock_asr_confidence=0.0),
        )
    ) as client:
        session, room = _setup_live(client)
        token = _token(client, room["code"])
        with client.websocket_connect(
            f"/ws/rooms/{room['code']}?lang=it&token={token}"
        ) as ws:
            ws.receive_json()  # hello
            client.post(
                f"/api/v1/sessions/{session['id']}/ingest",
                files={"file": ("clip.wav", make_wav(), "audio/wav")},
            )
            caption = ws.receive_json()
            target = caption["targets"]["it"]
            assert target["state"] == "unavailable"
            assert target["text"] == "Translation unavailable"
            assert "reason" not in caption and "risk" not in caption


def test_late_joiner_receives_history(tmp_path) -> None:
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ws3.db"),
            providers=ProviderSettings(
                mock_asr_confidence=0.99, mock_translation_confidence=0.99
            ),
        )
    ) as client:
        session, room = _setup_live(client)
        client.post(
            f"/api/v1/sessions/{session['id']}/ingest",
            files={"file": ("clip.wav", make_wav(), "audio/wav")},
        )
        token = _token(client, room["code"])
        with client.websocket_connect(
            f"/ws/rooms/{room['code']}?lang=en&token={token}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            replayed = ws.receive_json()
            assert replayed["type"] == "caption"
            assert replayed["sequence"] == 0


def test_session_status_broadcast(tmp_path) -> None:
    with make_client(
        Settings(database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ws4.db"))
    ) as client:
        session = client.post(
            "/api/v1/sessions",
            json={"room_id": str(uuid.uuid4()), "active_languages": ["it"]},
        ).json()
        room = client.post("/api/v1/rooms", json={"session_id": session["id"]}).json()
        token = _token(client, room["code"])
        with client.websocket_connect(
            f"/ws/rooms/{room['code']}?lang=it&token={token}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            client.post(
                f"/api/v1/sessions/{session['id']}/status", json={"status": "READY"}
            )
            assert ws.receive_json() == {"type": "session", "status": "READY"}


def test_invalid_token_is_rejected(tmp_path) -> None:
    with make_client(
        Settings(database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ws5.db"))
    ) as client:
        _, room = _setup_live(client)
        try:
            with client.websocket_connect(
                f"/ws/rooms/{room['code']}?lang=it&token=bad.token"
            ) as ws:
                ws.receive_json()
            raise AssertionError("expected rejection")
        except WebSocketDisconnect as exc:
            assert exc.code == 4401


def test_invalid_language_is_rejected(tmp_path) -> None:
    with make_client(
        Settings(database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ws6.db"))
    ) as client:
        _, room = _setup_live(client)
        token = _token(client, room["code"])
        try:
            with client.websocket_connect(
                f"/ws/rooms/{room['code']}?lang=fr&token={token}"
            ) as ws:
                ws.receive_json()
            raise AssertionError("expected rejection")
        except WebSocketDisconnect as exc:
            assert exc.code == 4400


def test_unknown_room_is_rejected(tmp_path) -> None:
    with make_client(
        Settings(database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ws7.db"))
    ) as client:
        try:
            with client.websocket_connect("/ws/rooms/GHOST777?lang=it&token=x.y") as ws:
                ws.receive_json()
            raise AssertionError("expected rejection")
        except WebSocketDisconnect as exc:
            assert exc.code == 4404


def test_language_switch_and_ping(tmp_path) -> None:
    with make_client(
        Settings(database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ws8.db"))
    ) as client:
        _, room = _setup_live(client)
        token = _token(client, room["code"])
        with client.websocket_connect(
            f"/ws/rooms/{room['code']}?lang=it&token={token}"
        ) as ws:
            ws.receive_json()  # hello
            ws.send_json({"type": "set_language", "lang": "en"})
            assert ws.receive_json() == {"type": "language", "lang": "en"}
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


def _recv_audio(ws, lang: str | None = None) -> dict:
    """Read events until an `audio` event arrives (TTS worker is async).

    The room broadcasts audio for EVERY target language; a client only plays
    its own language (AudioPlayer filters by language), so tests on a specific
    language must skip the others.
    """
    import time as _t

    deadline = _t.time() + 15
    while _t.time() < deadline:
        ev = ws.receive_json()
        if ev["type"] == "audio" and (lang is None or ev.get("language") == lang):
            return ev
    raise AssertionError("no audio event received within 15s")


def test_late_joiner_receives_recent_audio(tmp_path) -> None:
    """§80/§88: spoken audio is replayed to a client that connects (or reconnects)
    AFTER the line was spoken - a phone that drops its WS mid-line still hears the
    current line. The audio ring survives the room briefly emptying (the critical
    single-phone reconnect case)."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ws9.db"),
            providers=ProviderSettings(
                mock_asr_confidence=0.99, mock_translation_confidence=0.99
            ),
            safety=SafetySettings(tts_enabled=True),
        )
    ) as client:
        session, room = _setup_live(client)
        token = _token(client, room["code"])
        # First subscriber hears the line live.
        with client.websocket_connect(
            f"/ws/rooms/{room['code']}?lang=en&token={token}"
        ) as ws:
            ws.receive_json()  # hello
            client.post(
                f"/api/v1/sessions/{session['id']}/ingest",
                files={"file": ("clip.wav", make_wav(), "audio/wav")},
            )
            ws.receive_json()  # caption
            audio = _recv_audio(ws, "en")
            assert audio["language"] == "en"
        # Room is now empty (the only client left). A new subscriber connects
        # AFTER the line was spoken and must still receive the audio event.
        with client.websocket_connect(
            f"/ws/rooms/{room['code']}?lang=en&token={token}"
        ) as ws2:
            assert ws2.receive_json()["type"] == "hello"
            replayed = _recv_audio(ws2, "en")
            assert replayed["type"] == "audio"
            assert replayed["language"] == "en"
            assert replayed["sequence"] == audio["sequence"]


def test_broker_audio_ring_bounded_age_pruned_and_survives_empty() -> None:
    """§80/§88 broker ring: bounded by size, pruned by the staleness window, and
    NOT dropped when the room empties (so a reconnecting phone still hears the
    current line)."""
    from datetime import datetime, timezone

    from bayaan_server.metrics import MetricsRegistry
    from bayaan_server.ws.broker import AUDIO_HISTORY_SIZE, RoomBroker

    broker = RoomBroker(Settings(), MetricsRegistry())
    rid = uuid.uuid4()
    for i in range(AUDIO_HISTORY_SIZE + 3):
        broker.publish_audio(
            rid,
            {"type": "audio", "sequence": i, "language": "en",
             "created_at": datetime.now(timezone.utc).isoformat()},
        )
    recent = broker.recent_audio(rid)
    assert len(recent) == AUDIO_HISTORY_SIZE
    assert [e["sequence"] for e in recent] == list(range(3, AUDIO_HISTORY_SIZE + 3))

    # The only phone disconnects (room empties) -> ring must survive.
    conn = Connection(websocket=None, language="en", address="test")
    broker.subscribe(rid, conn)
    broker.unsubscribe(rid, conn)
    assert broker.recent_audio(rid), "audio ring must survive room emptying"

    # Age pruning: a ring whose ONLY entry is older than the staleness window is
    # pruned (never replayed) - hearing an old line is worse than silence (§88).
    rid2 = uuid.uuid4()
    from datetime import timedelta

    ancient = datetime.now(timezone.utc) - timedelta(seconds=10)
    broker.publish_audio(
        rid2,
        {"type": "audio", "sequence": 999, "language": "en",
         "created_at": ancient.isoformat()},
    )
    assert broker.recent_audio(rid2) == [], "stale audio must be pruned"