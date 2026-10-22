"""LibreTranslate adapter tests (§63/§69/§186: a real, provider-neutral MT
adapter). Network is stubbed so no server is required (§186 isolation)."""

from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest

from bayaan_server.config import ProviderSettings, Settings
from bayaan_server.providers.errors import ProviderOutageError
from bayaan_server.providers.interfaces import TranslationProvider
from bayaan_server.providers.libre import LibreTranslateProvider


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_libre_satisfies_translation_contract() -> None:
    provider = LibreTranslateProvider(Settings())
    assert isinstance(provider, TranslationProvider)


def test_libre_translate_carries_provenance(monkeypatch) -> None:
    seen: dict = {}

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"translatedText": "ciao mondo"})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    provider = LibreTranslateProvider(
        Settings(providers=ProviderSettings(libre_endpoint="http://lt"))
    )
    result = asyncio.run(provider.translate("مرحبا بالعالم", "ar", "it"))

    assert result.text == "ciao mondo"
    assert result.provider == "libre"
    assert result.target_language == "it"
    assert result.prompt_version == "libre-v1"
    assert result.created_at
    # §186: the adapter posts a LibreTranslate-compatible payload.
    assert seen["url"] == "http://lt/translate"
    assert seen["body"]["q"] == "مرحبا بالعالم"
    assert seen["body"]["source"] == "ar"
    assert seen["body"]["target"] == "it"


def test_libre_outage_is_typed(monkeypatch) -> None:
    def _boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    provider = LibreTranslateProvider(Settings())
    # §0 rule 8 / §87: a failed call raises the typed outage, never a guess.
    with pytest.raises(ProviderOutageError):
        asyncio.run(provider.translate("x", "ar", "en"))


def test_libre_empty_result_is_typed(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _Resp({"translatedText": ""}),
    )
    provider = LibreTranslateProvider(Settings())
    with pytest.raises(ProviderOutageError):
        asyncio.run(provider.translate("x", "ar", "en"))
