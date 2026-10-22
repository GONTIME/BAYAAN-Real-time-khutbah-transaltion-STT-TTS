"""Provider contract tests (§183: mock provider before real adapter).

Business logic sees only interfaces; mocks prove the contracts are usable
without any network access (§186). §195 Phase 6: the registry returns
failover-wrapped ports that still satisfy every protocol.
"""

from __future__ import annotations

import asyncio

from bayaan_server.config import Settings
from bayaan_server.providers.interfaces import ASRProvider, TTSProvider, TranslationProvider
from bayaan_server.providers.mock import build_providers


def test_mocks_satisfy_their_contracts() -> None:
    providers = build_providers(Settings())
    assert isinstance(providers.asr, ASRProvider)
    assert isinstance(providers.translation, TranslationProvider)
    assert isinstance(providers.tts, TTSProvider)
    # §87: the registry exposes one failover runner per capability.
    assert set(providers.runners) == {"asr", "translation", "tts"}


def test_mock_translation_carries_full_provenance() -> None:
    providers = build_providers(Settings())
    result = asyncio.run(
        providers.translation.translate("نص تجريبي", "ar", "it")
    )
    assert result.provider == "mock"
    assert result.model
    assert result.prompt_version
    assert result.glossary_version
    assert result.target_language == "it"


def test_mock_never_fakes_real_content() -> None:
    providers = build_providers(Settings())
    result = asyncio.run(providers.asr.transcribe(b"\x00\x01", "ar"))
    assert "MOCK" in result.text  # unmistakably a mock output (§0 rule 8)