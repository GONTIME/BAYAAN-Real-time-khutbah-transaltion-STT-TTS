"""Health check endpoint (BAYAAN_MASTER_SPEC.md §189, §112, §199).

Health is explicit and component-level: the operator preflight depends on it.
§195 Phase 6: the report also exposes the service mode (§57), the current
degradation reason, and the live provider-chain circuit states (§54/§87) so a
provider failure that was absorbed by failover is visible to the operator.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

import bayaan_server

router = APIRouter(tags=["health"])


class ProviderStatus(BaseModel):
    name: str
    state: str  # closed / half_open / open (§54)


class HealthResponse(BaseModel):
    """BUG-024: minimal public liveness payload. Mode/circuit detail is only
    served to authenticated operators via /api/v1/diagnostics/system-summary."""
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: Literal["ok", "error"]


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    state = request.app.state.service
    database: Literal["ok", "error"] = "error"
    try:
        from sqlalchemy import text

        async with state.session_factory() as db:
            await db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # noqa: BLE001 - health must never raise
        pass

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        version=bayaan_server.__version__,
        environment=state.settings.environment,
        database=database,
    )