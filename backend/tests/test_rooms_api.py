"""Rooms API tests (§197 Room/QR, §115 discovery rate limit, §116 token)."""

from __future__ import annotations

import uuid

from bayaan_server.config import RateLimitSettings, Settings
from fastapi.testclient import TestClient

from .conftest import make_client


def _make_session(client: TestClient) -> dict:
    return client.post(
        "/api/v1/sessions",
        json={"room_id": str(uuid.uuid4()), "active_languages": ["it", "en"]},
    ).json()


def test_create_room_and_discover(client: TestClient) -> None:
    session = _make_session(client)
    room = client.post(
        "/api/v1/rooms", json={"session_id": session["id"]}
    ).json()
    assert room["code"] and room["active"] and room["session_id"] == session["id"]

    discovery = client.get(f"/api/v1/rooms/{room['code']}")
    assert discovery.status_code == 200
    body = discovery.json()
    assert body["room_code"] == room["code"]
    assert body["session_status"] == "CREATED"
    assert body["languages"] == ["it", "en"]
    # §116: visitor token issued at discovery, expiring, no identity
    assert "." in body["visitor_token"]


def test_discover_unknown_room_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/rooms/NOPE1234").status_code == 404


def test_room_qr_endpoint(client: TestClient) -> None:
    room = client.post("/api/v1/rooms", json={}).json()
    resp = client.get(f"/api/v1/rooms/{room['code']}/qr.svg")
    assert resp.status_code == 200
    assert "image/svg+xml" in resp.headers["content-type"]
    assert "<svg" in resp.text


def test_discovery_rate_limit_is_separate(client: TestClient, tmp_path) -> None:
    """§115: discovery has its own bucket."""
    from bayaan_server.config import DatabaseSettings

    limited = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/rl.db"),
        rate_limit=RateLimitSettings(discovery_per_minute=2),
    )
    with make_client(limited) as c:
        room = c.post("/api/v1/rooms", json={}).json()
        assert c.get(f"/api/v1/rooms/{room['code']}").status_code == 200
        assert c.get(f"/api/v1/rooms/{room['code']}").status_code == 200
        assert c.get(f"/api/v1/rooms/{room['code']}").status_code == 429