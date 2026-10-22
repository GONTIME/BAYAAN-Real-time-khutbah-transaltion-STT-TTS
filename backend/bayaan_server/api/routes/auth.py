"""Operator authentication endpoints."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bayaan_server.auth.admin_auth import verify_admin
from bayaan_server.config import Settings, get_settings
from bayaan_server.security import operator_tokens

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_hours: int
    username: str


class OperatorInfoResponse(BaseModel):
    username: str
    role: str = "operator"


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request) -> LoginResponse:
    settings: Settings = getattr(
        getattr(request.app, "state", None), "settings", None
    ) or getattr(
        getattr(getattr(request.app, "state", None), "service", None), "settings", None
    ) or get_settings()

    state = getattr(request.app.state, "service", None)
    if state is not None:
        _ip = request.client.host if request.client else "unknown"
        if not state.rate_limiter.check("admin", f"login:{_ip}"):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
            )
    _user_ok = hmac.compare_digest(req.username, settings.auth.username)
    _pass_ok = hmac.compare_digest(req.password, settings.auth.password)
    if not (_user_ok and _pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    token = operator_tokens.issue(settings.auth, req.username)
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in_hours=settings.auth.token_ttl_hours,
        username=req.username,
    )


@router.get("/me", response_model=OperatorInfoResponse)
async def get_me(is_admin: bool = Depends(verify_admin)) -> OperatorInfoResponse:
    return OperatorInfoResponse(username="admin", role="operator")
