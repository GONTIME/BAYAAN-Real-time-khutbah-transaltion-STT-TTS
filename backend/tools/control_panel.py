"""Bayaan Control Panel — edit backend/.env in a browser and run the servers.

Same UI style as the Bayaan frontend (dark dashboard tokens from
frontend/src/styles.css) but built with Python's standard library only so it
works before any packaging step:

    python backend/tools/control_panel.py

Then open http://127.0.0.1:8765/ . The panel:

  * lists every setting from ``bayaan_server.config`` with a description,
  * saves changes back into ``backend/.env`` preserving comments/order,
  * starts/stops the backend (uvicorn) and frontend (Vite dev server),
  * checks real readiness (``/health`` and the frontend port) and surfaces
    startup failures for both in the Logs tab,
  * is bound to 127.0.0.1 (localhost) only and never exposes secrets in the
    HTTP API (secret fields are shown as "set"/"empty", never their value).

Allowed LAN reachability of Bayaan itself is an opt-in toggle (default OFF);
the panel always stays on 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Fixed paths (this file lives in backend/tools/)
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[1]  # <repo>/backend
PROJECT_ROOT = BACKEND_DIR.parent                   # <repo>
FRONTEND_DIR = PROJECT_ROOT / "frontend"            # <repo>/frontend
ENV_FILE = BACKEND_DIR / ".env"
PANEL_STATE_FILE = BACKEND_DIR / ".control_panel_state.json"

BACKEND_PORT = 8000
FRONTEND_PORT = 5173
PANEL_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
HEALTH_URL = f"http://{DEFAULT_HOST}:{BACKEND_PORT}/health"

BACKEND_READY_TIMEOUT_S = 60
FRONTEND_READY_TIMEOUT_S = 120
MAX_LOG_LINES = 800

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# Runtime detection
# ---------------------------------------------------------------------------


def resolve_python() -> str | None:
    """Pick a Python that has the backend deps (project venvs first, else PATH)."""
    candidates = [
        BACKEND_DIR / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return shutil.which("python") or shutil.which("python3")


def detect_backend_deps(python: str | None) -> str | None:
    """Return None when the chosen Python can run the backend, else a message."""
    if not python:
        return (
            "Python was not found. Install Python 3.11+ from "
            "https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), "
            "then run this file again."
        )
    probe = "import uvicorn, fastapi, pydantic_settings, bayaan_server; print('ok')"
    try:
        result = subprocess.run(
            [python, "-c", probe],
            capture_output=True,
            timeout=30,
            cwd=str(BACKEND_DIR),
            text=True,
        )
        if result.returncode == 0:
            return None
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else "unknown error"
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
    return (
        "The backend Python could not import its dependencies.\n\n"
        f"Python: {python}\nProblem: {detail}\n\n"
        "Fix: open a terminal in the backend folder and run:\n"
        "  python -m venv .venv\n"
        "  .\\.venv\\Scripts\\python -m pip install -e \".[all]\"\n"
    )


def resolve_node() -> str | None:
    return shutil.which("node")
# ---------------------------------------------------------------------------
# Option enumeration — introspect the pydantic Settings model so the list of
# settings can never drift out of sync with config.py.
# ---------------------------------------------------------------------------


def _iter_option_fields():
    """Yield leaf option metadata, mirroring how pydantic-settings builds env
    var names: ``BAYAAN_<GROUP>__<FIELD>``."""
    from pydantic_settings import BaseSettings

    from bayaan_server.config import Settings

    def walk(model, prefix, path):
        for name, field in model.model_fields.items():
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseSettings):
                child_prefix = getattr(annotation.model_config, "env_prefix", None)
                if not child_prefix:
                    child_prefix = prefix + name.upper() + "__"
                yield from walk(annotation, child_prefix, path + [name])
                continue
            env_key = prefix + name.upper()
            yield {"env_key": env_key, "name": name, "parent": path, "field": field}

    root_prefix = getattr(Settings.model_config, "env_prefix", None) or "BAYAAN_"
    yield from walk(Settings, root_prefix, [])


def group_for_path(path) -> str:
    if not path:
        return "Core"
    return path[0].replace("_", " ").title()


def _option_type(field):
    """Return ``(kind, choices)`` where kind is bool|int|float|str|select|list."""
    from typing import Literal, get_args, get_origin

    annotation = field.annotation
    origin = get_origin(annotation)
    if origin is Literal:
        return "select", list(get_args(annotation))
    if origin is list:
        return "list", None
    if annotation is bool:
        return "bool", None
    if annotation is int:
        return "int", None
    if annotation is float:
        return "float", None
    return "str", None


def _option_default(field):
    """Serialized default value for a field (pydantic default or factory)."""
    from pydantic_core import PydanticUndefined

    raw = field.default
    if raw is PydanticUndefined and field.default_factory is not None:
        try:
            raw = field.default_factory()
        except Exception:  # noqa: BLE001 - best-effort default display
            raw = None
    return raw


def is_secret_key(env_key: str) -> bool:
    upper = env_key.upper()
    if "API_KEY" in upper:
        return True
    if upper.endswith("_PASSWORD") or upper.endswith("_PASS"):
        return True
    if "TOKEN_SECRET" in upper:
        return True
    # A bare *_TOKEN is a credential too (e.g. the Telegram bot token, ADR-0016);
    # it must be shown as "set/empty" in the panel, never echoed. TTL-style keys
    # end in _TTL_HOURS / _TTL_MINUTES and stay visible.
    if upper.endswith("_TOKEN"):
        return True
    return False


def serialize_value(kind: str, raw) -> str:
    """Turn a python default into .env storage text."""
    if kind == "bool":
        return "true" if bool(raw) else "false"
    if kind == "list":
        try:
            return json.dumps(list(raw), ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return ""
    if raw is None:
        return ""
    return str(raw)


TARGET_LANGUAGES_KEY = "BAYAAN_LANGUAGES__TARGET_LANGUAGES"


def language_picker_choices() -> list[dict[str, str]]:
    """Curated target languages for the picker (ADR-0017).

    Imported lazily so this module keeps working without the backend package on
    the path (the panel is stdlib-only by design).
    """
    from bayaan_server.domain.languages import LANGUAGE_CATALOG

    return [
        {"code": entry.code, "label": entry.endonym, "english": entry.english}
        for entry in LANGUAGE_CATALOG
        if entry.code != "ar"  # the source language is never a target (§63)
    ]


def build_options() -> list[dict]:
    """Enumerate every config option with default + description + type."""
    options = []
    for meta in _iter_option_fields():
        field = meta["field"]
        kind, choices = _option_type(field)
        env_key = meta["env_key"]
        default = _option_default(field)
        description = OPTION_DESCRIPTIONS.get(env_key, "")
        if env_key == TARGET_LANGUAGES_KEY:
            # ADR-0017: the admin picks from the curated catalog, so the panel
            # renders a checkbox list (never free-typed codes) and the
            # description spells out what is supported.
            choices = language_picker_choices()
            description = (
                "Languages to translate into. Tick them below - the list is the "
                "curated catalog the server supports: "
                + ", ".join(c["code"] for c in choices)
                + ". A room can be narrowed further when starting it."
            )
        options.append(
            {
                "key": env_key,
                "group": group_for_path(meta["parent"]),
                "name": meta["name"],
                "kind": kind,
                "choices": choices,
                "secret": is_secret_key(env_key),
                "default": serialize_value(kind, default),
                "description": description,
            }
        )
    return options
# ---------------------------------------------------------------------------
# Human-readable descriptions for every setting, keyed by env var name.
# ---------------------------------------------------------------------------

OPTION_DESCRIPTIONS: dict[str, str] = {
    # Core
    "BAYAAN_APP_NAME": "Display name for the server (shown in page titles/logs).",
    "BAYAAN_ENVIRONMENT": "development | staging | production. Set to production for a real event.",
    "BAYAAN_LOG_LEVEL": "How chatty the server logs are. DEBUG is noisiest; ERROR only reports problems.",
    # Languages
    "BAYAAN_LANGUAGES__SOURCE_LANGUAGE": "Language code spoken from the minbar (the source), e.g. ar.",
    "BAYAAN_LANGUAGES__TARGET_LANGUAGES": 'JSON list of languages to translate into, e.g. ["it","en"].',
    # Audio capture
    "BAYAAN_AUDIO__INPUT_DEVICE": "Which PC audio device to capture (default = system default).",
    "BAYAAN_AUDIO__SAMPLE_RATE": "Audio sample rate in Hz (16000 is the speech standard).",
    "BAYAAN_AUDIO__CHANNELS": "Number of audio channels (1 = mono).",
    "BAYAAN_AUDIO__CAPTURE_ENABLED": "Master switch for PC audio input. WAV uploads always work; this enables live mic/system capture.",
    "BAYAAN_AUDIO__CAPTURE_MODE": "server_device = this PC captures its input; browser = the operator's browser streams the mic.",
    "BAYAAN_AUDIO__INGEST_WS_ENABLED": "Allow the operator browser to open the audio ingest WebSocket.",
    # Camera
    "BAYAAN_CAMERA__CAPTURE_ENABLED": "Master switch for the PC camera relay (hides the Start PC Camera button when off).",
    "BAYAAN_CAMERA__WIDTH": "Target webcam capture width in pixels.",
    "BAYAAN_CAMERA__HEIGHT": "Target webcam capture height in pixels.",
    "BAYAAN_CAMERA__FRAMES_PER_SECOND": "Webcam frames captured per second (10 is a good balance).",
    "BAYAAN_CAMERA__JPEG_QUALITY": "JPEG compression quality 0.1-1.0 (higher = bigger frames).",
    "BAYAAN_CAMERA__MAX_FRAME_BYTES": "Reject camera frames larger than this many bytes.",
    "BAYAAN_CAMERA__MAX_VIEWERS_PER_SESSION": "Max concurrent viewers of one camera stream (bounded fan-out).",
    "BAYAAN_CAMERA__MAX_INVALID_FRAMES": "Non-JPEG frames received before the camera WebSocket is closed.",
    "BAYAAN_CAMERA__VIEWER_POLL_INTERVAL_SECONDS": "How often MJPEG viewers wake up if a frame wake is missed.",
    # VAD
    "BAYAAN_VAD__RMS_SPEECH_THRESHOLD": "RMS audio level that counts as speech (lower = more sensitive).",
    "BAYAAN_VAD__SILENCE_MS_TO_CLOSE": "Silence this long closes the current utterance.",
    "BAYAAN_VAD__MIN_UTTERANCE_MS": "Ignore sound blips shorter than this.",
    "BAYAAN_VAD__MAX_UTTERANCE_MS": "Hard cap on one utterance length.",
    "BAYAAN_VAD__PRE_SPEECH_PAD_MS": "Audio kept before speech starts so the first word is not cut off.",
    # Database
    "BAYAAN_DATABASE__URL": "Database connection string. SQLite for a simple local file; Postgres for production.",
    "BAYAAN_DATABASE__ECHO_SQL": "Log every SQL statement (diagnostics only).",
    # Providers
    "BAYAAN_PROVIDERS__ASR": "Speech-to-text engine: mock | mock-failing | whisper.",
    "BAYAAN_PROVIDERS__TRANSLATION": "Translation engine: mock | mock-failing | libre | ollama | ollama_cloud.",
    "BAYAAN_PROVIDERS__TTS": "Text-to-speech engine: mock | disabled | piper (offline voices).",
    "BAYAAN_PROVIDERS__ASR_FALLBACK": "Second-choice ASR engine used if the primary fails (empty = none).",
    "BAYAAN_PROVIDERS__TRANSLATION_FALLBACK": "Second-choice translation engine if the primary fails (empty = none).",
    "BAYAAN_PROVIDERS__TTS_FALLBACK": "Second-choice TTS engine if the primary fails (empty = none).",
    "BAYAAN_PROVIDERS__MOCK_ASR_CONFIDENCE": "Confidence score the mock ASR reports (0.0-1.0).",
    "BAYAAN_PROVIDERS__MOCK_TRANSLATION_CONFIDENCE": "Confidence score the mock translator reports (0.0-1.0).",
    "BAYAAN_PROVIDERS__MOCK_ASR_FAIL_ALWAYS": "Make the mock ASR always fail (for testing failover).",
    "BAYAAN_PROVIDERS__MOCK_TRANSLATION_FAIL_ALWAYS": "Make the mock translator always fail (for testing failover).",
    "BAYAAN_PROVIDERS__MOCK_ASR_TEXT": "Fixed Arabic text the mock ASR returns (test/dev only).",
    "BAYAAN_PROVIDERS__WHISPER_ENDPOINT": "URL of the Whisper-compatible transcription API.",
    "BAYAAN_PROVIDERS__WHISPER_API_KEY": "Secret API key for the Whisper provider.",
    "BAYAAN_PROVIDERS__WHISPER_MODEL": "Whisper model name (e.g. whisper-1).",
    "BAYAAN_PROVIDERS__WHISPER_TIMEOUT_SECONDS": "Seconds to wait for a Whisper response before failing over.",
    "BAYAAN_PROVIDERS__LIBRE_ENDPOINT": "URL of your LibreTranslate server.",
    "BAYAAN_PROVIDERS__LIBRE_TIMEOUT_SECONDS": "Seconds to wait for LibreTranslate before failing over.",
    "BAYAAN_PROVIDERS__LIBRE_API_KEY": "Optional API key if your LibreTranslate instance requires one.",
    "BAYAAN_PROVIDERS__LIBRE_PROMPT_VERSION": "Prompt style tag sent to LibreTranslate (version marker).",
    "BAYAAN_PROVIDERS__OLLAMA_ENDPOINT": "URL of your local Ollama server.",
    "BAYAAN_PROVIDERS__OLLAMA_MODEL": "Ollama model used for translation (Arabic-strong e.g. aya:8b).",
    "BAYAAN_PROVIDERS__OLLAMA_TIMEOUT_SECONDS": "Seconds to wait for Ollama before failing over.",
    "BAYAAN_PROVIDERS__OLLAMA_TEMPERATURE": "Creativity 0.0-2.0 (lower = more precise).",
    "BAYAAN_PROVIDERS__OLLAMA_PROMPT_VERSION": "Prompt style tag for the local Ollama adapter.",
    "BAYAAN_PROVIDERS__OLLAMA_KEEP_ALIVE": "-1 pins the model in RAM for fast replies; otherwise seconds after use.",
    "BAYAAN_PROVIDERS__OLLAMA_NUM_CTX": "Context window in tokens (2048 is plenty for a khutbah segment).",
    "BAYAAN_PROVIDERS__OLLAMA_NUM_THREAD": "CPU threads for Ollama (0 = auto-detect).",
    "BAYAAN_PROVIDERS__OLLAMA_NUM_BATCH": "Token batch size for Ollama generation.",
    "BAYAAN_PROVIDERS__OLLAMA_CLOUD_ENDPOINT": "Ollama Cloud / hosted-compatible endpoint URL.",
    "BAYAAN_PROVIDERS__OLLAMA_CLOUD_MODEL": "Cloud model tag (e.g. gemma4:31b).",
    "BAYAAN_PROVIDERS__OLLAMA_CLOUD_API_KEY": "Secret key for Ollama Cloud. Sends khutbah text off-box (opt-in only).",
    "BAYAAN_PROVIDERS__OLLAMA_CLOUD_TIMEOUT_SECONDS": "Seconds to wait for Ollama Cloud before failing over.",
    "BAYAAN_PROVIDERS__OLLAMA_CLOUD_PROMPT_VERSION": "Prompt style tag for the cloud adapter.",
    "BAYAAN_PROVIDERS__OLLAMA_CLOUD_TEMPERATURE": "Cloud sampling temperature 0.0-2.0.",
    "BAYAAN_PROVIDERS__OLLAMA_CLOUD_TOP_P": "Cloud nucleus sampling 0.0-1.0.",
    "BAYAAN_PROVIDERS__OLLAMA_CLOUD_TOP_K": "Cloud top-k sampling (64 is typical).",
    "BAYAAN_PROVIDERS__OLLAMA_CLOUD_THINKING": "Keep true to translate only (disables chain-of-thought).",
    "BAYAAN_PROVIDERS__PIPER_EXEC": "Path to the Piper/sherpa CLI executable.",
    "BAYAAN_PROVIDERS__PIPER_VOICE_DIR": "Folder containing the Piper voice (.onnx+json) files.",
    "BAYAAN_PROVIDERS__PIPER_VOICE_AR": "Voice name used for Arabic, e.g. ar_JO-kareem-medium.",
    "BAYAAN_PROVIDERS__PIPER_VOICE_IT": "Voice name used for Italian, e.g. it_IT-paola-medium.",
    "BAYAAN_PROVIDERS__PIPER_VOICE_EN": "Voice name used for English, e.g. en_US-lessac-medium.",
    # Safety
    "BAYAAN_SAFETY__ASR_MIN_CONFIDENCE": "Lowest ASR confidence allowed (below = blocked, not broadcast).",
    "BAYAAN_SAFETY__ALIGNMENT_MIN_CONFIDENCE": "Lowest alignment confidence allowed.",
    "BAYAAN_SAFETY__TRANSLATION_MIN_CONFIDENCE": "Lowest translation confidence allowed.",
    "BAYAAN_SAFETY__TTS_MIN_CONFIDENCE": "Lowest confidence a line needs before it may be spoken aloud.",
    "BAYAAN_SAFETY__OVERALL_MIN_CONFIDENCE": "Composite minimum; below this the output becomes a safe fallback, never broadcast.",
    "BAYAAN_SAFETY__WEIGHTS__ASR": "Weight of the ASR signal in the overall confidence (all four weights sum to 1.0).",
    "BAYAAN_SAFETY__WEIGHTS__ALIGNMENT": "Weight of alignment in the overall confidence.",
    "BAYAAN_SAFETY__WEIGHTS__TRANSLATION": "Weight of translation in the overall confidence.",
    "BAYAAN_SAFETY__WEIGHTS__VALIDATION": "Weight of validation in the overall confidence.",
    "BAYAAN_SAFETY__ALIGNMENT_ENABLED": "Enable the alignment engine signal (Phase 4).",
    "BAYAAN_SAFETY__VALIDATION_ENABLED": "Enable the validator-chain signal (Phase 4).",
    "BAYAAN_SAFETY__TTS_ENABLED": "MASTER switch for spoken audio. Off = captions only, nothing spoken aloud.",
    "BAYAAN_SAFETY__FALLBACK_SHOW_ARABIC": "Show Arabic alongside the safe-fallback text when confidence is low.",
    # Validation
    "BAYAAN_VALIDATION__CHECK_NUMBERS": "Flag translations that change numbers (CRITICAL).",
    "BAYAAN_VALIDATION__CHECK_NEGATION": "Flag translations that flip yes/no meaning (CRITICAL).",
    "BAYAAN_VALIDATION__CHECK_ENTITIES": "Enforce named-entity consistency (names, places).",
    "BAYAAN_VALIDATION__CHECK_GLOSSARY": "Require approved terms from the glossary.",
    "BAYAAN_VALIDATION__CHECK_ADDITIONS": "Flag invented commentary not present in the source.",
    "BAYAAN_VALIDATION__CHECK_SCRIPTURE": "Apply the sacred-text reference policy.",
    "BAYAAN_VALIDATION__MAX_LENGTH_RATIO": "Flag output longer than source * this ratio.",
    "BAYAAN_VALIDATION__HIGH_RISK_THRESHOLD": "Risk at/above this value becomes display-only, never broadcast.",
    "BAYAAN_VALIDATION__MEDIUM_RISK_THRESHOLD": "Risk at/above this value is a warning.",
    "BAYAAN_VALIDATION__HADITH_REQUIRES_REFERENCE": "True = a hadith without a reference is blocked; false = it falls back to a general translation.",
    # TTS tuning
    "BAYAAN_TTS__MAX_STALENESS_MS": "Drop synthesized audio older than this; speaking an old line is worse than silence.",
    "BAYAAN_TTS__QUEUE_MAXSIZE": "Max queued TTS jobs (bounded, never infinite).",
    "BAYAAN_TTS__SAMPLE_RATE": "Audio sample rate used for TTS output.",
    "BAYAAN_TTS__MAX_AUDIO_SECONDS": "Cap one spoken segment's length in seconds.",
    "BAYAAN_TTS__REQUIRE_TRANSLATION_CONFIDENCE": "IMPORTANT: real MT providers report no confidence score, so True (default) silences TTS for them. "
    "Set False to allow speaking translations from score-less providers (ollama/ollama_cloud). Other safety rules still apply.",
    # Failover
    "BAYAAN_FAILOVER__REQUEST_TIMEOUT_MS": "Millis budget for a primary provider call.",
    "BAYAAN_FAILOVER__FALLBACK_TIMEOUT_MS": "Millis budget for a fallback provider call.",
    "BAYAAN_FAILOVER__TRANSLATION_REQUEST_TIMEOUT_MS": "Millis budget for LLM translation (needs more time than ASR/TTS).",
    "BAYAAN_FAILOVER__TRANSLATION_FALLBACK_TIMEOUT_MS": "Millis budget for the translation fallback chain.",
    "BAYAAN_FAILOVER__FAILURE_THRESHOLD": "Consecutive failures before a provider's circuit opens.",
    "BAYAAN_FAILOVER__COOLDOWN_MS": "Millis a provider stays 'open' before being retried.",
    "BAYAAN_FAILOVER__HEARTBEAT_TIMEOUT_MS": "Millis before a capability is considered stale.",
    "BAYAAN_FAILOVER__WATCHDOG_INTERVAL_MS": "Millis between watchdog health polls.",
    # Alerts
    "BAYAAN_ALERTS__ENABLED": "Master switch for operator alerts.",
    "BAYAAN_ALERTS__MIN_SEVERITY": "Lowest severity that triggers an alert: info | warning | error.",
    "BAYAAN_ALERTS__WEBHOOK_URL": "URL to receive alert webhooks (empty = log-only).",
    # Telegram caption mirror (ADR-0016)
    "BAYAAN_TELEGRAM__ENABLED": "Mirror every caption line (source + translations) to a Telegram bot/channel.",
    "BAYAAN_TELEGRAM__BOT_TOKEN": "Secret bot token from @BotFather. Never displayed or logged.",
    "BAYAAN_TELEGRAM__CHAT_ID": "Target chat: @channelname, -100... channel id, or a numeric chat id.",
    "BAYAAN_TELEGRAM__SEND_CAPTIONS": "Mirror live caption lines (the mirror's only scope).",
    "BAYAAN_TELEGRAM__QUEUE_MAXSIZE": "Bounded queue size; overflow drops the OLDEST line.",
    "BAYAAN_TELEGRAM__MIN_INTERVAL_MS": "Minimum millis between two sends (0 = every line).",
    "BAYAAN_TELEGRAM__TIMEOUT_SECONDS": "Per-request timeout for one Telegram send.",
    "BAYAAN_TELEGRAM__MAX_ATTEMPTS": "Bounded send attempts before a line is reported as failed.",
    "BAYAAN_TELEGRAM__DISABLE_NOTIFICATION": "Post silently (no notification ping in the channel).",
    "BAYAAN_TELEGRAM__INCLUDE_SOURCE": "Include the spoken source-language line in each message.",
    "BAYAAN_TELEGRAM__INCLUDE_ROOM_CODE": "Add a header line with the room code and line number.",
    "BAYAAN_TELEGRAM__SKIP_UNAVAILABLE": "Hide languages whose translation was blocked/unavailable.",
    "BAYAAN_TELEGRAM__API_BASE_URL": "Telegram API base URL (change only for a local mock).",
    "BAYAAN_TELEGRAM__PARSE_MODE": "Empty = plain text (safest); HTML or MarkdownV2 escape the text.",
    # Alignment
    "BAYAAN_ALIGNMENT__ENABLED": "Enable the alignment engine (Phase 4).",
    "BAYAAN_ALIGNMENT__WINDOW_SIZE": "Local window radius for alignment candidates.",
    "BAYAAN_ALIGNMENT__CONFIRM_THRESHOLD": "Alignment score that auto-confirms a match (0-1).",
    "BAYAAN_ALIGNMENT__CANDIDATE_THRESHOLD": "Alignment score to become a candidate (must be below confirm).",
    "BAYAAN_ALIGNMENT__LEXICAL_WEIGHT": "Weight of lexical similarity in the alignment score.",
    "BAYAAN_ALIGNMENT__FUZZY_WEIGHT": "Weight of fuzzy similarity in the alignment score.",
    "BAYAAN_ALIGNMENT__TEMPORAL_WEIGHT": "Weight of timing in the alignment score.",
    "BAYAAN_ALIGNMENT__CONTINUITY_WEIGHT": "Weight of neighbour continuity in the alignment score.",
    # Scripture
    "BAYAAN_SCRIPTURE__NEAR_MATCH_THRESHOLD": "Score for a near-verified scripture quote (VERIFIED_NEAR).",
    "BAYAAN_SCRIPTURE__REVIEW_THRESHOLD": "Score at/above which a quote needs manual review.",
    "BAYAAN_SCRIPTURE__QURAN_BLOCK_WITHOUT_REFERENCE": "Block Quran verses that have no approved reference.",
    "BAYAAN_SCRIPTURE__HADITH_FALLBACK_GENERAL": "Allow a general (non-attributed) translation when a hadith reference is missing.",
    # Auth / operator login
    "BAYAAN_AUTH__USERNAME": "Operator console login username. CHANGE BEFORE AN EVENT.",
    "BAYAAN_AUTH__PASSWORD": "Operator console login password. CHANGE BEFORE AN EVENT.",
    "BAYAAN_AUTH__OPERATOR_TOKEN_SECRET": "Secret used to sign operator session tokens. CHANGE TO A LONG RANDOM VALUE.",
    "BAYAAN_AUTH__TOKEN_TTL_HOURS": "Hours an operator login stays valid before re-authentication.",
    # Live transcript
    "BAYAAN_LIVE_TRANSCRIPT__ENABLED": "Segment the live khutbah into transcript segments.",
    "BAYAAN_LIVE_TRANSCRIPT__MIN_WORDS_PER_SEGMENT": "Fewest words a transcript segment may contain.",
    "BAYAAN_LIVE_TRANSCRIPT__MAX_WORDS_PER_SEGMENT": "Most words a transcript segment may contain.",
    "BAYAAN_LIVE_TRANSCRIPT__MAX_CHARS_PER_SEGMENT": "Most characters a transcript segment may contain.",
    # Security
    "BAYAAN_SECURITY__VISITOR_TOKEN_SECRET": "Secret used to sign worshipper visitor tokens. CHANGE TO A LONG RANDOM VALUE.",
    "BAYAAN_SECURITY__VISITOR_TOKEN_TTL_MINUTES": "Minutes a visitor token stays valid.",
    # Rate limiting
    "BAYAAN_RATE_LIMIT__ENABLED": "Master switch for per-category rate limits.",
    "BAYAAN_RATE_LIMIT__DISCOVERY_PER_MINUTE": "Max discovery requests per minute.",
    "BAYAAN_RATE_LIMIT__WS_PER_MINUTE": "Max WebSocket connections per minute.",
    "BAYAAN_RATE_LIMIT__ADMIN_PER_MINUTE": "Max admin/operator requests per minute.",
    # Realtime
    "BAYAAN_REALTIME__HISTORY_SIZE": "Recent caption messages replayed to a late-joining viewer.",
    # Room
    "BAYAAN_ROOM__CODE_LENGTH": "Number of characters in a room join code (4-32).",
    # Sermon
    "BAYAAN_SERMON__MAX_UPLOAD_BYTES": "Maximum allowed sermon upload size in bytes (10 MB default).",
    "BAYAAN_SERMON__ALLOWED_EXTENSIONS": 'JSON list of accepted upload formats, e.g. ["txt","md","docx","pdf"].',
    "BAYAAN_SERMON__MIN_SEGMENT_CHARS": "Shortest sermon segment kept after segmentation.",
    "BAYAAN_SERMON__MAX_SEGMENT_CHARS": "Longest sermon segment allowed after segmentation.",
    "BAYAAN_SERMON__STORAGE_ROOT": "Folder where uploaded sermon drafts are stored.",
    # Common
    "BAYAAN_MAX_ACTIVE_SESSIONS": "Max simultaneous active sermon sessions (bounded resources).",
    "BAYAAN_PUBLIC_BASE_URL": "URL phones actually reach the app at, e.g. http://192.168.1.20:5173. "
    "Set to your LAN address so QR codes/join links work from phones. Empty = use the request's address.",
}
# ---------------------------------------------------------------------------
# .env read / write  (preserves comments and line order)
# ---------------------------------------------------------------------------

KEYLINE_RE = re.compile(
    r"^\s*(?P<mark>#)?\s*(?P<key>[A-Z0-9_]+)\s*=\s*(?P<val>.*?)(\s+#.*)?$"
)


def parse_env_line(line: str):
    """Return ``(kind, key, value, commented)`` for a .env line.

    kind is 'blank' | 'comment' | 'assign'. Keys are uppercase.
    """
    stripped = line.rstrip()
    if not stripped.strip():
        return ("blank", None, None, None)
    match = KEYLINE_RE.match(stripped)
    if match:
        val = match.group("val").strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1].strip()
        return ("assign", match.group("key").upper(), val, match.group("mark") == "#")
    return ("comment", None, None, None)


def read_env_values(path: Path) -> dict[str, str]:
    """Active (un-commented) KEY=VALUE pairs in a .env file."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        kind, key, value, commented = parse_env_line(line)
        if kind == "assign" and not commented:
            out[key] = value
    return out


