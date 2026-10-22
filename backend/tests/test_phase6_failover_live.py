"""Phase 6 live acceptance tests (§195 provider failover, §54 circuit breaker,
§57 degraded mode, §111 watchdog).

§195 acceptance: **"simulated provider failure does not break original
service."** Driven through the real app + WebSocket + ingest + /health, using
the ``mock-failing`` failure-injection provider:

- with a fallback, captions keep reaching the phone after the primary dies,
  the switch is measurable, and /health reports the open circuit;
- with NO fallback, the app degrades (§57 arabic_only, degraded_mode_reason)
  yet keeps serving the §134 public fallback instead of crashing;
- a translation outage fails over per language the same way.
"""

from __future__ import annotations

import io
import struct
import wave

from fastapi.testclient import TestClient

from bayaan_server.config import (
    DatabaseSettings,
    FailoverSettings,
    ProviderSettings,
    Settings,
)
from .conftest import make_client


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
        json={"room_id": room["id"], "active_languages": ["en", "it"]},
    ).json()
    client.post(f"/api/v1/sessions/{session['id']}/status", json={"status": "READY"})
    client.post(f"/api/v1/sessions/{session['id']}/status", json={"status": "LIVE"})
    return session, room


def _ingest_and_collect(client, session_id, code):
    token = client.get(f"/api/v1/rooms/{code}").json()["visitor_token"]
    events: list[dict] = []
    with client.websocket_connect(
        f"/ws/rooms/{code}?lang=en&token={token}"
    ) as ws:
        assert ws.receive_json()["type"] == "hello"
        client.post(
            f"/api/v1/sessions/{session_id}/ingest",
            files={"file": ("clip.wav", make_wav(), "audio/wav")},
        )
        for _ in range(6):
            frame = ws.receive_json()
            events.append(frame)
            if frame.get("type") == "caption":
                break
    return events



def _summary(client: TestClient) -> dict:
    """BUG-024: mode/degraded_reason/provider circuits now come from the
    operator-authenticated diagnostics endpoint, not the public /health."""
    resp = client.get("/api/v1/diagnostics/system-summary")
    assert resp.status_code == 200, resp.text
    return resp.json()

TEXT = "مرحبا بكم في مسجدنا"


def test_primary_failure_served_by_fallback_service_unbroken(tmp_path) -> None:
    """§195 acceptance: a simulated ASR outage with a fallback chain must not
    break the live captions, and the switch must be observable."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/p1.db"),
            providers=ProviderSettings(
                asr="mock-failing",
                asr_fallback="mock",
                mock_asr_text=TEXT,
                mock_asr_confidence=0.99,
                mock_translation_confidence=0.99,
            ),
            failover=FailoverSettings(failure_threshold=1, cooldown_ms=60000),
        )
    ) as client:
        session, room = _setup_live(client)
        events = _ingest_and_collect(client, session["id"], room["code"])

        captions = [e for e in events if e.get("type") == "caption"]
        assert len(captions) >= 1
        target = captions[0]["targets"]["en"]
        # The fallback mock served the transcript: the service is unbroken.
        assert target["state"] == "ok"

        # §87 is observable: the failing primary's circuit is OPEN (via the
        # operator-authenticated diagnostics; /health is liveness-only, BUG-024).
        health = client.get("/health").json()
        assert health["status"] == "ok"  # DB + base service liveness
        summary = _summary(client)
        assert summary["mode"] == "full"
        asr_chain = summary["providers"]["asr"]
        assert asr_chain[0]["provider"] == "mock-failing"
        assert asr_chain[0]["state"] == "open"
        assert asr_chain[1]["state"] == "closed"

        # Metrics evidence: switch + fallback success + failure recorded.
        metrics = client.get("/metrics").text
        assert "bayaan_provider_switch_total 1.0" in metrics
        assert "bayaan_provider_success_asr_mock_total 1.0" in metrics
        assert "bayaan_provider_failure_asr_mock_failing_total 1.0" in metrics


def test_no_fallback_asr_down_degrades_but_keeps_serving(tmp_path) -> None:
    """§57/§58/§178: every ASR provider down with NO fallback -> the service
    degrades (arabic_only + degraded_mode_reason) while phones still receive
    the §134 public fallback - the service never crashes."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/p2.db"),
            providers=ProviderSettings(
                asr="mock-failing",
                mock_asr_text=TEXT,
                mock_translation_confidence=0.99,
            ),
            failover=FailoverSettings(failure_threshold=1, cooldown_ms=60000),
        )
    ) as client:
        session, room = _setup_live(client)
        events = _ingest_and_collect(client, session["id"], room["code"])

        captions = [e for e in events if e.get("type") == "caption"]
        assert len(captions) >= 1
        assert captions[0]["targets"]["en"]["state"] == "unavailable"

        # §57: the degraded mode with an explicit reason is visible to the
        # OPERATOR (diagnostics); the public /health stays liveness-only.
        health = client.get("/health").json()
        assert health["status"] == "ok"  # public liveness never reveals internals
        summary = _summary(client)
        assert summary["status"] == "degraded"
        assert summary["mode"] == "arabic_only"
        assert "asr_unavailable" in (summary["degraded_reason"] or "")

        metrics = client.get("/metrics").text
        assert "bayaan_asr_unavailable_total 1.0" in metrics
        assert "bayaan_alert_asr_unavailable_total 1.0" in metrics


def test_translation_failure_fails_over_per_language(tmp_path) -> None:
    """A translation outage hits the fallback translation provider; every
    language still gets its caption and /health stays FULL (capability intact)."""
    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/p3.db"),
            providers=ProviderSettings(
                translation="mock-failing",
                translation_fallback="mock",
                mock_asr_text=TEXT,
                mock_asr_confidence=0.99,
                mock_translation_confidence=0.99,
            ),
            failover=FailoverSettings(failure_threshold=1, cooldown_ms=60000),
        )
    ) as client:
        session, room = _setup_live(client)
        events = _ingest_and_collect(client, session["id"], room["code"])

        captions = [e for e in events if e.get("type") == "caption"]
        assert len(captions) >= 1
        for lang in ("en", "it"):
            assert captions[0]["targets"][lang]["state"] == "ok"

        health = client.get("/health").json()
        assert health["status"] == "ok"
        translation_chain = _summary(client)["providers"]["translation"]
        assert translation_chain[0]["state"] == "open"  # primary tripped, fallback works

        metrics = client.get("/metrics").text
        assert "bayaan_provider_switch_total 1.0" in metrics


def test_unchanged_default_config_still_serves_captions(tmp_path) -> None:
    """Regression guard: with default single-provider chains there are no
    switches, /health stays full/ok and captions flow as before."""
    with make_client(
        Settings(database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/p4.db"))
    ) as client:
        session, room = _setup_live(client)
        # Default mock_asr_text is empty -> mock emits [MOCK-ASR-UNAVAILABLE];
        # that still flows as a caption (the SERVICE responds, no crash).
        events = _ingest_and_collect(client, session["id"], room["code"])
        captions = [e for e in events if e.get("type") == "caption"]
        assert captions

        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert _summary(client)["mode"] == "full"
        metrics = client.get("/metrics").text
        assert "bayaan_provider_switch_total" not in metrics