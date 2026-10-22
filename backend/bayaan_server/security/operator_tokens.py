"""Operator authentication tokens (BAYAAN_MASTER_SPEC.md §116, ADR-0004).

Stateless HMAC-SHA256 signed bearer tokens for the Mosque Operations Cockpit
and administration API endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from bayaan_server.config import AuthSettings


def issue(settings: AuthSettings, username: str, now: datetime | None = None) -> str:
    issued = now or datetime.now(timezone.utc)
    expires = issued + timedelta(hours=settings.token_ttl_hours)
    body = json.dumps(
        {
            "sub": username,
            "exp": expires.isoformat(),
            "iat": issued.isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    sig = hmac.new(
        settings.operator_token_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return (
        base64.urlsafe_b64encode(body).decode().rstrip("=")
        + "."
        + sig
    )


def verify(settings: AuthSettings, token: str, now: datetime | None = None) -> str | None:
    """Verify operator token. Returns username if valid, None otherwise."""
    try:
        body_b64, sig = token.split(".", 1)
        body = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
        expected = hmac.new(
            settings.operator_token_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(body)
        exp = datetime.fromisoformat(payload["exp"])
        current_time = now or datetime.now(timezone.utc)
        if current_time >= exp:
            return None
        return payload.get("sub")
    except Exception:  # noqa: BLE001 - any malformed token is invalid
        return None
