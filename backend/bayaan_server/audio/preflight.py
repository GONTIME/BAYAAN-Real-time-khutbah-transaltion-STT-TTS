"""Preflight checks (BAYAAN_MASTER_SPEC.md §184, §199).

Every external dependency is verified explicitly. A missing audio device is
a hard failure in production, a warning in development (file ingest still
works there). Nothing is assumed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from bayaan_server.config import Settings


class PreflightComponent(BaseModel):
    name: str
    status: Literal["OK", "WARN", "FAIL"]
    detail: str


class PreflightReport(BaseModel):
    status: Literal["GREEN", "YELLOW", "RED"]
    environment: str
    components: list[PreflightComponent]


def _redacted_db_url(url: str) -> str:
    """BUG-005: strip any user:password from a DB URL for display."""
    from urllib.parse import urlsplit, urlunsplit
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    except Exception:  # noqa: BLE001
        return "database configured"


async def check_database(settings: Settings) -> PreflightComponent:
    from sqlalchemy import text

    from bayaan_server.db.engine import create_engine

    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return PreflightComponent(
            name="database", status="OK", detail=_redacted_db_url(settings.database.url)
        )
    except Exception as exc:  # noqa: BLE001
        # The raw exception may embed the full URL (incl. credentials).
        # Report the error class only — never the message or the URL.
        return PreflightComponent(
            name="database", status="FAIL", detail=f"connection failed: {type(exc).__name__}"
        )
    finally:
        await engine.dispose()


async def check_audio_device(settings: Settings) -> PreflightComponent:
    """§184: verify the configured device identifier exists."""
    try:
        import sounddevice as sd
    except ImportError:
        detail = "sounddevice not installed; only file ingest available"
        if settings.environment == "production":
            return PreflightComponent(name="audio_device", status="FAIL", detail=detail)
        return PreflightComponent(name="audio_device", status="WARN", detail=detail)

    try:
        device_id = settings.audio.input_device
        if device_id == "default":
            _, input_id = sd.default.device
            if input_id == -1:
                raise RuntimeError("no default input device")
        else:
            names = [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
            if device_id not in names:
                raise RuntimeError(
                    f"configured input device '{device_id}' not found; "
                    f"available: {names}"
                )
        return PreflightComponent(
            name="audio_device", status="OK", detail=f"device '{device_id}' present"
        )
    except Exception as exc:  # noqa: BLE001
        status = "FAIL" if settings.environment == "production" else "WARN"
        return PreflightComponent(name="audio_device", status=status, detail=str(exc))


def check_providers(settings: Settings) -> PreflightComponent:
    return PreflightComponent(
        name="providers",
        status="OK",
        detail=f"asr={settings.providers.asr}(fb={settings.providers.asr_fallback or '-'}), "
        f"translation={settings.providers.translation}"
        f"(fb={settings.providers.translation_fallback or '-'}), "
        f"tts={settings.providers.tts}(fb={settings.providers.tts_fallback or '-'})",
    )


def check_resilience(settings: Settings) -> PreflightComponent:
    """§54/§87/§111 Phase 6: ASR and translation must have at least one route;
    TTS is optional (§201 text-first, locked by default)."""
    missing = [
        cap
        for cap, primary, fallback in (
            ("asr", settings.providers.asr, settings.providers.asr_fallback),
            ("translation", settings.providers.translation, settings.providers.translation_fallback),
        )
        if (not primary or primary == "disabled") and not fallback
    ]
    fs = settings.failover
    if missing:
        return PreflightComponent(
            name="resilience",
            status="FAIL",
            detail=f"capabilities with no provider route: {missing}",
        )
    return PreflightComponent(
        name="resilience",
        status="OK",
        detail=f"failover chains loaded; circuit: "
        f"threshold={fs.failure_threshold}, cooldown_ms={fs.cooldown_ms}; "
        f"watchdog every {fs.watchdog_interval_ms}ms, "
        f"heartbeat >{fs.heartbeat_timeout_ms}ms trips (§111)",
    )


def check_security(settings: Settings) -> PreflightComponent:
    """§184/§116: production must not run with development defaults.

    Covers the visitor secret, the operator token secret, and the admin
    password (BUG-002). Never echoes secret values (BUG-005 redaction).
    """
    problems: list[str] = []
    if settings.security.visitor_secret_is_dev_default:
        problems.append("visitor_token_secret is still the development default; set BAYAAN_SECURITY__VISITOR_TOKEN_SECRET")
    if settings.auth.operator_secret_is_dev_default:
        problems.append("operator_token_secret is still the development default; set BAYAAN_AUTH__OPERATOR_TOKEN_SECRET")
    if settings.auth.password == "admin" or settings.auth.username == "admin":
        problems.append("default admin credentials are in use; set BAYAAN_AUTH__USERNAME and BAYAAN_AUTH__PASSWORD")
    if problems:
        if settings.environment == "production":
            return PreflightComponent(name="security", status="FAIL", detail="; ".join(problems))
        return PreflightComponent(name="security", status="WARN", detail="; ".join(problems))
    return PreflightComponent(name="security", status="OK", detail="secrets configured")


def check_safety_policy(settings: Settings) -> PreflightComponent:
    tts_state = "enabled" if settings.safety.tts_enabled else "locked"
    # §80/§166: show whether the TTS confidence floor is enforced or waived for
    # providers that report no confidence (operator opt-in, ADR: trust-the-chain).
    conf = (
        "floor-enforced"
        if settings.tts.require_translation_confidence
        else "waived-for-providers-without-a-score"
    )
    return PreflightComponent(
        name="safety_policy",
        status="OK",
        detail=f"tts={tts_state}, text-first (§201); tts_confidence={conf}",
    )


def check_tts_engine(settings: Settings) -> PreflightComponent:
    """§201/§194: TTS is optional. When it is enabled with the local piper
    engine, verify the binary and the per-language voice files are present so
    the operator learns at preflight (not during the first khutbah) that audio
    would be silent. A missing engine is a WARN, never a FAIL: §201 text-first
    captions still work and the room simply has no spoken audio."""
    from bayaan_server.providers.piper import PiperTTSProvider

    providers = settings.providers
    if not settings.safety.tts_enabled:
        return PreflightComponent(
            name="tts_engine", status="OK", detail="TTS locked (§201); engine not probed"
        )
    if providers.tts != "piper":
        return PreflightComponent(
            name="tts_engine",
            status="OK",
            detail=f"tts provider '{providers.tts}' in use",
        )
    engine = PiperTTSProvider(settings)
    import shutil

    exec_path = providers.piper_exec
    resolved = exec_path if Path(exec_path).is_file() else shutil.which(exec_path)
    problems: list[str] = []
    if not resolved:
        problems.append(
            f"piper binary not found: '{exec_path}' (set BAYAAN_PROVIDERS__PIPER_EXEC)"
        )
    for lang in settings.languages.target_languages:
        voice = engine.voice_for(lang)
        if not voice:
            problems.append(f"no piper voice configured for language '{lang}'")
            continue
        model, _ = engine.resolve_paths(voice)
        if not model.is_file():
            problems.append(f"piper voice '{voice}' missing: {model}")
    if problems:
        return PreflightComponent(name="tts_engine", status="WARN", detail="; ".join(problems))
    voices = ", ".join(
        f"{lang}={engine.voice_for(lang)}" for lang in settings.languages.target_languages
    )
    return PreflightComponent(
        name="tts_engine", status="OK", detail=f"piper ready ({resolved}); voices: {voices}"
    )


async def run_preflight(settings: Settings) -> PreflightReport:
    components = [
        await check_database(settings),
        await check_audio_device(settings),
        check_providers(settings),
        check_resilience(settings),
        check_safety_policy(settings),
        check_tts_engine(settings),
        check_security(settings),
    ]
    if any(c.status == "FAIL" for c in components):
        overall = "RED"
    elif any(c.status == "WARN" for c in components):
        overall = "YELLOW"
    else:
        overall = "GREEN"
    return PreflightReport(
        status=overall, environment=settings.environment, components=components
    )