def apply_env_updates(path: Path, updates: dict[str, str]) -> list[str]:
    """Write KEY=VALUE lines, preserving order and existing comment state.

    * A line that is currently ACTIVE stays active (updated in place) — we
      never comment out an existing setting the user has configured, even if
      its new value happens to equal the built-in default.
    * A line that is currently COMMENTED stays commented unless the new value
      differs from the default (then it must become active to take effect).
    * A key that does not exist yet is appended; if it equals the default it
      is appended commented to stay consistent with ``.env.example``.

    Returns the keys that actually changed.
    """
    original = path.read_text(encoding="utf-8").splitlines(keepends=True)
    defaults = {opt["key"]: opt["default"] for opt in build_options()}
    changed: list[str] = []
    output: list[str] = []
    remaining = dict(updates)
    for line in original:
        stripped = line.rstrip("\n").rstrip("\r")
        kind, key, _value, commented = parse_env_line(stripped)
        if kind == "assign" and key in remaining:
            new_value = remaining.pop(key)
            if commented and new_value != defaults.get(key):
                # Was commented but the value moved off-default -> activate.
                new_line = f"{key}={new_value}\n"
            elif commented:
                # Was commented and still matches the default -> stay commented.
                new_line = f"#{key}={new_value}\n"
            else:
                # Was active -> keep it active no matter what.
                new_line = f"{key}={new_value}\n"
            if new_line.rstrip() != stripped:
                changed.append(key)
            output.append(new_line)
        else:
            output.append(line)
    if remaining:
        if output and not output[-1].endswith("\n"):
            output[-1] += "\n"
        output.append("\n# --- Set from the Bayaan Control Panel ---\n")
        for key, new_value in remaining.items():
            if new_value == defaults.get(key):
                output.append(f"#{key}={new_value}\n")
            else:
                output.append(f"{key}={new_value}\n")
            changed.append(key)
    if not changed:
        return []
    _atomic_write(path, "".join(output))
    return changed


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------


