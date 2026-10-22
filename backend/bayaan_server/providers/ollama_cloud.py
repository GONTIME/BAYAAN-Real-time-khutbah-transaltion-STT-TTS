"""Ollama Cloud adapter — remote LLM translation (spec §63, §69, §71, §186, ADR-0011).

A real, provider-neutral machine-translation adapter that talks to the
**Ollama Cloud** ``POST /api/chat`` endpoint (``https://ollama.com``). It is the
frontier-quality counterpart to the local ``ollama`` adapter for the *general*
(non-sacred) path: business logic depends only on the ``TranslationProvider``
port (§0 rule 4 / §63), all network I/O lives here (§186), and every result
carries full §69 provenance.

Why a cloud model over the bundled LibreTranslate/Argos MT and the local
``aya:8b``: Argos/Opus-MT are literal and weak on Arabic religious register,
and even the local LLM trades fluency for the RAM budget of a small box. A
frontier cloud model (``gemma4:31b``, MMMLU 88.4% multilingual) produces
noticeably more fluent ar→en/it text. The trade is explicit and is an operator
decision (ADR-0011): khutbah text leaves the mosque to Ollama's cloud, which
"processes but does not train on" prompts/responses, so it must stay a
selectable adapter (default remains local) with a local fallback chain — never
the silent default.

Model selection is configuration, not code (§185 / §0 rule 4): the endpoint,
model name, API key, timeout and sampling all come from ``ProviderSettings``, so
later models/providers are added as config + a factory entry, and this port is
never touched.

Sacred text is still never machine-translated: this adapter only serves the
general/live and hadith-general fallback paths (§78/§86). Quran is retrieved
from the approved SQL store (§206). A call that cannot answer raises the typed
``ProviderOutageError`` so the §87 failover layer moves to the fallback — it
never fabricates a translation (§0 rule 8).
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

OLLAMA_CLOUD_PROVIDER_NAME = "ollama_cloud"

# §71 prompt policy, enforced inside the adapter (not business logic): the model
# must emit ONLY the translation — no commentary, no citations, no added rulings,
# no explanations. Kept identical to the local ``ollama`` adapter (§0 rule 8:
# the same translate-only guarantee regardless of where inference runs). This
# keeps hallucinated religious attribution off the caption even before the §79
# validator and §80 TTS gate run.
_SYSTEM_PROMPT = (
    "You are a professional Arabic-to-{language} translator for a mosque "
    "sermon (khutbah). Translate the user's Arabic text into {language}. "
    "Rules: (1) output ONLY the translation, nothing else; (2) do not add "
    "explanations, commentary, transliteration, or quotation marks; (3) never "
    "cite or invent Quran/Hadith references; (4) never add religious rulings, "
    "interpretations, or opinions; (5) preserve the meaning faithfully and keep "
    "well-known Islamic terms (e.g. Salah, Zakat, Ramadan, Masjid) as-is."
)

# Human-readable target names so the prompt stays natural; unknown codes fall
# back to the raw ISO code rather than a guess. Same map as the local adapter.
_LANGUAGE_NAMES = {
    "en": "English",
    "it": "Italian",
    "ar": "Arabic",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "ur": "Urdu",
    "id": "Indonesian",
    "tr": "Turkish",
}


class OllamaCloudProvider:
    """Frontier-quality translation over the Ollama Cloud ``/api/chat`` endpoint.

    Provider-neutral (§63): the concrete endpoint/model/credentials are injected
    from ``Settings`` (§185), so swapping model or even vendor is config, not a
    code change. Cloud transport differs from the local adapter in three ways,
    all verified against Ollama's Cloud docs (ADR-0011):
      * auth — every request carries ``Authorization: Bearer <api_key>``;
      * no local-only options — ``keep_alive``/``num_ctx``/``num_thread``/
        ``num_batch`` are meaningless server-side and are NOT sent;
      * thinking off by default — the model emits translation-only (honours §71
        and keeps latency/cost down); toggled via ``ollama_cloud_thinking``.
    """

    def __init__(self, settings: Settings) -> None:
        cfg = settings.providers
        self._endpoint = cfg.ollama_cloud_endpoint.rstrip("/")
        self._model = cfg.ollama_cloud_model
        self._api_key = cfg.ollama_cloud_api_key
        self._timeout = cfg.ollama_cloud_timeout_seconds
        self._prompt_version = cfg.ollama_cloud_prompt_version
        self._temperature = cfg.ollama_cloud_temperature
        self._top_p = cfg.ollama_cloud_top_p
        self._top_k = cfg.ollama_cloud_top_k
        self._thinking = cfg.ollama_cloud_thinking
        # §87/§185: the failover budget must not cut a call off before this
        # adapter's own timeout. A cloud call is network-bound (TLS + queue +
        # inference), so the adapter declares that floor and the runner extends
        # its budget to match — otherwise a legitimately slow generation is
        # misreported as an outage.
        self.min_timeout_ms = int(self._timeout * 1000)

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        # Cloud is key-authenticated (ADR-0011); an empty key means misconfig —
        # send it anyway so the server returns a typed 401 we surface, rather
        # than fabricating a translation (§0 rule 8).
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_payload(self, text: str, target_language: str) -> dict:
        language = _LANGUAGE_NAMES.get(target_language, target_language)
        return {
            "model": self._model,
            "stream": False,
            # Top-level per the Ollama chat API: disables the reasoning channel
            # so the response is the translation alone (§71). Gemma 4 supports
            # standard system/user roles, so the §71 prompt rides as the system
            # message unchanged from the local adapter.
            "think": self._thinking,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT.format(language=language)},
                {"role": "user", "content": text},
            ],
            # Gemma 4 best-practice sampling (docs.ollama.com); values are
            # config (§185) so a pair can be re-tuned without a code change.
            "options": {
                "temperature": self._temperature,
                "top_p": self._top_p,
                "top_k": self._top_k,
            },
        }

    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> TranslationResult:
        payload = self._build_payload(text, target_language)
        body = json.dumps(payload).encode("utf-8")
        headers = self._headers()

        def _call() -> dict:
            req = urllib.request.Request(
                f"{self._endpoint}/api/chat",
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _call)
        except urllib.error.HTTPError as exc:
            # 401/403 (bad key), 429 (quota), 5xx (upstream) — all outages for
            # the failover layer, with the HTTP status for ops alerting.
            raise ProviderOutageError(
                OLLAMA_CLOUD_PROVIDER_NAME,
                f"cloud HTTP {exc.code}: {exc.reason}",
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProviderOutageError(
                OLLAMA_CLOUD_PROVIDER_NAME, f"cloud request failed: {exc}"
            ) from exc

        translated = (data.get("message") or {}).get("content")
        if not translated or not translated.strip():
            raise ProviderOutageError(
                OLLAMA_CLOUD_PROVIDER_NAME, "model returned no translation"
            )

        # §69: provenance is explicit. Cloud emits no confidence, so it stays
        # None (absent != 0.0, §0 rule 8 / §166); the live path gates the
        # remaining signals (§80/§166) and the draft path is human-reviewed
        # (§96). TTS stays silent without an MT score (never faked).
        return TranslationResult(
            text=translated.strip(),
            source_language=source_language,
            target_language=target_language,
            confidence=None,
            provider=OLLAMA_CLOUD_PROVIDER_NAME,
            model=self._model,
            prompt_version=self._prompt_version,
            glossary_version="gv0",
            retrieval_version="none",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

