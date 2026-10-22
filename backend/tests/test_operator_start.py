"""Operator one-click start tests (§200: start a room, no khutbah text).

POST /api/v1/rooms/start creates a room + a CREATED session (no sermon
version) in one step, records the §98 audit event and returns the code +
QR/join URLs. It reuses the room, session, concurrency (§149) and audit
machinery - it adds no new business rule. GET /api/v1/sessions/active
lists the running sessions for the operator dashboard.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import make_client


def test_start_room_creates_room_and_created_session(client: TestClient) -> None:
    resp = client.post("/api/v1/rooms/start", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # Room is active and carries a short code.
    assert body["code"] and body["room_id"]
    # Session is CREATED and NOT tied to any prepared sermon (§200).
    assert body["status"] == "CREATED"
    # Default languages come from settings (§63: it + en).
    assert body["languages"] == ["it", "en"]
    # QR + join URLs point at the real routes.
    assert body["qr_url"].endswith(f"/api/v1/rooms/{body['code']}/qr.svg")
    assert body["join_url"].endswith(f"/r/{body['code']}")

    session = client.get(f"/api/v1/sessions/{body['session_id']}").json()
    assert session["status"] == "CREATED"
    assert session["sermon_version_id"] is None
    assert session["active_languages"] == ["it", "en"]
    assert session["room_id"] == body["room_id"]


def test_started_room_is_discoverable_and_qr_renders(client: TestClient) -> None:
    code = client.post("/api/v1/rooms/start", json={}).json()["code"]

    discovery = client.get(f"/api/v1/rooms/{code}")
    assert discovery.status_code == 200
    body = discovery.json()
    assert body["room_code"] == code
    assert body["session_status"] == "CREATED"
    assert "." in body["visitor_token"]  # §116 expiring, no identity

    qr = client.get(f"/api/v1/rooms/{code}/qr.svg")
    assert qr.status_code == 200
    assert "image/svg+xml" in qr.headers["content-type"]
    assert "<svg" in qr.text


def test_start_room_accepts_custom_languages(client: TestClient) -> None:
    resp = client.post("/api/v1/rooms/start", json={"active_languages": ["it"]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["languages"] == ["it"]
    session = client.get(f"/api/v1/sessions/{body['session_id']}").json()
    assert session["active_languages"] == ["it"]


def test_start_room_rejects_unknown_language(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/rooms/start", json={"active_languages": ["it", "zz"]}
    )
    assert resp.status_code == 422


def test_start_room_respects_max_active_sessions(client: TestClient, tmp_path) -> None:
    from bayaan_server.config import DatabaseSettings, Settings

    limited = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/start.db"),
        max_active_sessions=1,
    )
    with make_client(limited) as c:
        first = c.post("/api/v1/rooms/start", json={}).json()
        # Bring the first session to READY so it counts as active (§149).
        c.post(
            f"/api/v1/sessions/{first['session_id']}/status", json={"status": "READY"}
        )
        # A second start now hits the concurrency bound.
        resp = c.post("/api/v1/rooms/start", json={})
        assert resp.status_code == 409


def test_start_room_is_admin_rate_limited(client: TestClient, tmp_path) -> None:
    from bayaan_server.config import DatabaseSettings, RateLimitSettings, Settings

    limited = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/start_rl.db"),
        rate_limit=RateLimitSettings(admin_per_minute=2),
    )
    with make_client(limited) as c:
        assert c.post("/api/v1/rooms/start", json={}).status_code == 201
        assert c.post("/api/v1/rooms/start", json={}).status_code == 201
        assert c.post("/api/v1/rooms/start", json={}).status_code == 429


def test_active_sessions_lists_running_and_excludes_created(
    client: TestClient,
) -> None:
    started = client.post("/api/v1/rooms/start", json={}).json()
    # CREATED is not "active" (READY/LIVE) until the operator advances it.
    assert client.get("/api/v1/sessions/active").json() == []

    client.post(
        f"/api/v1/sessions/{started['session_id']}/status", json={"status": "READY"}
    )
    active = client.get("/api/v1/sessions/active").json()
    assert len(active) == 1
    assert active[0]["id"] == started["session_id"]
    assert active[0]["status"] == "READY"
