"""Diagnostics & Provider Testing API (§195, §199).

Provides operator dashboard with real-time inspection, interactive
provider translation testing with latency measurement, and system summary.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field

from bayaan_server.db.repository import LiveSessionRepository, RoomRepository
from bayaan_server.auth.admin_auth import verify_admin

router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])


class TestTranslationRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    target_language: Literal["en", "it"] = "it"


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
    t0 = time.perf_counter()
    try:
        res = await state.provider_set.translation.translate(
            payload.text, "ar", payload.target_language
        )
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return TestTranslationResponse(
            success=True,
            text=res.text,
            target_language=payload.target_language,
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
            target_language=payload.target_language,
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
        "config": {
            "asr_provider": providers_cfg.asr,
            "translation_provider": providers_cfg.translation,
            "translation_fallback": providers_cfg.translation_fallback,
            "tts_provider": providers_cfg.tts,
            "tts_enabled": state.settings.safety.tts_enabled,
            "audio_capture_enabled": audio_cfg.capture_enabled,
            "audio_capture_mode": audio_cfg.capture_mode,
            "audio_input_device": audio_cfg.input_device,
        },
    }
