"""Ollama adapter tests (§63/§69/§186: a real, provider-neutral LLM adapter).

Network is stubbed so no Ollama server is required (§186 isolation).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest

from bayaan_server.config import ProviderSettings, Settings
from bayaan_server.providers.errors import ProviderOutageError
from bayaan_server.providers.interfaces import TranslationProvider, TranslationResult
from bayaan_server.providers.ollama import OllamaProvider


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_ollama_satisfies_translation_contract() -> None:
    provider = OllamaProvider(Settings())
    assert isinstance(provider, TranslationProvider)


def test_ollama_translate_carries_provenance(monkeypatch) -> None:
    seen: dict = {}

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"message": {"role": "assistant", "content": "Peace be upon you"}})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    provider = OllamaProvider(
        Settings(
            providers=ProviderSettings(
                ollama_endpoint="http://ollama",
                ollama_model="aya:8b",
            )
        )
    )
    result = asyncio.run(provider.translate("السلام عليكم", "ar", "en"))

    assert result.text == "Peace be upon you"
    assert result.provider == "ollama"
    assert result.model == "aya:8b"
    assert result.target_language == "en"
    assert result.prompt_version == "ollama-v1"
    assert result.created_at
    # §186: the adapter posts an Ollama-compatible /api/chat payload.
    assert seen["url"] == "http://ollama/api/chat"
    assert seen["body"]["model"] == "aya:8b"
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"][0]["role"] == "system"
    assert seen["body"]["messages"][1] == {"role": "user", "content": "السلام عليكم"}
    # §71: the system prompt forbids inventing citations/commentary.
    assert "never" in seen["body"]["messages"][0]["content"].lower()


def test_ollama_outage_is_typed(monkeypatch) -> None:
    def _boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    provider = OllamaProvider(Settings())
    # §0 rule 8 / §87: a failed call raises the typed outage, never a guess.
    with pytest.raises(ProviderOutageError):
        asyncio.run(provider.translate("x", "ar", "en"))


def test_ollama_empty_result_is_typed(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _Resp({"message": {"content": "   "}}),
    )
    provider = OllamaProvider(Settings())
    with pytest.raises(ProviderOutageError):
        asyncio.run(provider.translate("x", "ar", "en"))


def test_ollama_pins_the_model_to_avoid_reloads() -> None:
    """§204 regression: the adapter must send ``keep_alive``.

    Ollama unloads an idle model after ~5 minutes, so without an explicit
    keep_alive the first utterance after any gap pays a full ~15s reload - worse
    than the ~4s of inference and visible as a mid-khutbah stall.
    """
    import urllib.request

    seen: dict = {}

    def _fake_urlopen(req, timeout=None):
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"message": {"role": "assistant", "content": "Peace be upon you"}})

    original = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen  # type: ignore[assignment]
    try:
        asyncio.run(
            OllamaProvider(Settings()).translate("\u0645\u0631\u062d\u0628\u0627", "ar", "en")
        )
    finally:
        urllib.request.urlopen = original  # type: ignore[assignment]

    payload = seen["payload"]
    # -1 pins the model in RAM for the life of the server.
    assert payload["keep_alive"] == -1
    # §204: small context, explicit batch; threads omitted (0 = auto).
    assert payload["options"]["num_ctx"] == 2048
    assert payload["options"]["num_batch"] == 512
    assert "num_thread" not in payload["options"]


def test_ollama_keep_alive_accepts_duration_strings() -> None:
    """§185: operators may pin forever (-1) or use a Go duration like ``10m``."""
    from bayaan_server.config import ProviderSettings

    from bayaan_server.providers.ollama import _parse_keep_alive

    assert _parse_keep_alive("-1") == -1
    assert _parse_keep_alive("10m") == "10m"
    assert _parse_keep_alive("  3600 ") == 3600
    assert _parse_keep_alive("") == -1

    pinned = OllamaProvider(
        Settings(providers=ProviderSettings(ollama_keep_alive="30m"))
    )
    assert pinned._keep_alive == "30m"


def test_ollama_declares_its_timeout_floor() -> None:
    """§87/§185 regression: the failover budget must not cut the LLM off early.

    The default failover budget (10s) is shorter than a local LLM cold start,
    so the adapter declares a floor from its own configured timeout and the
    runner extends the budget to it. Without this the first call always failed
    over as a phantom outage.
    """
    provider = OllamaProvider(
        Settings(providers=ProviderSettings(ollama_timeout_seconds=45.0))
    )
    assert provider.min_timeout_ms == 45_000
    # The floor must also clear the failover primary budget by default.
    from bayaan_server.config import FailoverSettings

    assert provider.min_timeout_ms > FailoverSettings().request_timeout_ms


def test_failover_budget_is_extended_to_the_adapter_floor() -> None:
    """§87: a slow adapter's own timeout wins over the fast MT default."""
    import asyncio

    from bayaan_server.domain.health import CircuitBreaker
    from bayaan_server.metrics import MetricsRegistry
    from bayaan_server.ops.alerts import AlertBus
    from bayaan_server.providers.failover import FailoverRunner, ProviderRoute
    from bayaan_server.providers.interfaces import TranslationResult

    class SlowLLM:
        """Sleeps past the default 10s failover budget but inside its own floor."""

        min_timeout_ms = 40_000

        async def translate(self, text, source, target) -> TranslationResult:
            await asyncio.sleep(12.0)
            return TranslationResult(
                text="late but valid",
                source_language=source,
                target_language=target,
                confidence=None,
                provider="slow",
                model="m",
                prompt_version="v1",
                glossary_version="gv0",
                retrieval_version="none",
                created_at="now",
            )

    settings = Settings()  # default 10s primary budget
    runner = FailoverRunner(
        "translation",
        [ProviderRoute("slow", SlowLLM(), CircuitBreaker(1, 60_000))],
        settings,
        AlertBus(settings.alerts),
        MetricsRegistry(),
    )
    result = asyncio.run(runner.call(lambda p: p.translate("x", "ar", "en")))
    assert result.text == "late but valid"


