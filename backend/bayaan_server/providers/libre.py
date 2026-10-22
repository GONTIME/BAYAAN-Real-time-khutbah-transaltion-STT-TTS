"""LibreTranslate adapter (BAYAAN_MASTER_SPEC.md §63, §69, §186).

A real, provider-neutral machine-translation adapter that talks to a
LibreTranslate-compatible HTTP endpoint. It is the production counterpart to
the mock provider: business logic depends only on the ``TranslationProvider``
port (§0 rule 4 / §63), all network I/O lives here (§186), and every result
carries full §69 provenance.

Self-hostable -> runs offline inside a mosque with no AI-vendor lock-in; swap
``BAYAAN_PROVIDERS__LIBRE_ENDPOINT`` for any compatible server. A provider that
cannot answer raises ``ProviderOutageError`` so the §87 failover layer moves to
the fallback - it never fabricates a translation (§0 rule 8).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from bayaan_server.config import Settings
from bayaan_server.providers.errors import ProviderOutageError

from .interfaces import TranslationResult

LIBRE_PROVIDER_NAME = "libre"


class LibreTranslateProvider:
    """Real MT over a LibreTranslate-compatible ``POST /translate`` endpoint."""

    def __init__(self, settings: Settings) -> None:
        cfg = settings.providers
        self._endpoint = cfg.libre_endpoint.rstrip("/")
        self._timeout = cfg.libre_timeout_seconds
        self._api_key = cfg.libre_api_key
        self._prompt_version = cfg.libre_prompt_version
        # §87/§185: same adapter-declared floor as the Ollama adapter - the
        # failover budget never cuts a call off before this adapter's own
        # configured timeout (a cold LibreTranslate container is also slow to
        # answer its first request).
        self.min_timeout_ms = int(self._timeout * 1000)

    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> TranslationResult:
        payload: dict[str, str] = {
            "q": text,
            "source": source_language,
            "target": target_language,
            "format": "text",
        }
        if self._api_key:
            payload["api_key"] = self._api_key
        body = json.dumps(payload).encode("utf-8")

        def _call() -> dict:
            req = urllib.request.Request(
                f"{self._endpoint}/translate",
                data=body,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _call)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProviderOutageError(
                LIBRE_PROVIDER_NAME, f"translation request failed: {exc}"
            ) from exc

        translated = data.get("translatedText")
        if not translated:
            raise ProviderOutageError(
                LIBRE_PROVIDER_NAME, "endpoint returned no translated text"
            )

        # §69: provenance is explicit. LibreTranslate emits no confidence, so it
        # stays None (absent != 0.0, §0 rule 8 / §166); the draft path is
        # human-reviewed (§96) and the live path gates the remaining signals
        # (§80/§166). TTS stays silent without an MT score (never faked).
        return TranslationResult(
            text=translated,
            source_language=source_language,
            target_language=target_language,
            confidence=None,
            provider=LIBRE_PROVIDER_NAME,
            model=self._endpoint,
            prompt_version=self._prompt_version,
            glossary_version="gv0",
            retrieval_version="none",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