def _validated_list(opt: dict, items: list) -> tuple[bool, str, str | None]:
    """Final check for ``list`` values; the language list is curated (ADR-0017).

    The server refuses an unknown language or an empty target set at startup, so
    the panel refuses to write one in the first place (§184: fail where the
    operator can still fix it).
    """
    if opt.get("key") != TARGET_LANGUAGES_KEY:
        return True, json.dumps(items, ensure_ascii=False), None
    allowed = {choice["code"] for choice in language_picker_choices()}
    unknown = [item for item in items if item not in allowed]
    if unknown:
        return (
            False,
            "",
            f"Unsupported language(s): {', '.join(map(str, unknown))}. "
            f"Choose from: {', '.join(sorted(allowed))}.",
        )
    if not items:
        return False, "", "Pick at least one language."
    return True, json.dumps(items, ensure_ascii=False), None


def validate_value(opt: dict, raw) -> tuple[bool, str, str | None]:
    """Validate input against the option. Returns (ok, value_to_save, error)."""
    kind = opt["kind"]
    value = str(raw).strip() if not isinstance(raw, bool) else ("true" if raw else "false")
    if kind == "bool":
        lowered = value.lower()
        if lowered in ("true", "1", "yes", "on"):
            return True, "true", None
        if lowered in ("false", "0", "no", "off", ""):
            return True, "false", None
        return False, "", "Enter true or false."
    if kind == "int":
        try:
            return True, str(int(value)), None
        except (TypeError, ValueError):
            return False, "", "Enter a whole number."
    if kind == "float":
        try:
            return True, str(float(value)), None
        except (TypeError, ValueError):
            return False, "", "Enter a number."
    if kind == "select":
        if value in opt["choices"]:
            return True, value, None
        return False, "", f"Choose one of: {', '.join(map(str, opt['choices']))}."
    if kind == "list":
        cleaned = value if value.startswith("[") else f"[{value}]"
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            # Fall back to a plain comma-separated list of bare strings,
            # e.g. "fr, en" -> ["fr", "en"] (comma required on this path).
            if "," in value:
                items = [item.strip() for item in value.split(",") if item.strip()]
                if items and all(isinstance(i, str) for i in items):
                    return _validated_list(opt, items)
            return False, "", "Enter a JSON list, e.g. [\"it\",\"en\"]."
        if not isinstance(parsed, list):
            return False, "", "Enter a JSON list, e.g. [\"it\",\"en\"]."
        return _validated_list(opt, parsed)
    return True, str(value), None