def test_translation_result_confidence_is_optional() -> None:
    """§0 rule 8 / §166 regression: an adapter that emits no score reports None.

    ``confidence`` used to be a required float, so ``confidence=None`` raised a
    pydantic ValidationError that the failover layer turned into a chain-wide
    outage - every Ollama translation failed.
    """
    result = TranslationResult(
        text="hello",
        source_language="ar",
        target_language="en",
        confidence=None,
        provider="ollama",
        model="aya:8b",
        prompt_version="ollama-v1",
        glossary_version="gv0",
        retrieval_version="none",
        created_at="now",
    )
    assert result.confidence is None
    # Absent is NOT zero: the §167 composite must exclude it, not score it 0.
    from bayaan_server.pipeline.confidence import translation_confidence

    assert translation_confidence(None) is None


def test_ollama_registers_in_provider_chain() -> None:
    # §188: the provider name is selectable as primary + fallback like any MT.
    from bayaan_server.providers.mock import build_providers

    providers = build_providers(
        Settings(
            providers=ProviderSettings(translation="ollama", translation_fallback="mock")
        )
    )
    names = [route.name for route in providers.runners["translation"].routes]
    assert names == ["ollama", "mock"]


def test_ollama_translation_broadcasts_live_caption(tmp_path) -> None:
    """§78 end-to-end: a real Ollama translation reaches the public caption.

    Guards the whole chain (provider contract -> §79 validator -> §95 gate) so a
    contract mismatch can never silently degrade every live caption again.
    Network is stubbed; the pipeline itself is the real one.
    """
    import io
    import struct
    import urllib.request
    import wave

    from bayaan_server.config import DatabaseSettings
    from fastapi.testclient import TestClient

    from .conftest import make_client

    arabic = "مرحبا بكم في مسجدنا"

    def _fake_urlopen(req, timeout=None):
        assert "/api/chat" in req.full_url
        return _Resp({"message": {"role": "assistant", "content": "Welcome to our mosque"}})

    original = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen  # type: ignore[assignment]
    try:
        with make_client(
            Settings(
                database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/ol.db"),
                providers=ProviderSettings(
                    translation="ollama",
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
                    f"/api/v1/sessions/{session['id']}/status", json={"status": status}
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
