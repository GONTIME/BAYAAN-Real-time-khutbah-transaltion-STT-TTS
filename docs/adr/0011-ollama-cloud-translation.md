# ADR-0011: Ollama Cloud translation adapter (frontier MT, opt-in)

**Status:** Accepted

**Context:** §63 translation is provider-neutral and today offers three MT
adapters for the *general* (non-sacred) path: `mock`, `libre` (LibreTranslate /
Argos Opus-MT) and `ollama` (a self-hosted local LLM, default model `aya:8b`).
None satisfied the pilot's needs on both axes: Argos/Opus-MT are small and fast
but literal and weak on Arabic morphology and religious register, and the local
`aya:8b` trades fluency for the RAM budget of a mosque box. The acceptance need
was **higher-quality ar→en/it output** for a live khutbah caption.

Ollama ships a **Cloud API** (`https://ollama.com/api/chat`, Bearer-key
authenticated, no install required) and a frontier multilingual model,
`gemma4:31b` (MMMLU 88.4%, 256K context, native `system`-role support, with a
configurable thinking mode). Google also publishes `translategemma` (4B/12B/27B,
built on Gemma 3, 55 languages including Arabic) as a purpose-built translation
alternative for a later A/B. This ADR adds the cloud path as a **selectable,
config-driven adapter** — never the silent default — because it is the one MT
option that deviates from §58.

**The §58 tradeoff is explicit and is the point of this ADR.** §58 states khutbah
text must never leave the mosque. A cloud model contradicts that: the Arabic
segment is sent to Ollama's cloud, which states it *processes* prompts/responses
to answer requests and does **not** train on them. For a mosque that values
fidelity over strict on-prem data residency this is a reasonable, operator-owned
trade — but it is a **deviation**, so it must be an opt-in adapter with a local
fallback, not the default provider and not the only option.

**Decisions:**

1. **A new `TranslationProvider` adapter, selected by name like any other MT.**
   `providers/ollama_cloud.py::OllamaCloudProvider` implements the existing
   `TranslationProvider` port (§63/§0 rule 4). Business logic is unchanged: it is
   registered under the name `ollama_cloud` in `providers/mock.build_providers`
   and selectable as `providers.translation` / `providers.translation_fallback`
   (§188). Routing, the §79 validator, the §80 TTS gate and provenance all work
   with zero change. The `TranslationProvider` port is not touched.

2. **Cloud is opt-in; the default and the safe fallback stay local.**
   `translation` still defaults to `mock` and the local `ollama`/`libre` adapters
   remain available. A mosque that opts in should set
   `BAYAAN_PROVIDERS__TRANSLATION=ollama_cloud` with a **local fallback**
   (`...TRANSLATION_FALLBACK=ollama`) so a cloud outage degrades to local MT, not
   to SAFE_UNAVAILABLE (§87 failover already does this per-circuit).

3. **Model/provider selection is configuration, not code (§185).**
   `ollama_cloud_endpoint`, `ollama_cloud_model` (default `gemma4:31b`),
   `ollama_cloud_api_key`, timeout and sampling (`temperature`/`top_p`/`top_k`,
   Gemma-4 best-practice defaults) live in `ProviderSettings`. Adding another
   cloud model — e.g. a future `translategemma` cloud tag or a different vendor —
   is a value change plus (at most) one factory entry; the adapter and the port
   are untouched. The API key is injected via
   `BAYAAN_PROVIDERS__OLLAMA_CLOUD_API_KEY` and never committed (§64).

4. **Cloud transport is correct and isolated in the adapter (§186).**
   The request carries `Authorization: Bearer <key>`, posts the Ollama
   `/api/chat` payload, and **omits local-only options** (`keep_alive`,
   `num_ctx`, `num_thread`, `num_batch`) that are meaningless server-side.
   **Thinking is disabled by default** (`think: false`) so the model returns the
   translation alone — this honours §71 (translate-only) and keeps latency/cost
   down; the reasoning channel is a diagnostics-only toggle. The §71 system
   prompt is kept identical to the local adapter, so the "no citations, no
   rulings, translation only" guarantee is the same wherever inference runs.

5. **Failure is typed and never faked (§0 rule 8).**
   A network error, an HTTP error (401 bad key, 429 quota, 5xx upstream) or an
   empty response raises `ProviderOutageError`, which the §87 layer turns into a
   fallback attempt and, if every provider fails, SAFE_UNAVAILABLE. The adapter
   declares its `min_timeout_ms` from its own configured timeout so the failover
   budget does not cut a legitimate cloud round-trip short. Provenance is full
   §69 (`provider="ollama_cloud"`, `model`, `prompt_version`); cloud emits no
   confidence so it stays `None` (§166) and TTS stays silent without an MT score.

6. **Sacred text still never reaches the model.**
   This adapter only serves the general/live and hadith-general fallback paths
   (§78/§86); Quran is retrieved from the approved SQL store (§206) and never
   machine-translated. The privacy deviation is therefore bounded to *general*
   sermon speech, which is the smallest possible surface for an opt-in cloud MT.

7. **Verification without a live key.**
   `tests/test_ollama_cloud_provider.py` stubs the network (§186) and pins the
   contract, provenance, Bearer auth, absence of local-only options, thinking
   off, typed outages on URLError and HTTP 401, the timeout floor, provider-chain
   registration, and an end-to-end live-caption test through the real pipeline.
   `tools/ollama_cloud_check.py` is the operator A/B harness: it drives a real
   utterance to the real cloud and prints the caption + route so an operator can
   confirm quality/latency (and compare against a later `translategemma` run)
   before enabling it for a khutbah.

**Consequences / follow-ups:**
- Operators must supply an API key and consciously accept the §58 deviation.
- Cost and latency are now usage-dependent (~$0.14/1M input, $0.40/1M output;
  network round-trip); the timeout floor and local fallback bound the blast
  radius.
- Recommended next step for the pilot: run `tools/ollama_cloud_check.py` against
  real khutbah utterances for `gemma4:31b` **and** a `translategemma` cloud/local
  run, and record which wins on ar→en/it fidelity in this ADR before switching a
  mosque over. No code change is needed to run that comparison — only config.