# ---------------------------------------------------------------------------
# LAN / reachability state (persisted next to .env, never committed)
# ---------------------------------------------------------------------------


def load_panel_state() -> dict:
    if PANEL_STATE_FILE.is_file():
        try:
            return json.loads(PANEL_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - corrupt state file, start clean
            pass
    return {"lan": False}


def save_panel_state(state: dict) -> None:
    _atomic_write(PANEL_STATE_FILE, json.dumps(state, indent=2))


def lan_enabled() -> bool:
    return bool(load_panel_state().get("lan", False))


def set_lan_enabled(enabled: bool) -> None:
    state = load_panel_state()
    state["lan"] = bool(enabled)
    save_panel_state(state)


# ---------------------------------------------------------------------------
# Process management (backend = uvicorn, frontend = Vite dev server)
# ---------------------------------------------------------------------------


class ServiceProcess:
    """One server process. Logs go to a bounded ring buffer, termination uses
    taskkill (tree) on Windows so child processes die with the parent."""

    def __init__(self, name: str):
        self.name = name
        self.process: subprocess.Popen | None = None
        self.lines: deque[tuple[int, str]] = deque(maxlen=MAX_LOG_LINES)
        self.lock = threading.Lock()
        self._seq = 0
        self.last_error: str = ""

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def log_append(self, line: str) -> None:
        with self.lock:
            self._seq += 1
            self.lines.append((self._seq, line))

    def log_tail(self, since: int = 0) -> tuple[list[tuple[int, str]], int]:
        with self.lock:
            return [item for item in self.lines if item[0] > since], self._seq

    def clear_log(self) -> None:
        with self.lock:
            self.lines.clear()

    def start(self, cmd: list[str], cwd: Path) -> tuple[bool, str]:
        if self.running():
            return False, f"{self.name} is already running."
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            self.last_error = f"Executable not found: {exc.filename}"
            return False, self.last_error
        except OSError as exc:
            self.last_error = f"Could not start {self.name}: {exc}"
            return False, self.last_error
        threading.Thread(target=self._pump, daemon=True, name=f"log-{self.name}").start()
        return True, ""

    def _pump(self) -> None:
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        try:
            for raw_line in proc.stdout:
                self.log_append(raw_line.rstrip("\n"))
            rest = proc.stdout.read()
            if rest:
                for raw_line in rest.splitlines():
                    self.log_append(raw_line.rstrip("\n"))
        except Exception:  # noqa: BLE001 - log reader must never raise
            pass

    def stop(self) -> None:
        proc = self.process
        self.process = None
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:  # noqa: BLE001 - fall back to terminate()
                proc.terminate()
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


BACKEND_PROC = ServiceProcess("backend")
FRONTEND_PROC = ServiceProcess("frontend")
def backend_command(host: str) -> list[str]:
    python = resolve_python() or sys.executable
    return [
        python,
        "-m",
        "uvicorn",
        "bayaan_server.api.app:create_app",
        "--factory",
        "--host",
        host,
        "--port",
        str(BACKEND_PORT),
    ]


def frontend_command(host: str) -> list[str]:
    node = resolve_node()
    vite_js = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"
    if node and vite_js.is_file():
        return [
            node,
            str(vite_js),
            "--host",
            host,
            "--port",
            str(FRONTEND_PORT),
            "--strictPort",
            "--clearScreen",
            "false",
        ]
    return [
        "npm.cmd",
        "run",
        "dev",
        "--",
        "--host",
        host,
        "--port",
        str(FRONTEND_PORT),
        "--strictPort",
    ]


def service_host() -> str:
    return "0.0.0.0" if lan_enabled() else DEFAULT_HOST


def start_backend() -> tuple[bool, str]:
    err = detect_backend_deps(resolve_python())
    if err is not None:
        return False, err
    return BACKEND_PROC.start(backend_command(service_host()), BACKEND_DIR)


def start_frontend() -> tuple[bool, str]:
    node = resolve_node()
    vite_js = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"
    if not node and not vite_js.is_file():
        return False, (
            "Node.js or the frontend dependencies were not found. "
            "Install Node.js from https://nodejs.org, then run `npm install` "
            "inside the `frontend` folder."
        )
    return FRONTEND_PROC.start(frontend_command(service_host()), FRONTEND_DIR)


def stop_backend() -> None:
    BACKEND_PROC.stop()


def stop_frontend() -> None:
    FRONTEND_PROC.stop()


def backend_ready() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def frontend_ready() -> bool:
    try:
        with socket.create_connection((DEFAULT_HOST, FRONTEND_PORT), timeout=2):
            return True
    except Exception:  # noqa: BLE001
        return False


def wait_ready(check, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.4)
    return check()


def start_all() -> dict:
    """Start backend first, wait for it to be healthy, then start the frontend."""
    results: dict[str, str | None] = {"backend": None, "frontend": None}
    ok, err = start_backend()
    results["backend"] = None if ok else err
    if ok and not wait_ready(backend_ready, BACKEND_READY_TIMEOUT_S):
        BACKEND_PROC.last_error = (
            f"Backend started but /health did not answer in "
            f"{BACKEND_READY_TIMEOUT_S}s. See the Logs tab."
        )
    ok2, err2 = start_frontend()
    results["frontend"] = None if ok2 else err2
    if ok2 and not wait_ready(frontend_ready, FRONTEND_READY_TIMEOUT_S):
        FRONTEND_PROC.last_error = (
            f"Frontend started but :{FRONTEND_PORT} did not answer in "
            f"{FRONTEND_READY_TIMEOUT_S}s. See the Logs tab."
        )
    return results


def stop_all() -> None:
    stop_frontend()
    stop_backend()


def service_status() -> dict[str, Any]:
    return {
        "backend": {
            "running": BACKEND_PROC.running(),
            "ready": backend_ready() if BACKEND_PROC.running() else False,
            "error": BACKEND_PROC.last_error,
        },
        "frontend": {
            "running": FRONTEND_PROC.running(),
            "ready": frontend_ready() if FRONTEND_PROC.running() else False,
            "error": FRONTEND_PROC.last_error,
        },
        "lan": lan_enabled(),
        "host": service_host(),
        "backend_port": BACKEND_PORT,
        "frontend_port": FRONTEND_PORT,
        "panel_port": PANEL_PORT,
        "open_url": (
            f"http://{DEFAULT_HOST}:{FRONTEND_PORT}/"
            if (FRONTEND_PROC.running() and frontend_ready())
            else None
        ),
    }
# ---------------------------------------------------------------------------
# HTTP API + UI
# ---------------------------------------------------------------------------

PANEL_TOKEN = os.environ.get("BAYAAN_PANEL_TOKEN") or os.urandom(16).hex()


def _redact(line: str) -> str:
    """Hide any stored secret value that appears in a log line."""
    if not any(tok in line.upper() for tok in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
        return line
    secrets = [v for v in read_env_values(ENV_FILE).values() if len(v) >= 6]
    for secret in secrets:
        line = line.replace(secret, "••••••")
    return line


def build_options_payload(env_file: Path | None = None) -> dict:
    """Assemble the options payload for the UI. Secret values are never
    transmitted, only whether they are currently set (source == 'env')."""
    env_values = read_env_values(env_file or ENV_FILE)
    options = build_options()
    for opt in options:
        value = env_values.get(opt["key"])
        if value is None:
            opt.update({"value": opt["default"], "source": "default"})
        else:
            opt.update({"value": value, "source": "env"})
        if opt["secret"]:
            opt["value"] = ""  # UI shows "set"/"empty" instead
    return {"options": options, "env_file": str(env_file or ENV_FILE)}


class PanelHandler(BaseHTTPRequestHandler):
    """Local-only handler. The panel is bound to DEFAULT_HOST (127.0.0.1)."""

    server_version = "BayaanPanel/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - silence default logging
        return

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_page(self) -> None:
        body = PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    def _options_payload(self) -> dict:
        return build_options_payload(ENV_FILE)

    def do_GET(self):  # noqa: C901 - small route table
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._send_page()
        if path == "/api/status":
            return self._send_json(service_status())
        if path == "/api/options":
            return self._send_json(self._options_payload())
        if path == "/api/logs":
            query = parse_qs(parsed.query)
            service = query.get("service", ["backend"])[0]
            since = int(query.get("since", ["0"])[0] or "0")
            proc = BACKEND_PROC if service == "backend" else FRONTEND_PROC
            tail, seq = proc.log_tail(since)
            lines = [{"id": i, "text": _redact(t)} for i, t in tail]
            return self._send_json({"lines": lines, "next": seq, "service": service})
        return self._send_json({"error": "Not found"}, status=404)

    def do_POST(self):  # noqa: C901 - small route table
        from urllib.parse import urlparse

        path = urlparse(self.path).path
        payload = self._read_json()
        if path == "/api/process":
            return self._handle_process(payload)
        if path == "/api/save":
            return self._handle_save(payload)
        if path == "/api/lan":
            enabled = bool(payload.get("enabled", False))
            need_restart = bool(BACKEND_PROC.running() or FRONTEND_PROC.running())
            set_lan_enabled(enabled)
            return self._send_json(
                {
                    "ok": True,
                    "lan": enabled,
                    "notice": (
                        "Servers are running; restart them for the new address "
                        "to take effect."
                        if need_restart
                        else ""
                    ),
                }
            )
        if path == "/api/log-clear":
            service = payload.get("service", "backend")
            proc = BACKEND_PROC if service == "backend" else FRONTEND_PROC
            proc.clear_log()
            return self._send_json({"ok": True})
        return self._send_json({"error": "Not found"}, status=404)

    def _handle_process(self, payload: dict) -> None:
        action = payload.get("action")
        service = payload.get("service", "all")
        if action == "start":
            if service in ("backend", "frontend", "all"):
                # Start in a background thread so the browser request returns
                # immediately; the UI polls /api/status until services are ready.
                threading.Thread(
                    target=self._start_async, args=(service,), daemon=True
                ).start()
                return self._send_json({"ok": True, "starting": service})
            return self._send_json({"error": f"Unknown service {service}"}, status=400)
        if action == "stop":
            if service == "all":
                stop_all()
            elif service == "backend":
                stop_backend()
            else:
                stop_frontend()
            return self._send_json({"ok": True})
        return self._send_json({"error": f"Unknown action {action}"}, status=400)

    def _start_async(self, service: str) -> None:
        if service in ("backend", "all"):
            ok, err = start_backend()
            if not ok:
                BACKEND_PROC.last_error = err
            elif not wait_ready(backend_ready, BACKEND_READY_TIMEOUT_S):
                BACKEND_PROC.last_error = (
                    f"Backend started but /health did not answer in "
                    f"{BACKEND_READY_TIMEOUT_S}s. See the Logs tab."
                )
        if service in ("frontend", "all"):
            ok, err = start_frontend()
            if not ok:
                FRONTEND_PROC.last_error = err
            elif not wait_ready(frontend_ready, FRONTEND_READY_TIMEOUT_S):
                FRONTEND_PROC.last_error = (
                    f"Frontend started but :{FRONTEND_PORT} did not answer in "
                    f"{FRONTEND_READY_TIMEOUT_S}s. See the Logs tab."
                )

    def _handle_save(self, payload: dict) -> None:
        values = payload.get("values", {})
        options = [opt for opt in build_options() if not opt["secret"]]
        options_map = {opt["key"]: opt for opt in options}
        updates: dict[str, str] = {}
        errors: list[dict] = []
        for key, raw in values.items():
            opt = options_map.get(key)
            if opt is None or raw is None:
                continue  # unchanged / unknown / secret
            ok, saved, err = validate_value(opt, raw)
            if ok:
                updates[key] = saved
            else:
                errors.append({"key": key, "error": err or "Invalid value"})
        if errors:
            return self._send_json({"ok": False, "errors": errors}, status=400)
        changed = apply_env_updates(ENV_FILE, updates)
        return self._send_json({"ok": True, "changed": changed})
# ---------------------------------------------------------------------------
# UI — one self-contained page using the Bayaan design tokens.
# ---------------------------------------------------------------------------

_PAGE_CSS = r"""
:root {
  --bg:#0f1310; --surface:#161c17; --surface-2:#1b231d;
  --border:#26302a; --border-strong:#36443b;
  --text:#e7ece6; --text-2:#b4bdb2; --muted:#8d988c;
  --primary:#1c6d4d; --primary-hover:#23825d; --primary-strong:#2fa376;
  --primary-soft:rgba(47,163,118,.13); --primary-line:rgba(47,163,118,.3);
  --accent:#34d399; --accent-ring:rgba(52,211,153,.2);
  --success:#4ade80; --success-soft:rgba(74,222,128,.12);
  --warning:#f0b84e; --warning-soft:rgba(240,184,78,.12);
  --danger:#f5847b; --danger-soft:rgba(245,132,123,.13);
  --gold:#f2c94c; --green:#1c6d4d;
}
* { box-sizing:border-box; }
html,body { margin:0; padding:0; }
body {
  background:var(--bg); color:var(--text);
  font-family:"Segoe UI",system-ui,-apple-system,sans-serif;
  font-size:14px; line-height:1.5;
  display:flex; flex-direction:column; min-height:100vh;
}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--border-strong);border-radius:999px}

.dash-nav {
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  padding:12px 24px; background:var(--surface);
  border-bottom:1px solid var(--border); position:sticky; top:0; z-index:50;
  flex-wrap:wrap;
}
.nav-brand{display:flex;align-items:center;gap:12px}
.brand-badge{
  background:var(--primary); color:var(--gold); font-weight:800;
  font-size:13px; letter-spacing:.12em; padding:5px 11px; border-radius:7px;
}
.brand-title{font-weight:700;font-size:15px}
.nav-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pill{
  display:inline-flex; align-items:center; gap:6px; padding:4px 10px;
  border-radius:999px; font-size:12px; font-weight:600; white-space:nowrap;
  border:1px solid var(--border-strong);
}
.pill.on{background:var(--success-soft);color:var(--success);border-color:transparent}
.pill.off{background:var(--danger-soft);color:var(--danger);border-color:transparent}
.pill.starting{background:var(--warning-soft);color:var(--warning);border-color:transparent}
.pill .dot{width:8px;height:8px;border-radius:50%;background:currentColor}

.layout{flex:1;display:grid;grid-template-columns:340px 1fr;gap:18px;padding:18px 24px}
@media (max-width:980px){ .layout{grid-template-columns:1fr} }
.side{display:flex;flex-direction:column;gap:14px}
.main{display:flex;flex-direction:column;gap:14px;min-width:0}

.card{
  background:var(--surface); border:1px solid var(--border);
  border-radius:12px; padding:16px; display:flex; flex-direction:column; gap:12px;
}
.card h2{margin:0;font-size:15px;font-weight:700}
.card .sub{color:var(--muted);font-size:12px;margin:0}

.hero{display:flex;flex-direction:column;gap:12px}
.hero-buttons{display:flex;gap:10px;flex-wrap:wrap}
.btn{
  border:1px solid var(--border-strong); background:var(--surface-2);
  color:var(--text); border-radius:9px; padding:8px 16px; font-size:14px;
  font-weight:600; cursor:pointer; transition:filter .12s, transform .03s;
}
.btn:hover{border-color:var(--primary);color:var(--primary-strong)}
.btn:active{transform:translateY(.5px)}
.btn:disabled{opacity:.55;cursor:not-allowed}
.btn-primary{background:var(--primary);color:#fff;border-color:transparent}
.btn-primary:hover{background:var(--primary-hover);color:#fff}
.btn-live{background:#d43b2e;color:#fff;font-weight:700;border-color:transparent}
.btn-live:hover{background:#b9362a;color:#fff}
.btn-lg{font-size:16px;padding:12px 22px;border-radius:11px}
.btn-danger{background:var(--danger);color:#fff;border-color:transparent}
.btn-danger:hover{background:#d4594e}
.btn-sm{font-size:12px;padding:5px 10px;border-radius:7px;border:1px solid var(--border-strong)}

.service-row{
  display:flex; align-items:center; gap:10px; padding:10px 0;
  border-bottom:1px solid var(--border);
}
.service-row:last-child{border-bottom:none}
.service-info{flex:1;min-width:0}
.service-name{font-weight:600}
.service-meta{color:var(--muted);font-size:12px}

.switch{position:relative;display:inline-block;width:44px;height:24px;flex:none}
.switch input{opacity:0;width:0;height:0}
.slider{
  position:absolute;cursor:pointer;inset:0;background:var(--border-strong);
  transition:.15s;border-radius:999px;
}
.slider:before{
  content:"";position:absolute;height:18px;width:18px;left:3px;bottom:3px;
  background:#fff;transition:.15s;border-radius:50%;
}
input:checked + .slider{background:var(--primary)}
input:checked + .slider:before{transform:translateX(20px)}
input:focus + .slider{box-shadow:0 0 0 3px var(--accent-ring)}

.tabs{display:flex;gap:6px;border-bottom:1px solid var(--border);padding-bottom:8px}
.tab-btn{
  border:1px solid transparent; background:transparent; color:var(--muted);
  padding:7px 14px; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer;
}
.tab-btn.active{background:var(--surface-2);color:var(--text);border-color:var(--border-strong)}
.tab-panel{display:none}
.tab-panel.active{display:block}

.searchbar{
  width:100%; padding:9px 12px; border-radius:9px; border:1px solid var(--border-strong);
  background:var(--surface-2); color:var(--text); font-size:13px;
}
.searchbar::placeholder{color:var(--muted)}

.group{margin-bottom:18px}
.group-title{
  font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.08em;
  color:var(--gold); margin:14px 0 6px;
}
.opt-row{
  display:grid; grid-template-columns:minmax(240px,1.2fr) minmax(200px,1fr); gap:14px;
  padding:11px 0; border-bottom:1px solid var(--border); align-items:center;
}
.opt-row.dirty{background:var(--primary-soft);border-radius:8px;padding-left:8px}
.opt-desc{color:var(--muted);font-size:12px;margin-top:2px}
.opt-key{font-family:Consolas,monospace;font-weight:600;font-size:12px;color:var(--text-2);word-break:break-all}
.opt-control{display:flex;flex-direction:column;gap:4px;align-items:flex-start}
.opt-input{
  width:100%; padding:7px 10px; border-radius:8px; border:1px solid var(--border-strong);
  background:var(--surface-2); color:var(--text); font-size:13px;
}
.opt-input:focus{outline:none;border-color:var(--primary-strong)}
.check-label{display:flex;gap:8px;align-items:center;cursor:pointer;font-size:13px;gap:10px}
.lang-picker{display:flex;flex-wrap:wrap;gap:6px}
.lang-chip{
  display:inline-flex; align-items:center; gap:6px; padding:5px 10px; border-radius:999px;
  border:1px solid var(--border-strong); background:var(--surface-2); cursor:pointer; font-size:12px;
}
.lang-chip.on{border-color:var(--primary-strong); background:var(--primary-soft); font-weight:600}

.savebar{
  position:sticky; bottom:14px; display:flex; gap:10px; align-items:center;
  background:var(--surface); border:1px solid var(--border-strong); border-radius:12px;
  padding:12px 16px; margin:10px 24px 0; flex-wrap:wrap;
  box-shadow:0 4px 20px rgba(0,0,0,.4);
}
.savebar .hint{color:var(--warning);font-size:12px;flex:1;min-width:200px}
.notice{padding:9px 12px; border-radius:9px; font-size:13px; display:none}
.notice.show{display:block}
.notice.ok{background:var(--success-soft);color:var(--success)}
.notice.err{background:var(--danger-soft);color:var(--danger)}

.logs-wrap{display:flex;flex-direction:column;gap:10px}
.log-meta{display:flex;justify-content:space-between;align-items:center;gap:10px}
.log-pane{
  background:#0b0e0b; border:1px solid var(--border); border-radius:10px;
  font-family:Consolas,monospace; font-size:12px; padding:10px 12px;
  height:240px; overflow:auto; white-space:pre-wrap; word-break:break-word;
  color:var(--text-2);
}
.footer{
  padding:14px 24px; border-top:1px solid var(--border); color:var(--muted);
  font-size:12px; text-align:center;
}
"""
_PAGE_BODY = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bayaan Control Panel</title>
<style>
{__CSS__}
</style>
</head>
<body>
<nav class="dash-nav">
  <div class="nav-brand">
    <span class="brand-badge">BAYAAN</span>
    <span class="brand-title">Control Panel</span>
  </div>
  <div class="nav-actions">
    <span id="lanPill" class="pill off"><span class="dot"></span>Local</span>
    <span id="bePill" class="pill off"><span class="dot"></span>Backend</span>
    <span id="fePill" class="pill off"><span class="dot"></span>Frontend</span>
  </div>
</nav>

<div class="layout">
  <aside class="side">
    <div class="card">
      <h2>Bayaan</h2>
      <p class="sub">Starts the backend (the brain) first, then the frontend (the website).</p>
      <div class="hero-buttons">
        <button id="startAllBtn" class="btn btn-primary btn-lg">▶ Start Bayaan</button>
        <button id="stopAllBtn" class="btn btn-danger btn-lg">■ Stop Bayaan</button>
      </div>
      <button id="openBtn" class="btn btn-lg" disabled>Open Bayaan</button>
      <div id="heroNotice" class="notice"></div>
    </div>

    <div class="card">
      <h2>Services</h2>
      <div id="services"><p class="sub">Loading…</p></div>
      <p class="sub">Use the individual Start/Stop buttons when you only need one of the two.</p>
    </div>

    <div class="card">
      <h2>Phones on the same Wi-Fi</h2>
      <p class="sub">OFF by default: Bayaan then only works on this PC (127.0.0.1). Turn this on only when you are ready to allow phones/tablets on your network to connect. The control panel itself always stays on 127.0.0.1.</p>
      <label class="check-label">
        <span class="switch"><input type="checkbox" id="lanToggle"><span class="slider"></span></span>
        Allow phones to connect
      </label>
      <p id="lanNotice" class="notice"></p>
      <p class="sub">When on, remember to set <b>BAYAAN_PUBLIC_BASE_URL</b> in Settings to your PC's LAN address so QR codes work from phones.</p>
    </div>
  </aside>

  <main class="main">
    <div class="card">
      <div class="tabs">
        <button class="tab-btn active" data-tab="settings">Settings</button>
        <button class="tab-btn" data-tab="logs">Logs</button>
      </div>

      <div class="tab-panel active" id="tab-settings">
        <input id="search" class="searchbar" type="text" placeholder="Search settings, e.g. TTS, port, key…">
        <div id="options"></div>
      </div>

      <div class="tab-panel" id="tab-logs">
        <div class="logs-wrap">
          <div class="log-meta">
            <h2>Backend — uvicorn (port 8000)</h2>
            <button class="btn btn-sm" data-clear="backend">Clear</button>
          </div>
          <div class="log-pane" id="log-backend"></div>
          <div class="log-meta">
            <h2>Frontend — Vite (port 5173)</h2>
            <button class="btn btn-sm" data-clear="frontend">Clear</button>
          </div>
          <div class="log-pane" id="log-frontend"></div>
        </div>
      </div>
    </div>
  </main>
</div>

<div id="savebar" class="savebar" hidden>
  <span class="hint">Changes are saved to backend/.env. Restart the servers for them to take effect.</span>
  <button id="discardBtn" class="btn">Discard</button>
  <button id="saveBtn" class="btn btn-primary">Save settings</button>
</div>

<footer class="footer">
  Bayaan Control Panel · runs locally on this PC · settings apply after a server restart
</footer>

<script>
{__JS__}
</script>
</body>
</html>
"""
_PAGE_JS = r"""
let status = null;
let options = [];
let edited = {};       // key -> new value (only changed fields)
let logCursor = { backend: 0, frontend: 0 };

async function api(path, body) {
  const req = {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
  const res = await fetch(path, req);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data && (data.error || (data.errors && JSON.stringify(data.errors)))) || ("HTTP " + res.status));
  return data;
}

function el(id) { return document.getElementById(id); }
function pill(id, state, label) {
  const map = { on: ["on", label], off: ["off", label], starting: ["starting", label + "…"] };
  const [cls, text] = map[state] || map.off;
  const node = el(id);
  node.className = "pill " + cls;
  node.innerHTML = '<span class="dot"></span>' + text;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
function notice(kind, msg) {
  const node = el("heroNotice");
  node.className = "notice show " + kind;
  node.textContent = msg;
  clearTimeout(window.__nt);
  window.__nt = setTimeout(() => { node.className = "notice"; }, 8000);
}

async function refreshStatus() {
  try {
    const st = await api("/api/status");
    status = st;
    const be = st.backend, fe = st.frontend;
    pill("bePill", be.running ? (be.ready ? "on" : "starting") : "off", "Backend");
    pill("fePill", fe.running ? (fe.ready ? "on" : "starting") : "off", "Frontend");
    pill("lanPill", st.lan ? "on" : "off", st.lan ? "LAN enabled" : "Local only");
    el("lanToggle").checked = !!st.lan;
    const openBtn = el("openBtn");
    openBtn.disabled = !st.open_url;
    openBtn.textContent = st.open_url ? "Open Bayaan" : (fe.running ? "Waiting for frontend…" : "Open Bayaan");
    if (be.error || fe.error) notice("err", [be.error, fe.error].filter(Boolean).join(" | "));
    renderServices(st);
  } catch (e) { /* transient */ }
}

function renderServices(st) {
  const wrap = el("services");
  const rows = ["backend", "frontend"].map((name) => {
    const info = st[name];
    const state = info.running ? (info.ready ? "on" : "starting") : "off";
    const label = name === "backend" ? "Backend · port 8000" : "Frontend · port 5173";
    const stateText = info.running ? (info.ready ? "Running" : "Starting…") : "Stopped";
    const verb = info.running ? "Stop" : "Start";
    return `<div class="service-row">
      <span class="pill ${state}"><span class="dot"></span>${stateText}</span>
      <div class="service-info">
        <div class="service-name">${label}</div>
        <div class="service-meta">${esc(info.error || "")}</div>
      </div>
      <button class="btn btn-sm" onclick="toggleService('${name}')">${verb}</button>
    </div>`;
  }).join("");
  wrap.innerHTML = rows;
}

async function toggleService(name) {
  const running = status && status[name] && status[name].running;
  await api("/api/process", { action: running ? "stop" : "start", service: name });
  refreshStatus();
}

function groupTitle(group) {
  const g = (group || "").replace("BAYAAN_", "");
  return g.split("__")[0] || "Core";
}
function renderOptions() {
  const q = (el("search").value || "").toLowerCase();
  const groups = {};
  for (const opt of options) {
    const g = groupTitle(opt.key);
    (groups[g] = groups[g] || []).push(opt);
  }
  let html = "";
  for (const [g, list] of Object.entries(groups)) {
    const visible = list.filter(o =>
      !q ||
      (o.key || "").toLowerCase().includes(q) ||
      (o.description || "").toLowerCase().includes(q) ||
      (o.name || "").toLowerCase().includes(q));
    if (!visible.length) continue;
    html += `<div class="group"><div class="group-title">${esc(g)}</div>`;
    for (const opt of visible) html += optRow(opt);
    html += "</div>";
  }
  el("options").innerHTML = html || '<p class="sub">No settings match your search.</p>';
  updateSavebar();
  // Re-bind after every render: the rows are recreated on each change, so a
  // listener attached only once (at load) would stop working after the first
  // edit.
  document.querySelectorAll("[data-key]").forEach((node) => {
    const opt = options.find((o) => o.key === node.dataset.key);
    if (opt) bindRow(opt);
  });
}

function parseListValue(raw) {
  try {
    const parsed = JSON.parse(raw || "[]");
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch (_e) {
    return [];
  }
}

function optRow(opt) {
  const dirty = edited[opt.key] !== undefined;
  // A pending edit wins over the server value: re-rendering (search box, save,
  // discard) must never silently drop what the operator just typed.
  const pending = dirty ? edited[opt.key] : opt.value;
  const desc = opt.description ? `<div class="opt-desc">${esc(opt.description)}</div>` : "";
  let control = "";
  const key = `data-key="${esc(opt.key)}"`;
  if (opt.kind === "bool") {
    const checked = pending === "true" || pending === "1";
    control = `<label class="check-label">
      <span class="switch"><input type="checkbox" ${key} ${checked ? "checked" : ""}><span class="slider"></span></span>
      ${checked ? "Enabled" : "Disabled"}</label>`;
  } else if (opt.kind === "select") {
    control = `<select ${key} class="opt-input">${opt.choices.map(c =>
      `<option value="${esc(c)}" ${String(pending) === String(c) ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  } else if (opt.kind === "list" && Array.isArray(opt.choices) && opt.choices.length) {
    // ADR-0017: curated pick-list (languages) - tick, never type. The hidden
    // input keeps the JSON value the rest of the panel already reads/writes.
    const selected = parseListValue(pending);
    const chips = opt.choices.map(c => {
      const on = selected.includes(c.code);
      return `<label class="lang-chip ${on ? "on" : ""}" title="${esc(c.english)}">
        <input type="checkbox" class="lang-chip-box" data-code="${esc(c.code)}" ${on ? "checked" : ""}>
        ${esc(c.label)} <span class="sub">(${esc(c.code)})</span></label>`;
    }).join("");
    control = `<div class="lang-picker">${chips}</div>
      <input type="hidden" ${key} value="${esc(JSON.stringify(selected))}">`;
  } else if (opt.secret) {
    control = `<input type="password" ${key} class="opt-input" value="" placeholder="${dirty ? "new value set — type to replace" : (opt.source === "env" ? "•••••• set — type to replace" : "empty (default)")}">`;
  } else {
    control = `<input type="text" ${key} class="opt-input" value="${esc(pending)}" placeholder="${esc(opt.default)}">`;
  }
  return `<div class="opt-row ${dirty ? "dirty" : ""}">
    <div><div class="opt-key">${esc(opt.key)}</div>${desc}</div>
    <div class="opt-control">${control}</div>
  </div>`;
}

function applyEdit(opt, v) {
  if (opt.secret) {
    if (v) edited[opt.key] = v; else delete edited[opt.key];
  } else if (v === opt.value) {
    delete edited[opt.key];
  } else {
    edited[opt.key] = v;
  }
}

function syncRow(opt) {
  // Update only what changed: the dirty highlight and the save bar. Re-rendering
  // the whole list here would destroy the focused input after every keystroke,
  // which made the panel feel like it "could not change" a setting.
  const node = document.querySelector(`[data-key="${CSS.escape(opt.key)}"]`);
  const row = node ? node.closest(".opt-row") : null;
  if (row) row.classList.toggle("dirty", edited[opt.key] !== undefined);
  updateSavebar();
}

function bindRow(opt) {
  const node = document.querySelector(`[data-key="${CSS.escape(opt.key)}"]`);
  if (!node) return;
  const read = () => (opt.kind === "bool" ? (node.checked ? "true" : "false") : node.value);
  node.addEventListener("input", () => {
    applyEdit(opt, read());
    syncRow(opt);
  });
  node.addEventListener("change", () => {
    applyEdit(opt, read());
    syncRow(opt);
  });
  // ADR-0017 curated list picker: each chip writes the hidden JSON input, so
  // the saved value stays a normal JSON list the server already validates.
  const picker = node.parentElement;
  if (picker) {
    picker.querySelectorAll(".lang-chip-box").forEach((box) => {
      box.addEventListener("change", () => {
        // Catalog order (DOM order), not click order: the configured order is
        // what worshippers see on the room page.
        const codes = Array.from(picker.querySelectorAll(".lang-chip-box"))
          .filter((b) => b.checked)
          .map((b) => b.dataset.code);
        // Keep the hidden JSON input in sync with what we save, so the DOM and
        // `edited` can never disagree.
        node.value = JSON.stringify(codes);
        applyEdit(opt, JSON.stringify(codes));
        // Toggle the chips in place - never re-render while the operator works.
        picker.querySelectorAll(".lang-chip").forEach((chip) => {
          const inner = chip.querySelector(".lang-chip-box");
          chip.classList.toggle("on", Boolean(inner && inner.checked));
        });
        syncRow(opt);
      });
    });
  }
}

function updateSavebar() {
  el("savebar").hidden = Object.keys(edited).length === 0;
}

async function saveSettings() {
  try {
    const res = await api("/api/save", { values: edited });
    if (res.ok) {
      edited = {};
      notice("ok", "Saved " + res.changed.length + " setting(s) to backend/.env. Restart the servers to apply.");
      loadOptions();
    }
  } catch (e) {
    notice("err", "Save failed: " + e.message);
  }
}

async function loadOptions() {
  try {
    const data = await api("/api/options");
    options = data.options;
    // renderOptions() binds every row it renders.
    renderOptions();
  } catch (e) {
    el("options").innerHTML = '<p class="notice err show">Could not load settings: ' + esc(e.message) + '</p>';
  }
}

async function refreshLog(name) {
  try {
    const data = await api(`/api/logs?service=${name}&since=${logCursor[name]}`);
    const pane = el("log-" + name);
    for (const line of data.lines) pane.textContent += line.text + "\n";
    pane.scrollTop = pane.scrollHeight;
    logCursor[name] = data.next;
  } catch (e) { /* polling */ }
}

async function clearLog(name) {
  await api("/api/log-clear", { service: name });
  logCursor[name] = 0;
  el("log-" + name).textContent = "";
}

el("startAllBtn").addEventListener("click", async () => {
  notice("ok", "Starting backend first, then frontend… this can take up to a minute.");
  try {
    const res = await api("/api/process", { action: "start", service: "all" });
    if (res.ok) notice("ok", "Servers started.");
    refreshStatus();
  } catch (e) { notice("err", e.message); }
});
el("stopAllBtn").addEventListener("click", async () => {
  try {
    await api("/api/process", { action: "stop", service: "all" });
    notice("ok", "Servers stopped.");
    refreshStatus();
  } catch (e) { notice("err", e.message); }
});
el("openBtn").addEventListener("click", () => {
  if (status && status.open_url) window.open(status.open_url, "_blank");
});
el("lanToggle").addEventListener("change", async (e) => {
  try {
    const res = await api("/api/lan", { enabled: e.target.checked });
    if (res.notice) notice("err", res.notice);
    refreshStatus();
  } catch (err) { e.target.checked = !e.target.checked; notice("err", err.message); }
});
el("saveBtn").addEventListener("click", saveSettings);
el("discardBtn").addEventListener("click", () => { edited = {}; loadOptions(); });
el("search").addEventListener("input", renderOptions);
document.querySelectorAll(".tab-btn").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + btn.dataset.tab));
  })
);
document.querySelectorAll("[data-clear]").forEach((btn) =>
  btn.addEventListener("click", () => clearLog(btn.dataset.clear))
);

