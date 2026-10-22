"""Preflight endpoint (BAYAAN_MASTER_SPEC.md §184, §199 preflight checklist)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from bayaan_server.auth.admin_auth import verify_admin

from bayaan_server.audio.preflight import PreflightReport, run_preflight

router = APIRouter(prefix="/api/v1", tags=["preflight"])


@router.get("/preflight", response_model=PreflightReport)
async def preflight(request: Request, _=Depends(verify_admin)) -> PreflightReport:
    state = request.app.state.service
    return await run_preflight(state.settings)