"""Ollama Cloud adapter tests (§63/§69/§71/§186 + ADR-0011).

Network is stubbed so no Ollama Cloud account is required (§186 isolation).
These tests pin the cloud-specific transport (Bearer auth, no local-only
options, thinking off, HTTP-error typing) so a regression can never silently
turn every cloud translation into a chain-wide outage again.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest

from bayaan_server.config import FailoverSettings, ProviderSettings, Settings
from bayaan_server.providers.errors import ProviderOutageError
from bayaan_server.providers.interfaces import TranslationProvider
from bayaan_server.providers.ollama_cloud import OllamaCloudProvider


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _HTTPError(urllib.error.HTTPError):
    """A buildable HTTPError (urllib's signature is awkward to fake)."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__("https://ollama.com/api/chat", code, reason, hdrs=None, fp=None)


def test_ollama_cloud_satisfies_translation_contract() -> None:
    provider = OllamaCloudProvider(Settings())
    assert isinstance(provider, TranslationProvider)


def test_ollama_cloud_translate_carries_provenance(monkeypatch) -> None:
    seen: dict = {}

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["auth"] = req.get_header("Authorization")
        return _Resp({"message": {"role": "assistant", "content": "Welcome to our mosque"}})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    provider = OllamaCloudProvider(
        Settings(
            providers=ProviderSettings(
                ollama_cloud_endpoint="https://ollama.com",
                ollama_cloud_model="gemma4:31b",
                ollama_cloud_api_key="secret-key",
            )
        )
    )
    result = asyncio.run(provider.translate("مرحبا بكم في مسجدنا", "ar", "en"))

    assert result.text == "Welcome to our mosque"
    assert result.provider == "ollama_cloud"
    assert result.model == "gemma4:31b"
    assert result.target_language == "en"
    assert result.prompt_version == "ollama-cloud-v1"
    assert result.confidence is None  # §166: cloud emits no score; absent != 0.
    assert result.created_at
    # §186 + ADR-0011: posts the Ollama Cloud /api/chat payload.
    assert seen["url"] == "https://ollama.com/api/chat"
    assert seen["body"]["model"] == "gemma4:31b"
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"][1] == {
        "role": "user",
        "content": "مرحبا بكم في مسجدنا",
    }
    # §71: the system prompt forbids inventing citations/commentary.
    assert "never" in seen["body"]["messages"][0]["content"].lower()
    # ADR-0011: cloud is Bearer-authenticated.
    assert seen["auth"] == "Bearer secret-key"



def test_ollama_cloud_omits_local_only_options(monkeypatch) -> None:
    """§186/ADR-0011: local-only tuning must NOT be sent to the cloud.

    keep_alive/num_ctx/num_thread/num_batch are meaningless server-side; sending
    them is at best ignored and at worst a rejected request. The cloud adapter
    instead sends the model name verbatim, ``think`` off and Gemma 4 sampling.
    """
    seen: dict = {}

    def _fake_urlopen(req, timeout=None):
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"message": {"content": "hi"}})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    provider = OllamaCloudProvider(Settings())
    asyncio.run(provider.translate("x", "ar", "en"))

    payload = seen["payload"]
    for local_only in ("keep_alive", "num_ctx", "num_thread", "num_batch"):
        assert local_only not in payload
        assert local_only not in payload.get("options", {})
    # §71: thinking disabled so the response is the translation alone.
    assert payload["think"] is False
    # Gemma 4 best-practice sampling is present and configurable.
    assert payload["options"]["temperature"] == 1.0
    assert payload["options"]["top_p"] == 0.95
    assert payload["options"]["top_k"] == 64


def test_ollama_cloud_outage_is_typed(monkeypatch) -> None:
    def _boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    provider = OllamaCloudProvider(Settings())
    # §0 rule 8 / §87: a failed call raises the typed outage, never a guess.
    with pytest.raises(ProviderOutageError):
        asyncio.run(provider.translate("x", "ar", "en"))