loadOptions();
setInterval(refreshStatus, 2500);
setInterval(() => { refreshLog("backend"); refreshLog("frontend"); }, 1500);
refreshStatus();
"""
PAGE_HTML = (
    _PAGE_BODY.replace("{__CSS__}", _PAGE_CSS).replace("{__JS__}", _PAGE_JS)
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_panel(port: int = PANEL_PORT, open_browser: bool = True) -> None:
    """Start the local-only panel server; blocks until Ctrl+C."""
    server = ThreadingHTTPServer((DEFAULT_HOST, port), PanelHandler)
    print("=" * 62)
    print(f"  BAYAAN Control Panel  ->  http://{DEFAULT_HOST}:{port}/")
    print("  The panel is bound to 127.0.0.1 (this PC only).")
    print("  Press Ctrl+C to close the panel (Bayaan keeps running if started).")
    print("=" * 62)
    if open_browser:
        webbrowser.open(f"http://{DEFAULT_HOST}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPanel stopped.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="bayaan-control-panel",
        description="Edit backend/.env in a browser and run Bayaan's servers.",
    )
    parser.add_argument("--port", type=int, default=PANEL_PORT, help="panel port")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser")
    parser.add_argument(
        "--dump-options",
        action="store_true",
        help="print all config options as JSON, then exit",
    )
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="verify backend/frontend prerequisites, then exit",
    )
    parser.add_argument(
        "--lang",
        choices=("en", "ar"),
        default="en",
        help="UI language (English only for now)",
    )
    args = parser.parse_args(argv)

    if args.dump_options:
        print(json.dumps(build_options(), ensure_ascii=False, indent=2))
        return 0

    if args.check_deps:
        problems = []
        py = resolve_python()
        err = detect_backend_deps(py)
        if err:
            problems.append(err)
        node = resolve_node()
        vite_js = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"
        if not node and not vite_js.is_file():
            problems.append(
                "Frontend needs Node.js and its npm packages. Install Node.js "
                "from https://nodejs.org then run `npm install` in `frontend`."
            )
        if problems:
            print("\n\n".join(problems))
            return 1
        print("OK: backend and frontend prerequisites look ready.")
        return 0

    run_panel(port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())