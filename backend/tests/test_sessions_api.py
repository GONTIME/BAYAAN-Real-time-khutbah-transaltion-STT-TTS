"""Live session API tests (§189 basic API, §149 concurrency, §98 audit)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def _create(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/sessions",
        json={"room_id": str(uuid.uuid4()), "active_languages": ["it", "en"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_and_get_session(client: TestClient) -> None:
    created = _create(client)
    fetched = client.get(f"/api/v1/sessions/{created['id']}").json()
    assert fetched["id"] == created["id"]
    assert fetched["status"] == "CREATED"
    assert fetched["active_languages"] == ["it", "en"]


def test_get_missing_session_returns_404(client: TestClient) -> None:
    resp = client.get(f"/api/v1/sessions/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_lifecycle_transitions(client: TestClient) -> None:
    created = _create(client)
    sid = created["id"]

    resp = client.post(f"/api/v1/sessions/{sid}/status", json={"status": "LIVE"})
    assert resp.status_code == 409  # CREATED -> LIVE is not allowed

    resp = client.post(f"/api/v1/sessions/{sid}/status", json={"status": "READY"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "READY"

    resp = client.post(f"/api/v1/sessions/{sid}/status", json={"status": "LIVE"})
    assert resp.status_code == 200
    assert resp.json()["started_at"] is not None

    resp = client.post(f"/api/v1/sessions/{sid}/status", json={"status": "ENDED"})
    assert resp.status_code == 200
    assert resp.json()["ended_at"] is not None

    # Terminal states are final (§111: no silent reopen).
    resp = client.post(f"/api/v1/sessions/{sid}/status", json={"status": "LIVE"})
    assert resp.status_code == 409


def test_concurrency_limit_enforced(client: TestClient) -> None:
    # Default settings allow 4 active sessions (§149 bounded concurrency).
    for _ in range(4):
        created = _create(client)
        resp = client.post(
            f"/api/v1/sessions/{created['id']}/status", json={"status": "READY"}
        )
        assert resp.status_code == 200

    resp = client.post(
        "/api/v1/sessions",
        json={"room_id": str(uuid.uuid4()), "active_languages": ["it"]},
    )
    assert resp.status_code == 409