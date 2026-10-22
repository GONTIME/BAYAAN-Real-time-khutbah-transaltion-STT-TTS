"""§189 Phase 0 acceptance: health check must reflect real component state."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"]


def test_health_lists_environment(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["environment"] in {"development", "staging", "production"}

def test_health_public_payload_minimal(client: TestClient) -> None:
    """BUG-024: public /health exposes liveness only — no provider circuit
    detail or degraded-mode internals."""
    body = client.get("/health").json()
    assert set(body.keys()) <= {"status", "version", "environment", "database"}
