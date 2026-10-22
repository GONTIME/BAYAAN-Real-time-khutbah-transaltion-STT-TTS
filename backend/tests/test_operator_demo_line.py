"""Operator "Send a test line" (§200) + LAN reachability (ADR-0013) tests.

- POST /sessions/{id}/demo-line drives a scripted Arabic utterance through the
  REAL pipeline (no mic/ASR) so captions + live translation reach the room's
  WebSocket - the same safety path as live audio (§0 rule 5).
- BAYAAN_PUBLIC_BASE_URL makes the QR code + operator join link point at a
  LAN-reachable address instead of the dev-proxy Host (127.0.0.1), so phones on
  the same Wi-Fi can open the room (ADR-0013 / §197).
- The WS hello carries tts_enabled so the room page is honest about audio.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bayaan_server.config import (
    DatabaseSettings,
    ProviderSettings,
    SafetySettings,
    Settings,
)

from .conftest import make_client


def _live_session(client: TestClient) -> tuple[dict, dict]:
    """Start a room + bring its session CREATED -> READY -> LIVE."""
    started = client.post("/api/v1/rooms/start", json={}).json()
    sid = started["session_id"]
    for status in ("READY", "LIVE"):
        client.post(f"/api/v1/sessions/{sid}/status", json={"status": status})
    room = client.get(f"/api/v1/rooms/{started['code']}").json()
    return started, room


def test_demo_line_broadcasts_a_real_caption(tmp_path) -> None:
    """§200: a scripted line reaches the room WS as a real, translated caption
    without any microphone or ASR backend."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/dl.db"),
            providers=ProviderSettings(mock_translation_confidence=0.99),
        )
    ) as client:
        started, room = _live_session(client)
        token = room["visitor_token"]
        with client.websocket_connect(
            f"/ws/rooms/{room['room_code']}?lang=en&token={token}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            resp = client.post(
                f"/api/v1/sessions/{started['session_id']}/demo-line",
                json={"arabic": "مرحبا بكم في مسجدنا"},
            )
            assert resp.status_code == 200, resp.text
            caption = ws.receive_json()
            assert caption["type"] == "caption"
            assert caption["arabic"] == "مرحبا بكم في مسجدنا"
            # The mock MT chain produced real (marked) target text end-to-end.
            assert "MOCK-TRANSLATION:en" in caption["targets"]["en"]["text"]


def test_demo_line_requires_live_session(client: TestClient) -> None:
    """§190: sending a line to a non-LIVE session is a typed 409, not a crash."""
    started = client.post("/api/v1/rooms/start", json={}).json()
    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/demo-line",
        json={"arabic": "مرحبا"},
    )
    assert resp.status_code == 409
    assert "LIVE" in resp.json()["detail"]


def test_demo_line_rejects_empty_text(client: TestClient) -> None:
    """An empty line is rejected by validation before it ever hits the pipeline."""
    started, _ = _live_session(client)
    resp = client.post(
        f"/api/v1/sessions/{started['session_id']}/demo-line", json={"arabic": ""}
    )
    assert resp.status_code == 422


def test_demo_line_unknown_session_404(client: TestClient) -> None:
    import uuid

    resp = client.post(
        f"/api/v1/sessions/{uuid.uuid4()}/demo-line", json={"arabic": "مرحبا"}
    )
    assert resp.status_code == 404


def test_public_base_url_drives_qr_and_join(tmp_path) -> None:
    """ADR-0013: when BAYAAN_PUBLIC_BASE_URL is set, the QR + join link use it
    (LAN-reachable) instead of the incoming request's Host."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/pub.db"),
            public_base_url="http://192.168.1.20:5173",
        )
    ) as client:
        body = client.post("/api/v1/rooms/start", json={}).json()
        assert body["join_url"] == f"http://192.168.1.20:5173/r/{body['code']}"
        assert body["qr_url"] == (
            f"http://192.168.1.20:5173/api/v1/rooms/{body['code']}/qr.svg"
        )
        # The QR itself encodes the LAN-reachable room URL.
        qr = client.get(f"/api/v1/rooms/{body['code']}/qr.svg")
        assert qr.status_code == 200


def test_join_url_falls_back_to_request_base(client: TestClient) -> None:
    """Without a configured base URL the previous behaviour is preserved."""
    body = client.post("/api/v1/rooms/start", json={}).json()
    # TestClient's base_url is http://testserver.
    assert body["join_url"].startswith("http://testserver/r/")


def test_hello_reports_tts_enabled(tmp_path) -> None:
    """§201: the hello event reflects the server's real audio policy so the
    room page never shows a dead audio toggle."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/tts.db"),
            safety=SafetySettings(tts_enabled=True),
            providers=ProviderSettings(mock_asr_confidence=0.99),
        )
    ) as client:
        _, room = _live_session(client)
        token = room["visitor_token"]
        with client.websocket_connect(
            f"/ws/rooms/{room['room_code']}?lang=en&token={token}"
        ) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["tts_enabled"] is True


def test_hello_reports_tts_disabled_by_default(tmp_path) -> None:
    """Default (§201 text-first): hello reports tts_enabled=False."""
    with make_client(
        Settings(database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/tts2.db"))
    ) as client:
        _, room = _live_session(client)
        token = room["visitor_token"]
        with client.websocket_connect(
            f"/ws/rooms/{room['room_code']}?lang=en&token={token}"
        ) as ws:
            hello = ws.receive_json()
            assert hello["tts_enabled"] is False
