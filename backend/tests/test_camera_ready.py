"""PC camera relay pre-flight tests (ADR-0015).

The camera WebSocket only accepts a LIVE session, and it refuses *before* the
browser's webcam is switched on - which the operator experienced as "the camera
starts and closes again after a second" with no explanation. These tests pin the
new HTTP pre-flight (`GET /api/v1/sessions/{id}/camera/ready`) that answers the
question with an explicit reason BEFORE ``getUserMedia`` is called.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bayaan_server.config import CameraSettings, DatabaseSettings, Settings

from .conftest import make_client


def _client(tmp_path, name: str = "cam", **camera) -> TestClient:
    return make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/{name}.db"),
            camera=CameraSettings(**camera),
        )
    )


def _operator_token(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _ready(client: TestClient, session_id: str, token: str) -> dict:
    resp = client.get(
        f"/api/v1/sessions/{session_id}/camera/ready",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_camera_ready_explains_a_non_live_session(tmp_path) -> None:
    """The exact case that looked like "camera starts then closes"."""
    with _client(tmp_path) as client:
        token = _operator_token(client)
        started = client.post("/api/v1/rooms/start", json={}).json()
        sid = started["session_id"]

        # CREATED: the relay would refuse (WS 4409) - say so before the webcam.
        body = _ready(client, sid, token)
        assert body["ready"] is False
        assert body["code"] == 4409
        assert "LIVE" in body["reason"]

        # READY is still not LIVE.
        client.post(f"/api/v1/sessions/{sid}/status", json={"status": "READY"})
        assert _ready(client, sid, token)["ready"] is False

        # LIVE: now the relay accepts.
        client.post(f"/api/v1/sessions/{sid}/status", json={"status": "LIVE"})
        live = _ready(client, sid, token)
        assert live["ready"] is True
        assert live["code"] is None
        assert live["reason"] == ""


def test_camera_ready_reports_a_disabled_relay(tmp_path) -> None:
    with _client(tmp_path, capture_enabled=False) as client:
        token = _operator_token(client)
        started = client.post("/api/v1/rooms/start", json={}).json()
        body = _ready(client, started["session_id"], token)
        assert body["ready"] is False
        assert body["code"] == 4432
        assert "disabled" in body["reason"]


def test_camera_ready_reports_an_unknown_session(tmp_path) -> None:
    import uuid

    with _client(tmp_path) as client:
        token = _operator_token(client)
        body = _ready(client, str(uuid.uuid4()), token)
        assert body["ready"] is False
        assert body["code"] == 4404


def test_camera_ready_requires_an_operator_token(tmp_path) -> None:
    with _client(tmp_path) as client:
        started = client.post("/api/v1/rooms/start", json={}).json()
        resp = client.get(
            f"/api/v1/sessions/{started['session_id']}/camera/ready",
            headers={"Authorization": "Bearer not-a-token"},
        )
        assert resp.status_code == 401


def test_system_summary_reports_the_camera_switch(tmp_path) -> None:
    """The UI hides the camera card when the relay is off (§184 honest UI)."""
    with _client(tmp_path, name="on") as client:
        body = client.get("/api/v1/diagnostics/system-summary").json()
        assert body["config"]["camera_enabled"] is True
    with _client(tmp_path, name="off", capture_enabled=False) as client:
        body = client.get("/api/v1/diagnostics/system-summary").json()
        assert body["config"]["camera_enabled"] is False
