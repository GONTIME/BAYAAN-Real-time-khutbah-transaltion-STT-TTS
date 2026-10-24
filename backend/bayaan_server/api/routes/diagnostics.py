"""Diagnostics & Provider Testing API (§195, §199).

Provides operator dashboard with real-time inspection, interactive
provider translation testing with latency measurement, and system summary.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field

from bayaan_server.db.repository import (
    AuditLogRepository,
    LiveSessionRepository,
    RoomRepository,
)
from bayaan_server.auth.admin_auth import verify_admin
from bayaan_server.domain.languages import catalog_payload, language_label
from bayaan_server.notify.telegram import TelegramDeliveryError

router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])


class TestTranslationRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    # ADR-0017: any language the admin configured is testable - the old
    # Literal["en", "it"] rejected every other configured target with a 422.
    # None means "the first configured target".
    target_language: str | None = None


class TestTranslationResponse(BaseModel):
    success: bool
    text: str
    target_language: str
    confidence: float | None
    provider: str
    model: str
    latency_ms: float
    error: str | None = None


@router.post("/test-translation", response_model=TestTranslationResponse)
async def test_translation(
    payload: TestTranslationRequest, request: Request, _=Depends(verify_admin)
) -> TestTranslationResponse:
    state = request.app.state.service
    # ADR-0017: the operator tests the languages this server is configured for.
    # Anything else is rejected with the configured list spelled out (§184)
    # instead of a generic validation error.
    configured = state.settings.languages.target_languages
    target_language = payload.target_language or configured[0]
    if target_language not in configured:
        raise HTTPException(
            status_code=422,
            detail=(
                f"target language '{target_language}' is not configured on this "
                f"server. Configured targets: {', '.join(configured)}"
            ),
        )
    t0 = time.perf_counter()
    try:
        res = await state.provider_set.translation.translate(
            payload.text, state.settings.languages.source_language, target_language
        )
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return TestTranslationResponse(
            success=True,
            text=res.text,
            target_language=target_language,
            confidence=res.confidence,
            provider=res.provider,
            model=res.model,
            latency_ms=latency_ms,
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return TestTranslationResponse(
            success=False,
            text="",
            target_language=target_language,
            confidence=None,
            provider="error",
            model="none",
            latency_ms=latency_ms,
            error=str(exc),
        )


@router.get("/system-summary")
async def system_summary(request: Request, _=Depends(verify_admin)) -> dict[str, Any]:
    state = request.app.state.service
    audio_cfg = state.settings.audio
    providers_cfg = state.settings.providers

    active_sessions_count = 0
    recent_rooms: list[dict[str, Any]] = []
    async with state.session_factory() as db:
        active_sessions = await LiveSessionRepository(db).list_active(5)
        active_sessions_count = len(active_sessions)

    provider_status = {
        capability: [
            {"provider": entry["provider"], "state": entry["state"]}
            for entry in runner.snapshot()
        ]
        for capability, runner in state.provider_set.runners.items()
    }

    return {
        "status": "ok" if (state.health.mode.value == "full") else "degraded",
        "mode": state.health.mode.value,
        "degraded_reason": state.health.degraded_reason,
        "active_sessions_count": active_sessions_count,
        "providers": provider_status,
        # ADR-0017: one place for every language picker in the operator UIs.
        # ``targets`` is what this server is configured for (what a room can
        # start with), ``catalog`` is the full curated list the admin can
        # enable in the Control Panel - so the picker can show both without
        # hard-coding codes in the frontend.
        "languages": {
            "source": state.settings.languages.source_language,
            "targets": [
                {"code": code, "label": language_label(code)}
                for code in state.settings.languages.target_languages
            ],
            "catalog": catalog_payload(),
        },
        "config": {
            "asr_provider": providers_cfg.asr,
            "translation_provider": providers_cfg.translation,
            "translation_fallback": providers_cfg.translation_fallback,
            "tts_provider": providers_cfg.tts,
            "tts_enabled": state.settings.safety.tts_enabled,
            "audio_capture_enabled": audio_cfg.capture_enabled,
            "audio_capture_mode": audio_cfg.capture_mode,
            "audio_input_device": audio_cfg.input_device,
            # ADR-0015: the operator UI hides "Start PC Camera" when the relay
            # is off on the server, instead of offering a button that cannot work.
            "camera_enabled": state.settings.camera.capture_enabled,
            # ADR-0016: token-free mirror status (enabled/active/queue depth).
            "telegram_mirror": state.notifier.configured,
        },
    }


@router.get("/safety-blocks")
async def safety_blocks(
    request: Request, limit: int = 20, _=Depends(verify_admin)
) -> dict[str, Any]:
    """Operator-only: WHY lines were held back (§98/§168, ADR-0017).

    The public room sees only the §134 fallback message, by design. The operator
    needs the opposite: an explainable reason. This reads the append-only audit
    log (never a second source of truth) so the safety gate stays exactly as
    strict as it is - it just stops being a black box for the person running the
    khutbah.
    """
    from bayaan_server.domain.enums import AuditAction

    state = request.app.state.service
    async with state.session_factory() as db:
        entries = await AuditLogRepository(db).list_recent(
            [AuditAction.SAFETY_BLOCK, AuditAction.TTS_BLOCK], limit=limit
        )
    return {"blocks": entries, "count": len(entries)}


class TestTelegramRequest(BaseModel):
    text: str = Field(
        default="Bayaan Telegram mirror test",
        min_length=1,
        max_length=1000,
    )


class TestTelegramResponse(BaseModel):
    success: bool
    configured: dict[str, Any]
    latency_ms: float
    error: str | None = None


@router.post("/test-telegram", response_model=TestTelegramResponse)
async def test_telegram(
    request: Request,
    payload: TestTelegramRequest | None = None,
    _=Depends(verify_admin),
) -> TestTelegramResponse:
    """ADR-0016: verify the Telegram mirror without going live.

    Sends one message immediately rather than queueing it: "queued" would prove
    nothing about the token, the chat id, or whether the bot is an admin in the
    target channel. The response never contains the bot token (§58) - errors are
    scrubbed inside the notifier before they get here.
    """
    state = request.app.state.service
    notifier = state.notifier
    text = payload.text if payload is not None else TestTelegramRequest().text
    t0 = time.perf_counter()
    try:
        await notifier.send_now(text)
    except TelegramDeliveryError as exc:
        return TestTelegramResponse(
            success=False,
            configured=notifier.configured,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 1),
            error=str(exc),
        )
    return TestTelegramResponse(
        success=True,
        configured=notifier.configured,
        latency_ms=round((time.perf_counter() - t0) * 1000.0, 1),
    )
