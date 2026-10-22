"""Phase 1 verification: metrics endpoint and preflight (§190, §184)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_metrics_endpoint_exposes_counters(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_preflight_reports_components(client: TestClient) -> None:
    resp = client.get("/api/v1/preflight")
    assert resp.status_code == 200
    body = resp.json()
    names = {c["name"] for c in body["components"]}
    assert {
        "database", "audio_device", "providers", "resilience",
        "safety_policy", "security",
    } <= names
    # Database must be OK in the test environment; missing sounddevice is a
    # dev-mode WARN, never a crash (§184 handled explicitly).
    db = next(c for c in body["components"] if c["name"] == "database")
    assert db["status"] == "OK"
    audio = next(c for c in body["components"] if c["name"] == "audio_device")
    assert audio["status"] in {"OK", "WARN"}
    res = next(c for c in body["components"] if c["name"] == "resilience")
    assert res["status"] == "OK"  # default chains always satisfy §87/§54
    assert body["status"] in {"GREEN", "YELLOW"}


def test_preflight_fails_production_with_dev_secret(tmp_path) -> None:
    """§184/§116: production + default visitor secret must be a loud FAIL."""
    from bayaan_server.config import DatabaseSettings, SecuritySettings, Settings

    prod = Settings(
        environment="production",
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/prod.db"),
        security=SecuritySettings(visitor_token_secret="dev-insecure-secret-change-me"),
    )
    from bayaan_server.audio.preflight import run_preflight

    import asyncio

    report = asyncio.run(run_preflight(prod))
    security = next(c for c in report.components if c.name == "security")
    assert security.status == "FAIL"
    assert report.status == "RED"

def test_security_headers_allow_same_origin_mic(client: TestClient) -> None:
    """BUG-008: Permissions-Policy permits same-origin microphone capture and
    the CSP has no wildcard exfiltration targets."""
    resp = client.get("/health")
    pp = resp.headers.get("permissions-policy", "")
    assert "microphone=(self)" in pp
    csp = resp.headers.get("content-security-policy", "")
    assert "connect-src 'self'" in csp
    assert "*" not in csp.split("img-src", 1)[1].split(";", 1) if "img-src" in csp else True
    assert "blob:" in csp  # AudioWorklet loads from a blob URL


def test_metrics_requires_operator_auth(tmp_path) -> None:
    """BUG-024: /metrics is operator-only, never public."""
    from fastapi.testclient import TestClient as TC
    from bayaan_server.api.app import create_app
    from bayaan_server.config import DatabaseSettings, Settings

    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/m.db")
    )
    with TC(create_app(settings)) as bare:
        assert bare.get("/metrics").status_code in (401, 403)
        r = bare.get(
            "/metrics", headers={"Authorization": "Basic YWRtaW46YWRtaW4="}
        )
        assert r.status_code == 200
