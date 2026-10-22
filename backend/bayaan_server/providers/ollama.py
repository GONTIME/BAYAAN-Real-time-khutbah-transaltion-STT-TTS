"""Ollama adapter — local LLM translation (BAYAAN_MASTER_SPEC.md §63, §69, §71, §186).

A real, provider-neutral machine-translation adapter that talks to an
**Ollama-compatible** local LLM (``POST /api/chat``). It is the higher-quality
counterpart to the LibreTranslate adapter for the *general* (non-sacred) path:
business logic depends only on the ``TranslationProvider`` port (§0 rule 4 /
§63), all network I/O lives here (§186), and every result carries full §69
provenance.

Why a local LLM over the bundled LibreTranslate/Argos MT: Argos/Opus-MT models
are small and fast but literal and weak on Arabic morphology and religious
register; a general-purpose LLM (Aya/Qwen2.5, both free and Arabic-strong)
produces noticeably more fluent ar→en/it text and can honour the glossary via
prompt. It stays **self-hosted and offline** — no API key, no vendor lock-in,
no khutbah text leaving the mosque (§58 privacy) — so it fits the pilot where
LibreTranslate is not good enough but an online API is not acceptable.

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

OLLAMA_PROVIDER_NAME = "ollama"

# §71 prompt policy, enforced inside the adapter (not business logic): the model
# must emit ONLY the translation — no commentary, no citations, no added rulings,
# no explanations. This keeps hallucinated religious attribution off the caption
# even before the §79 validator and §80 TTS gate run.
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
# back to the raw ISO code rather than a guess.
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


def _parse_keep_alive(raw: str) -> int | str:
    """§185: ``keep_alive`` accepts ``-1`` (pin forever) or a Go duration.

    Ollama takes an integer second count OR a duration string like ``5m``. We
    keep it a plain string in settings (so ``-1`` and ``10m`` both work) and
    resolve it here: a bare integer is sent through as an int, anything else is
    passed on verbatim for Ollama to parse.
    """
    text = (raw or "").strip()
    if not text:
        return -1
    try:
        return int(text)
    except ValueError:
        return text


class OllamaProvider:
    """Real LLM translation over an Ollama-compatible ``POST /api/chat`` endpoint."""

    def __init__(self, settings: Settings) -> None:
        cfg = settings.providers
        self._endpoint = cfg.ollama_endpoint.rstrip("/")
        self._model = cfg.ollama_model
        self._timeout = cfg.ollama_timeout_seconds
        self._temperature = cfg.ollama_temperature
        self._prompt_version = cfg.ollama_prompt_version
        self._keep_alive = _parse_keep_alive(cfg.ollama_keep_alive)
        self._num_ctx = cfg.ollama_num_ctx
        self._num_thread = cfg.ollama_num_thread
        self._num_batch = cfg.ollama_num_batch
        # §87/§185: the failover budget must not cut a call off before this
        # adapter's own timeout. A local LLM loads its weights on the first
        # call (~15s observed for aya:8b cold start), so the adapter declares
        # that floor and the runner extends its budget to match - otherwise a
        # legitimate cold start is misreported as an outage.
        self.min_timeout_ms = int(self._timeout * 1000)

    def _build_payload(self, text: str, target_language: str) -> dict:
        language = _LANGUAGE_NAMES.get(target_language, target_language)
        options: dict = {"temperature": self._temperature}
        # §204: measured tuning. num_ctx keeps the KV cache small (a khutbah
        # segment is a few sentences); num_thread 0 means "let Ollama decide".
        if self._num_ctx:
            options["num_ctx"] = self._num_ctx
        if self._num_thread:
            options["num_thread"] = self._num_thread
        if self._num_batch:
            options["num_batch"] = self._num_batch
        return {
            "model": self._model,
            "stream": False,
            # §204: pinning the model is the dominant latency win on this box.
            "keep_alive": self._keep_alive,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT.format(language=language)},
                {"role": "user", "content": text},
            ],
            "options": options,
        }

    async def translate(
        self, text: str, source_language: str, target_language: str
    ) -> TranslationResult:
        payload = self._build_payload(text, target_language)
        body = json.dumps(payload).encode("utf-8")

        def _call() -> dict:
            req = urllib.request.Request(
                f"{self._endpoint}/api/chat",
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
                OLLAMA_PROVIDER_NAME, f"translation request failed: {exc}"
            ) from exc

        translated = (data.get("message") or {}).get("content")
        if not translated or not translated.strip():
            raise ProviderOutageError(
                OLLAMA_PROVIDER_NAME, "model returned no translation"
            )

        # §69: provenance is explicit. Ollama emits no confidence, so it stays
        # None (absent != 0.0, §0 rule 8 / §166); the live path gates the
        # remaining signals (§80/§166) and the draft path is human-reviewed
        # (§96). TTS stays silent without an MT score (never faked).
        return TranslationResult(
            text=translated.strip(),
            source_language=source_language,
            target_language=target_language,
            confidence=None,
            provider=OLLAMA_PROVIDER_NAME,
            model=self._model,
            prompt_version=self._prompt_version,
            glossary_version="gv0",
            retrieval_version="none",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
