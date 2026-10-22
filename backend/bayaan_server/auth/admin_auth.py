from __future__ import annotations

import base64
import hmac
from fastapi import Request, HTTPException, status
from bayaan_server.config import Settings, get_settings
from bayaan_server.security import operator_tokens


async def verify_admin(request: Request) -> bool:
    """Verify administrator access via Bearer operator token or HTTP Basic credentials.

    Accepts:
    1. Authorization: Bearer <operator_token>
    2. Authorization: Basic <base64(user:pass)>
    """
    settings: Settings = getattr(
        getattr(request.app, "state", None), "settings", None
    ) or getattr(
        getattr(getattr(request.app, "state", None), "service", None), "settings", None
    ) or get_settings()

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed authorization header",
            headers={"WWW-Authenticate": "Basic"},
        )

    scheme, token = parts[0].lower(), parts[1].strip()

    if scheme == "bearer":
        username = operator_tokens.verify(settings.auth, token)
        if username:
            return True
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired operator token",
        )

    if scheme == "basic":
        try:
            decoded = base64.b64decode(token).decode("utf-8")
            username, password = decoded.split(":", 1)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid basic auth encoding",
                headers={"WWW-Authenticate": "Basic"},
            )

        if hmac.compare_digest(username, settings.auth.username) and hmac.compare_digest(password, settings.auth.password):
            return True

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin credentials",
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unsupported authentication scheme",
        headers={"WWW-Authenticate": "Basic"},
    )