def test_ollama_cloud_http_error_is_typed(monkeypatch) -> None:
    """ADR-0011: a 401 (bad key) / 429 (quota) / 5xx is an outage, not a guess."""

    def _unauthorized(req, timeout=None):
        raise _HTTPError(401, "Unauthorized")

    monkeypatch.setattr("urllib.request.urlopen", _unauthorized)
    provider = OllamaCloudProvider(Settings())
    with pytest.raises(ProviderOutageError):
        asyncio.run(provider.translate("x", "ar", "en"))


def test_ollama_cloud_empty_result_is_typed(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _Resp({"message": {"content": "   "}}),
    )
    provider = OllamaCloudProvider(Settings())
    with pytest.raises(ProviderOutageError):
        asyncio.run(provider.translate("x", "ar", "en"))


def test_ollama_cloud_declares_its_timeout_floor() -> None:
    """§87/§185: the failover budget must not cut a network call off early."""
    provider = OllamaCloudProvider(
        Settings(providers=ProviderSettings(ollama_cloud_timeout_seconds=45.0))
    )
    assert provider.min_timeout_ms == 45_000
    # The floor must also clear the failover primary budget by default.
    assert provider.min_timeout_ms > FailoverSettings().request_timeout_ms


def test_ollama_cloud_registers_in_provider_chain() -> None:
    # §188: the provider name is selectable as primary + fallback like any MT.
    from bayaan_server.providers.mock import build_providers

    providers = build_providers(
        Settings(
            providers=ProviderSettings(
                translation="ollama_cloud", translation_fallback="ollama"
            )
        )
    )
    names = [route.name for route in providers.runners["translation"].routes]
    assert names == ["ollama_cloud", "ollama"]


def test_ollama_cloud_translation_broadcasts_live_caption(tmp_path) -> None:
    """§78 end-to-end: a real cloud translation reaches the public caption.

    Guards the whole chain (provider contract -> §79 validator -> §95 gate) with
    the network stubbed and the real pipeline. Proves the port swap is invisible
    to business logic (§0 rule 4).
    """
    import io
    import struct
    import urllib.request
    import wave

    from bayaan_server.config import DatabaseSettings

    from .conftest import make_client

    arabic = "مرحبا بكم في مسجدنا"

    def _fake_urlopen(req, timeout=None):
        assert "/api/chat" in req.full_url
        return _Resp(
            {"message": {"role": "assistant", "content": "Welcome to our mosque"}}
        )

    original = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen  # type: ignore[assignment]
    try:
        with make_client(
            Settings(
                database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/olc.db"),
                providers=ProviderSettings(
                    translation="ollama_cloud",
                    mock_asr_text=arabic,
                    mock_asr_confidence=0.99,
                ),
            )
        ) as client:
            room = client.post("/api/v1/rooms", json={}).json()
            session = client.post(
                "/api/v1/sessions",
                json={"room_id": room["id"], "active_languages": ["en"]},
            ).json()
            for status in ("READY", "LIVE"):
                client.post(
                    f"/api/v1/sessions/{session['id']}/status",
                    json={"status": status},
                )

            token = client.get(f"/api/v1/rooms/{room['code']}").json()["visitor_token"]
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 8000)
                w.writeframes(b"".join(struct.pack("<h", 3000) for _ in range(9600)))
                w.writeframes(b"\x00\x00" * 16000)

            with client.websocket_connect(
                f"/ws/rooms/{room['code']}?lang=en&token={token}"
            ) as ws:
                assert ws.receive_json()["type"] == "hello"
                client.post(
                    f"/api/v1/sessions/{session['id']}/ingest",
                    files={"file": ("c.wav", buf.getvalue(), "audio/wav")},
                )
                caption = ws.receive_json()
    finally:
        urllib.request.urlopen = original  # type: ignore[assignment]

    target = caption["targets"]["en"]
    assert caption["content_type"] == "GENERAL"
    assert target["route"] == "general_mt"
    assert target["state"] == "ok"
    assert target["text"] == "Welcome to our mosque"
    # §80/§166: no MT score means no speech - never a faked confidence.
    assert target["speak"] is False
