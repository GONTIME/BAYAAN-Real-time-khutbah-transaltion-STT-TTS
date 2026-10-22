"""Metrics endpoint (BAYAAN_MASTER_SPEC.md §190)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from bayaan_server.auth.admin_auth import verify_admin

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics(request: Request, _=Depends(verify_admin)) -> PlainTextResponse:
    return PlainTextResponse(
        request.app.state.service.metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )