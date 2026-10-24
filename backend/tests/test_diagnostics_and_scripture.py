"""Tests for the diagnostics router and enhanced scripture search."""

from fastapi.testclient import TestClient


def test_diagnostics_summary_and_translation(client: TestClient):
    # System summary
    res = client.get("/api/v1/diagnostics/system-summary")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] in ("ok", "degraded")
    assert "providers" in data
    assert "config" in data

    # Test translation with mock provider
    t_res = client.post(
        "/api/v1/diagnostics/test-translation",
        json={"text": "الحمد لله", "target_language": "it"},
    )
    assert t_res.status_code == 200
    t_data = t_res.json()
    assert t_data["success"] is True
    assert t_data["target_language"] == "it"
    assert t_data["latency_ms"] >= 0


def test_safety_blocks_explain_why_a_line_was_held_back(tmp_path):
    """§98/§168 + ADR-0017: the operator gets the reason, the room does not.

    Safety is NOT relaxed here - the unverified Quran line is still blocked and
    the public caption is still the §134 fallback. The difference is that the
    operator can now read WHY, instead of treating the gate as a black box.
    """
    from bayaan_server.config import (
        DatabaseSettings,
        ProviderSettings,
        Settings,
    )

    from .conftest import make_client
    from .test_phase4_live import QURAN_TEXT

    with make_client(
        Settings(
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/blocks.db"),
            providers=ProviderSettings(
                mock_asr_confidence=0.99, mock_translation_confidence=0.99
            ),
        )
    ) as client:
        started = client.post("/api/v1/rooms/start", json={}).json()
        sid = started["session_id"]
        for status in ("READY", "LIVE"):
            client.post(f"/api/v1/sessions/{sid}/status", json={"status": status})
        room = client.get(f"/api/v1/rooms/{started['code']}").json()

        with client.websocket_connect(
            f"/ws/rooms/{room['room_code']}?lang=en&token={room['visitor_token']}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            client.post(
                f"/api/v1/sessions/{sid}/demo-line", json={"arabic": QURAN_TEXT}
            )
            caption = ws.receive_json()
        # §134 unchanged: the room sees the fallback, never the internal reason.
        assert caption["targets"]["en"]["text"] == "Translation unavailable"
        assert "reason" not in caption

        resp = client.get("/api/v1/diagnostics/safety-blocks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] >= 1
        entry = body["blocks"][0]
        assert entry["action"] == "SAFETY_BLOCK"
        assert entry["target_id"] == sid
        # §168: the operator-facing reason is explainable and non-empty.
        assert entry["metadata"]["reason"]
        assert entry["metadata"]["lang"] in ("it", "en")
        assert entry["metadata"]["sequence"] >= 0


def test_safety_blocks_requires_admin(client: TestClient):
    from .conftest import make_client
    from bayaan_server.config import Settings

    anonymous = make_client(Settings(), headers={})
    resp = anonymous.get("/api/v1/diagnostics/safety-blocks")
    assert resp.status_code in (401, 403)


def test_scripture_search_and_pagination(client: TestClient):
    # Insert verses
    r1 = client.post(
        "/api/v1/scripture",
        json={
            "kind": "QURAN",
            "key": "1:1",
            "arabic": "بسم الله الرحمن الرحيم",
            "surah": "1",
            "ayah": "1",
            "translations": {"en": "In the name of Allah", "it": "In nome di Allah"},
        },
    )
    assert r1.status_code == 201

    r2 = client.post(
        "/api/v1/scripture",
        json={
            "kind": "QURAN",
            "key": "2:255",
            "arabic": "الله لا إله إلا هو الحي القيوم",
            "surah": "2",
            "ayah": "255",
            "translations": {
                "en": "Allah, there is no deity except Him",
                "it": "Iddio! Non c'è dio all'infuori di Lui",
            },
        },
    )
    assert r2.status_code == 201

    # Search by key
    s_res = client.get("/api/v1/scripture?q=2:255")
    assert s_res.status_code == 200
    items = s_res.json()
    assert len(items) == 1
    assert items[0]["key"] == "2:255"

    # Search by Arabic substring
    s_res2 = client.get("/api/v1/scripture?q=الرحمن")
    assert s_res2.status_code == 200
    items2 = s_res2.json()
    assert len(items2) == 1
    assert items2[0]["key"] == "1:1"

    # Pagination test
    p_res = client.get("/api/v1/scripture?limit=1&offset=0")
    assert p_res.status_code == 200
    assert len(p_res.json()) == 1


from unittest.mock import AsyncMock, patch
import pytest
import httpx
from bayaan_server.config import Settings, ProviderSettings
from bayaan_server.providers.whisper import WhisperASRProvider
from bayaan_server.providers.registry import build_providers


@pytest.mark.asyncio
async def test_whisper_provider_transcription():
    settings = Settings(
        providers=ProviderSettings(
            asr="whisper",
            whisper_api_key="test-key",
            whisper_endpoint="https://api.openai.com/v1",
        )
    )
    provider = WhisperASRProvider(settings)
    assert provider.name == "whisper"
    
    mock_resp = httpx.Response(200, json={"text": "الحمد لله رب العالمين"}, request=httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions"))
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        dummy_pcm = b"\x00\x00" * 16000
        res = await provider.transcribe(dummy_pcm, "ar")
        assert res.text == "الحمد لله رب العالمين"
        # BUG-013: the transcription API returns no confidence — the adapter
        # must report None (absent), never a fabricated 0.95.
        assert res.confidence is None
        # Removed assertion on res.provider as AsrResult does not have a provider attribute (§166)


def test_registry_builds_whisper():
    settings = Settings(
        providers=ProviderSettings(
            asr="whisper",
            whisper_api_key="test-key",
        )
    )
    providers = build_providers(settings)
    assert providers.asr is not None


