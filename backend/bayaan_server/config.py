"""Application configuration.

BAYAAN_MASTER_SPEC.md rules implemented here:
- §185 No Magic Values: every threshold lives in typed settings, never inline.
- §184 No Silent Assumptions: every external dependency (DB, providers,
  audio device) is an explicit, validated setting - never a hardcoded guess.
- §0 rule 4: provider selection is configuration, not business logic.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfidenceWeights(BaseSettings):
    """Weights for composite confidence (§167).

    overall_confidence = weighted_function(
        asr_confidence,
        alignment_confidence,
        translation_confidence,
        validation_score,
    )
    """

    asr: float = Field(default=0.25, ge=0.0, le=1.0)
    alignment: float = Field(default=0.25, ge=0.0, le=1.0)
    translation: float = Field(default=0.35, ge=0.0, le=1.0)
    validation: float = Field(default=0.15, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "ConfidenceWeights":
        # §167: weights must define a proper weighted average.
        total = self.asr + self.alignment + self.translation + self.validation
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"Confidence weights must sum to 1.0 (got {total:.6f})"
            )
        return self


class SafetySettings(BaseSettings):
    """Safety thresholds (§166-§168).

    Safety blockers are never overridden by an overall average (§167):
    any per-signal breach below forces BLOCK regardless of overall score.

    Signals whose engines do not exist yet (alignment -> Phase 4 §193,
    validators -> Phase 4) are disabled via flags instead of being faked
    with silent zero scores; disabled signals are excluded from the
    composite and from threshold enforcement.
    """

    asr_min_confidence: float = Field(default=0.60, ge=0.0, le=1.0)
    alignment_min_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    translation_min_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    tts_min_confidence: float = Field(default=0.90, ge=0.0, le=1.0)
    # Composite threshold (§167): below it the output becomes FALLBACK,
    # never an automatic broadcast (§0 rule 5).
    overall_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    weights: ConfidenceWeights = Field(default_factory=ConfidenceWeights)

    alignment_enabled: bool = True  # §77 aligner engine lands in Phase 4 (§193)
    validation_enabled: bool = True  # §79 validator chain lands in Phase 4 (§193)

    tts_enabled: bool = False  # §201 text-first; TTS locked by default (§174)
    fallback_show_arabic: bool = True  # §92: fallback instead of guessing


class TtsSettings(BaseSettings):
    """TTS output settings (§88, §194 Phase 5, §201 text-first).

    TTS is locked OFF by default (§201): the first release is
    ``ASR -> safe translation -> live captions``. When enabled, every spoken
    segment must still pass the §80 gate - a low-confidence or unsafe
    translation never becomes audio (§0 rule 5).

    The queue is bounded (§149: never infinite) and drops audio that has gone
    stale (§80 ``MAX_TTS_STALENESS``): speaking an old line after the imam has
    moved on is worse than silence, so a stale job is discarded, not played.

    The master on/off switch is ``SafetySettings.tts_enabled`` (single switch,
    used by preflight); this block holds the operational tuning only.
    """

    max_staleness_ms: int = Field(default=8000, ge=0)  # §88 / §80
    queue_maxsize: int = Field(default=32, ge=1)  # §149 bounded queue
    sample_rate: int = Field(default=24000, ge=8000, le=96000)
    # Cap synthesized length so one utterance cannot monopolize playback
    # (§185: no magic value - configurable, heuristic lives in the mock).
    max_audio_seconds: float = Field(default=12.0, gt=0.0)
    # §80/§166: real MT providers (ollama / ollama_cloud / libre) emit NO
    # confidence score (§0 rule 8: never fabricate one), which the §80 gate
    # otherwise treats as not-speakable. This flag is the explicit operator
    # opt-in: when False, a MISSING provider confidence no longer blocks TTS
    # (the other §80 rules - safety ALLOW, validation ALLOW, sacred-text
    # reference, freshness, non-empty text - still apply; and a NUMERIC
    # confidence below tts_min_confidence is ALWAYS refused regardless).
    # Default True keeps the strict behaviour for providers that cannot score.
    require_translation_confidence: bool = Field(default=True)


class FailoverSettings(BaseSettings):
    """Resilience tuning (§54 circuit breaker, §87 failover, §111 watchdog).

    Every budget/threshold is explicit configuration (§185) - the failover
    layer never hardcodes a timeout or a retry count. §54: a provider's
    circuit opens after ``failure_threshold`` consecutive failures, stays OPEN
    for ``cooldown_ms``, probes HALF_OPEN, and re-opens on a failed probe -
    retries are bounded, never indefinite.
    """

    request_timeout_ms: int = Field(default=10_000, ge=100)   # primary call budget (§87 task.timeout)
    fallback_timeout_ms: int = Field(default=4_000, ge=100)   # §87 short_timeout for fallbacks
    # §63/§87: LLM translation (Ollama) needs a larger budget than ASR/TTS -
    # cold model load alone is ~10-15s. Per-capability budgets keep ASR/TTS
    # tight while letting the translation chain use its own timeouts (§185).
    translation_request_timeout_ms: int = Field(default=65_000, ge=100)
    translation_fallback_timeout_ms: int = Field(default=30_000, ge=100)
    failure_threshold: int = Field(default=3, ge=1)           # consecutive failures before OPEN
    cooldown_ms: int = Field(default=30_000, ge=100)          # OPEN -> HALF_OPEN wait
    heartbeat_timeout_ms: int = Field(default=15_000, ge=1_000)  # §111 staleness threshold
    watchdog_interval_ms: int = Field(default=5_000, ge=100)  # §111 polling cadence


class AlertsSettings(BaseSettings):
    """Alert delivery (§195 Alerts; §179 operational separation of concerns).

    Alerts report degraded provider state to the operator; they never gate
    captions. Channel is a structured log + metrics now; ``webhook_url`` is the
    documented placeholder for a real endpoint (ADR-0007) - empty means no
    outbound delivery, the operator still sees alerts in the log feed.
    """

    enabled: bool = True
    min_severity: Literal["info", "warning", "error"] = "warning"
    webhook_url: str = ""


class TelegramSettings(BaseSettings):
    """Telegram caption mirror (ADR-0016; §107 non-blocking, §134 public text).

    When enabled, every finalized caption - the SAME post-gate event the room
    WebSocket receives - is mirrored to a Telegram bot chat or channel as
    "source language + text of translated languages". Off by default (§184 no
    silent assumptions): an empty ``bot_token`` or ``chat_id`` keeps the
    notifier inert even when ``enabled`` is true.

    The mirror is a reporting channel, never a second publication path. It is
    fed AFTER the §78 router, §79 validator and §95/§167 safety gate, so it can
    never show text the room suppressed (§0 rule 5); a blocked target travels
    as the public fallback message, never as an operator-facing reason.
    """

    enabled: bool = False
    bot_token: str = ""          # @BotFather token; a secret - env only
    # Bot chat, "@channelusername" or "-100..." channel/supergroup id. All three
    # use the identical sendMessage path, so a bot DM and a channel both work.
    chat_id: str = ""
    send_captions: bool = True   # mirror live caption lines (the ADR-0016 scope)
    # §149 bounded resources: the queue never grows without limit. When full the
    # OLDEST line is dropped - a live mirror must keep the newest line.
    queue_maxsize: int = Field(default=32, ge=1)
    # Minimum spacing between two sends; 0 = every line. Guards Telegram's
    # per-chat rate limits on a fast-speaking imam.
    min_interval_ms: int = Field(default=0, ge=0)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    max_attempts: int = Field(default=3, ge=1, le=10)  # bounded retries (§54 spirit)
    # §0 rule 8: silent by default, so a mirror never rings a channel at 3am.
    disable_notification: bool = True
    include_source: bool = True      # the spoken Arabic line
    include_room_code: bool = True   # header line (one lookup per session)
    # False mirrors the public fallback text too (honest: same text as the room).
    skip_unavailable: bool = False
    api_base_url: str = "https://api.telegram.org"  # overridable for tests
    # Empty = plain text: Arabic carrying <, & or * can never break rendering.
    # "HTML"/"MarkdownV2" are opt-in; the notifier escapes the text to match.
    parse_mode: str = ""


class ValidationSettings(BaseSettings):
    """§79/§66-§68/§101/§23 validation thresholds (Phase 4, §185 no magic values).

    The validator chain flags a translation and classifies each issue; a
    CRITICAL warning always BLOCKs (§167). Risk is bucketed so a
    non-critical-but-risky output becomes DISPLAY_ONLY (§79) rather than
    silently broadcast.
    """

    # Per-check toggles (§79 check list). All on by default in Phase 4.
    check_numbers: bool = True        # §66  CRITICAL on number change
    check_negation: bool = True       # §67  CRITICAL on polarity flip
    check_entities: bool = True       # §68  NUMBER entities enforced
    check_glossary: bool = True       # §22  approved-term presence
    check_additions: bool = True      # §22.3 no invented commentary
    check_scripture: bool = True      # §78  sacred-text reference policy

    # §22.3 additions: flag when target words exceed source * this ratio.
    max_length_ratio: float = Field(default=2.5, gt=1.0)
    # §23 risk buckets. >= high -> DISPLAY_ONLY; medium/low -> WARNING.
    high_risk_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    medium_risk_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    # §86: Hadith may fall back to a general (non-attributed) translation, so
    # a missing hadith reference is a WARNING unless this strict policy is on.
    hadith_requires_reference: bool = False


class AlignmentSettings(BaseSettings):
    """§77 sermon_aligner thresholds (Phase 4, §193).

    Live ASR text is scored against a local window of prepared segments. The
    weighted score mixes lexical overlap, character-level fuzzy similarity,
    temporal position and continuity; semantic similarity is absent until an
    embedding model lands (excluded, never faked - §0 rule 8, §166).
    """

    enabled: bool = True
    window_size: int = Field(default=5, ge=1)          # local window radius
    confirm_threshold: float = Field(default=0.72, ge=0.0, le=1.0)
    candidate_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    # Weights for the composite alignment score (renormalized to sum to 1.0).
    lexical_weight: float = Field(default=0.45, ge=0.0, le=1.0)
    fuzzy_weight: float = Field(default=0.35, ge=0.0, le=1.0)
    temporal_weight: float = Field(default=0.10, ge=0.0, le=1.0)
    continuity_weight: float = Field(default=0.10, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> "AlignmentSettings":
        # §77: a candidate must be strictly below a confirm threshold, else the
        # WAIT band is empty and every candidate is immediately confirmed.
        if self.candidate_threshold >= self.confirm_threshold:
            raise ValueError(
                "candidate_threshold must be below confirm_threshold "
                f"({self.candidate_threshold} >= {self.confirm_threshold})"
            )
        return self


class ScriptureSettings(BaseSettings):
    """§76/§42.3 scripture verification thresholds (Phase 4).

    Exact normalized text is VERIFIED; a strict fuzzy match is VERIFIED_NEAR;
    a weaker match is MANUAL_REVIEW_REQUIRED; nothing is UNKNOWN. Only
    VERIFIED / VERIFIED_NEAR carry an approved reference (§0 rule 8: never
    claim a citation we cannot verify).
    """

    near_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    review_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    # §78/§86: QURAN with no approved reference is BLOCKED; HADITH falls back
    # to a general (non-attributed) translation when no reference is found.
    quran_block_without_reference: bool = True
    hadith_fallback_general: bool = True


class AuthSettings(BaseSettings):
    """Operator authentication settings (§116, ADR-0004)."""

    model_config = SettingsConfigDict(
        env_prefix="BAYAAN_AUTH__",
        extra="ignore",
    )

    username: str = "admin"
    password: str = "admin"
    operator_token_secret: str = "dev-insecure-operator-secret-change-me"
    token_ttl_hours: int = Field(default=24, ge=1)

    @property
    def operator_secret_is_dev_default(self) -> bool:
        return self.operator_token_secret == "dev-insecure-operator-secret-change-me"


class LiveTranscriptSettings(BaseSettings):
    """Semantic discourse segmentation for live khutbah ingest."""

    enabled: bool = True
    min_words_per_segment: int = Field(default=6, ge=1, le=50)
    max_words_per_segment: int = Field(default=22, ge=5, le=100)
    max_chars_per_segment: int = Field(default=140, ge=20, le=500)


class SecuritySettings(BaseSettings):
    """§116 session security: random visitor tokens, expiration, no identity."""

    visitor_token_secret: str = "dev-insecure-secret-change-me"
    visitor_token_ttl_minutes: int = Field(default=720, ge=1)
    # §184: production must configure a real secret - preflight fails loudly.
    @property
    def visitor_secret_is_dev_default(self) -> bool:
        return self.visitor_token_secret == "dev-insecure-secret-change-me"


class RateLimitSettings(BaseSettings):
    """§115: separate limits per category - never one global limit."""

    enabled: bool = True
    discovery_per_minute: int = Field(default=30, ge=1)
    ws_per_minute: int = Field(default=60, ge=1)
    admin_per_minute: int = Field(default=120, ge=1)


class RealtimeSettings(BaseSettings):
    """WebSocket broadcast behaviour (§55, §188)."""

    history_size: int = Field(default=20, ge=0, le=200)


class RoomSettings(BaseSettings):
    code_length: int = Field(default=8, ge=4, le=32)


class LanguageSettings(BaseSettings):
    """Language configuration (§63, ADR-0017).

    ``target_languages`` is chosen from the curated catalog in
    ``domain.languages`` - the admin picks from that list, and anything outside
    it is rejected here at startup with the valid codes spelled out, instead of
    failing later as a mysterious runtime error (§184: no silent assumptions).
    """

    source_language: str = "ar"
    target_languages: list[str] = Field(default_factory=lambda: ["it", "en"])

    @model_validator(mode="after")
    def _targets_come_from_the_catalog(self) -> "LanguageSettings":
        from bayaan_server.domain.languages import (
            LANGUAGE_CODES,
            SOURCE_LANGUAGE,
            TARGET_LANGUAGE_CODES,
            target_codes_csv,
        )

        # De-duplicate while preserving the configured order: the order is the
        # order worshippers see on the room page and in the Telegram mirror.
        deduped = list(dict.fromkeys(self.target_languages))
        unknown = [code for code in deduped if code not in TARGET_LANGUAGE_CODES]
        if unknown:
            raise ValueError(
                f"unsupported target language(s): {', '.join(unknown)}. "
                f"Supported targets: {target_codes_csv()}"
            )
        if SOURCE_LANGUAGE in deduped:
            raise ValueError(
                f"'{SOURCE_LANGUAGE}' is the source language and cannot be a target "
                f"(§63). Supported targets: {target_codes_csv()}"
            )
        if self.source_language not in LANGUAGE_CODES:
            raise ValueError(
                f"unsupported source language '{self.source_language}'. "
                f"Supported languages: {', '.join(sorted(LANGUAGE_CODES))}"
            )
        if not deduped:
            raise ValueError(
                "target_languages must not be empty - at least one target is "
                f"required (§63). Supported targets: {target_codes_csv()}"
            )
        self.target_languages = deduped
        return self


class AudioSettings(BaseSettings):
    """Audio capture configuration (§184: configured device, verified at
    preflight - never "assume microphone is device 0").

    PC audio input (ADR-0014). The master spec constrains input to the digital
    mixer, but real deployments often run the server on a PC that *is* the
    audio source. Two explicit, config-gated paths feed the SAME
    ``LiveIngestService`` pipeline (never a second, weaker path - §0 rule 5):

      - ``server_device``: the server captures the configured input device via
        ``DeviceAudioSource`` (``sounddevice``) and streams frames into the
        pipeline. Requires the ``audio`` extra and runs the capture on the PC
        hosting the server.
      - ``browser``: the operator's browser captures the PC microphone with
        ``getUserMedia`` and streams 16 kHz mono int16 PCM frames over the
        operator ingest WebSocket. Works even when the server is remote.

    ``capture_enabled`` is the master switch: with it OFF (the default) neither
    path activates and behaviour is unchanged from the mixer/WAV-upload spec.
    """

    input_device: str = "default"
    sample_rate: int = Field(default=16000, ge=8000, le=96000)
    channels: int = Field(default=1, ge=1, le=2)

    # ADR-0014: PC audio input. Master switch, default OFF (spec is mixer-only).
    capture_enabled: bool = False
    # Which PC input path is active when capture is enabled (§184: explicit).
    capture_mode: Literal["server_device", "browser"] = "server_device"
    # Allow the operator browser to open the ingest WebSocket when the server
    # is configured for ``browser`` mode. Kept separate so a deployment can lock
    # the ingest channel independently of the server-side capture path.
    ingest_ws_enabled: bool = True


class VadSettings(BaseSettings):
    """Energy VAD tuning. All values explicit and configurable (§185)."""

    rms_speech_threshold: float = Field(default=500.0, gt=0.0)
    silence_ms_to_close: int = Field(default=700, ge=100)
    min_utterance_ms: int = Field(default=400, ge=100)
    max_utterance_ms: int = Field(default=30_000, ge=1000)
    pre_speech_pad_ms: int = Field(default=120, ge=0)


class SermonSettings(BaseSettings):
    """Sermon preparation limits (§185: no magic values; §160 sizing rules).

    Segments must be small enough for latency yet big enough to keep
    meaning: one-or-two-word segments are merged away, two-page
    segments are split.
    """

    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    allowed_extensions: list[str] = Field(
        default_factory=lambda: ["txt", "md", "docx", "pdf"]
    )
    min_segment_chars: int = Field(default=30, ge=5)
    max_segment_chars: int = Field(default=500, ge=100)
    storage_root: str = "storage"


class DatabaseSettings(BaseSettings):
    """§117: PostgreSQL is the production store.

    SQLite is allowed only for local dev/test because Docker/Postgres is not
    available in this environment (documented deviation, docs/adr/0001).
    """

    url: str = "sqlite+aiosqlite:///./bayaan_dev.db"
    echo_sql: bool = False


class ProviderSettings(BaseSettings):
    """Provider selection (§63: provider-neutral; §188: fallback chain).

    Each capability has a primary and an optional fallback name; the failover
    runner (§87) serves the primary and moves to the fallback when its circuit
    opens (§54). ``''`` / ``disabled`` select no provider. Only mock providers
    exist; mock confidences are configurable so the full safety path can be
    exercised in dev/test. Real adapters are added per later phases.

    ``mock-failing`` is the §195 failure-injection provider: it always raises a
    simulated outage, so failover, degraded mode and the acceptance ("simulated
    provider failure does not break original service") are exercisable
    end-to-end without touching any adapter.
    """

    asr: Literal["mock", "mock-failing", "whisper"] = "mock"
    translation: Literal[
        "mock", "mock-failing", "libre", "ollama", "ollama_cloud"
    ] = "mock"
    tts: Literal["mock", "disabled", "piper"] = "mock"
    asr_fallback: Literal["", "mock", "mock-failing", "whisper"] = ""
    translation_fallback: Literal[
        "", "mock", "mock-failing", "libre", "ollama", "ollama_cloud"
    ] = ""
    tts_fallback: Literal["", "mock", "piper"] = ""
    mock_asr_confidence: float = Field(default=0.90, ge=0.0, le=1.0)
    mock_translation_confidence: float = Field(default=0.90, ge=0.0, le=1.0)
    # §195 test/ops hook: the always-failing mock variant.
    mock_asr_fail_always: bool = False
    mock_translation_fail_always: bool = False
    # Test/dev hook: when non-empty the mock ASR returns this fixed text
    # instead of the unavailable marker, so the prepared-sermon retrieval
    # path (§78) can be exercised end-to-end without a real ASR backend.
    # Configuration, not business logic (§0 rule 4); empty in production.
    mock_asr_text: str = ""
    # Whisper ASR adapter (OpenAI / Groq / local OpenAI-compatible transcription endpoint)
    whisper_endpoint: str = "https://api.openai.com/v1/audio/transcriptions"
    whisper_api_key: str = ""
    whisper_model: str = "whisper-1"
    whisper_timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)

    # §63/§186: LibreTranslate adapter connection (real MT). Self-hostable so
    # it runs offline inside a mosque with no AI-vendor lock-in; swap the
    # endpoint for any LibreTranslate-compatible server. See docs/DATABASE.md
    # and docker-compose.yml. Thresholds/tuning are config, not magic (§185).
    libre_endpoint: str = "http://localhost:5000"
    libre_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)
    libre_api_key: str = ""  # optional, for a key-protected instance
    libre_prompt_version: str = "libre-v1"
    # §63/§186: local-LLM (Ollama) adapter - the higher-quality free/offline MT
    # option for the general (non-sacred) path. Self-hostable and offline (no
    # key, no vendor lock-in, khutbah text never leaves the mosque, §58). Any
    # Ollama-compatible server works; pick an Arabic-strong model (aya:8b /
    # qwen2.5:7b). Quran is still retrieved from the SQL store (§206), never MT.
    # Thresholds/tuning are config, not magic (§185).
    ollama_endpoint: str = "http://localhost:11434"
    ollama_model: str = "aya:8b"
    ollama_timeout_seconds: float = Field(default=60.0, gt=0.0, le=300.0)
    ollama_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    ollama_prompt_version: str = "ollama-v1"
    # §204 tuning (measured on the pilot box, see tools/bench_ollama_configs.py).
    # keep_alive=-1 pins the model in RAM permanently. This is the single
    # biggest latency win available: Ollama's default unloads a model after
    # ~5 minutes, so the first utterance after any idle gap pays a full reload
    # (~15s for aya:8b) - far worse than the ~4s of inference. Pinning it turns
    # every subsequent call into a ~4s call. Costs one model's RAM while the
    # server runs, which is the right trade for a machine whose whole job is
    # translating one khutbah.
    ollama_keep_alive: str = "-1"
    # Shorter context than Ollama's 8192 default: a khutbah segment is a few
    # sentences, and the KV cache it saves is real RAM on a 16GB laptop.
    ollama_num_ctx: int = Field(default=2048, ge=256, le=32768)
    # 0 = let Ollama pick (it auto-detects physical cores).
    ollama_num_thread: int = Field(default=0, ge=0, le=256)
    ollama_num_batch: int = Field(default=512, ge=1, le=4096)
    # §63/§186 + ADR-0011: Ollama *Cloud* adapter - frontier-quality MT for the
    # general (non-sacred) path when a mosque opts in. Unlike the local adapter
    # this is key-authenticated and khutbah text leaves the box to Ollama's
    # cloud ("processed, not used for training"), which is a documented §58
    # deviation the operator chooses (ADR-0011); it is never the silent default.
    # Model selection is config, not code: the API expects the exact tag name
    # (e.g. "gemma4:31b"); adding another model/vendor later is a value here +
    # a factory entry, with no change to the TranslationProvider port.
    ollama_cloud_endpoint: str = "https://ollama.com"
    ollama_cloud_model: str = "gemma4:31b"
    # Set via env BAYAAN_PROVIDERS__OLLAMA_CLOUD_API_KEY (never committed, §64).
    ollama_cloud_api_key: str = ""
    ollama_cloud_timeout_seconds: float = Field(default=45.0, gt=0.0, le=300.0)
    ollama_cloud_prompt_version: str = "ollama-cloud-v1"
    # Gemma 4 best-practice sampling (docs.ollama.com). Higher temperature than
    # the local MT default is intentional for this frontier model; all tunable.
    ollama_cloud_temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    ollama_cloud_top_p: float = Field(default=0.95, ge=0.0, le=1.0)
    ollama_cloud_top_k: int = Field(default=64, ge=0, le=256)
    # §71: thinking off so the model returns the translation alone (no reasoning
    # preamble). Flip to true only for diagnostics — it violates translate-only.
    ollama_cloud_thinking: bool = False

    # §194/§63 + ADR-0013: Piper / Sherpa-ONNX local TTS adapter. Runs open
    # Piper-format voices fully offline on CPU (no GPU, no vendor, no internet)
    # — the right default for varied / low-end mosque hardware. The binary is a
    # `piper` (or sherpa-onnx CLI) that accepts `--model <voice.onnx>
    # --output_raw`; voices live in `piper_voice_dir` as `<voice>.onnx` (+ the
    # optional `<voice>.onnx.json` declaring the sample rate). One voice per
    # target language; add a new language here + in PiperTTSProvider.
    piper_exec: str = "piper"
    piper_voice_dir: str = ""
    piper_voice_ar: str = "ar_JO-kareem-medium"
    piper_voice_it: str = "it_IT-paola-medium"
    piper_voice_en: str = "en_US-lessac-medium"


class CameraSettings(BaseSettings):
    """PC camera relay tuning (ADR-0015; §185 no magic values).

    The operator's browser captures its webcam and relays JPEG frames over an
    operator-authenticated WebSocket; the server keeps only the latest frame
    per session and distributes it as an MJPEG stream to the projector and
    (optionally) worshipper phones. Everything is explicit configuration - the
    frontend negotiates toward these values via getUserMedia constraints and
    the canvas encode settings (§184).
    """

    # Master switch. When off, the camera WebSocket closes 4432 and the MJPEG
    # route returns 404 (the "Start PC Camera" button is hidden by the API).
    capture_enabled: bool = True
    # Target capture frame. The browser asks for exactly these values; the
    # canvas is created at this size so JPEG output never exceeds it.
    width: int = Field(default=640, ge=160, le=1920)
    height: int = Field(default=360, ge=120, le=1080)
    frames_per_second: int = Field(default=10, ge=1, le=30)
    jpeg_quality: float = Field(default=0.6, ge=0.1, le=1.0)
    max_frame_bytes: int = Field(default=512 * 1024, ge=8 * 1024)
    # Concurrent MJPEG viewers per session (§149: bounded fan-out).
    max_viewers_per_session: int = Field(default=10, ge=1)
    # Consecutive invalid (non-JPEG) frames before the WS is closed 4431.
    max_invalid_frames: int = Field(default=3, ge=1)
    # Defensive fallback wake timeout for MJPEG viewers (§185 explicit, no hot
    # loop). Frames wake viewers via an event the moment they land; this only
    # bounds recovery if a wake were ever missed.
    viewer_poll_interval_seconds: float = Field(default=0.25, gt=0.0, le=2.0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BAYAAN_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "Bayaan Server"
    environment: Literal["development", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    languages: LanguageSettings = Field(default_factory=LanguageSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    camera: CameraSettings = Field(default_factory=CameraSettings)
    vad: VadSettings = Field(default_factory=VadSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    tts: TtsSettings = Field(default_factory=TtsSettings)
    failover: FailoverSettings = Field(default_factory=FailoverSettings)
    alerts: AlertsSettings = Field(default_factory=AlertsSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    alignment: AlignmentSettings = Field(default_factory=AlignmentSettings)
    scripture: ScriptureSettings = Field(default_factory=ScriptureSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    live_transcript: LiveTranscriptSettings = Field(default_factory=LiveTranscriptSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    realtime: RealtimeSettings = Field(default_factory=RealtimeSettings)
    room: RoomSettings = Field(default_factory=RoomSettings)
    sermon: SermonSettings = Field(default_factory=SermonSettings)

    # Session limits (§149 concurrency rules: bounded resources, never infinite)
    max_active_sessions: int = Field(default=4, ge=1)

    # §197/§191: the public base URL worshippers actually reach the app at. When
    # set, the QR code and the operator "join" link are built from THIS value
    # instead of the incoming request's Host header. That is what makes the app
    # reachable from phones on the same Wi-Fi: behind the dev proxy the Host is
    # always 127.0.0.1, so a phone scanning the QR would otherwise be sent to an
    # address it cannot resolve. Set e.g. BAYAAN_PUBLIC_BASE_URL=http://192.168.1.20:5173
    # (empty = fall back to the request base URL, the previous behaviour).
    public_base_url: str = ""


# The backend .env lives next to this module's package root (backend/.env).
# Loaded ONLY here, in get_settings(), never in a bare Settings(...) - so the
# test suite (which builds Settings directly) stays hermetic and cannot be
# perturbed by a developer's local .env (validated: direct Settings() ignores it).
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


@lru_cache
def get_settings() -> Settings:
    if _ENV_FILE.is_file():
        # override=False: a real environment variable (BAYAAN_PROVIDERS__*, set
        # by the shell or CI) always wins over the .env file (standard dotenv).
        load_dotenv(_ENV_FILE, override=False)
    return Settings()