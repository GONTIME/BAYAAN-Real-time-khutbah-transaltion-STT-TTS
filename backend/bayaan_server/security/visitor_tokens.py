"""Visitor tokens (BAYAAN_MASTER_SPEC.md §116).

For worshippers: random session token, expiration, no identity. Issued at
room discovery, verified at WebSocket connect. HMAC-signed and stateless;
contains no personal data.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
from datetime import datetime, timedelta, timezone

from bayaan_server.config import SecuritySettings


def issue(settings: SecuritySettings, now: datetime | None = None) -> str:
    issued = now or datetime.now(timezone.utc)
    expires = issued + timedelta(minutes=settings.visitor_token_ttl_minutes)
    body = json.dumps(
        {"exp": expires.isoformat()}, separators=(",", ":"), sort_keys=True
    ).encode()
    sig = hmac.new(
        settings.visitor_token_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return (
        base64.urlsafe_b64encode(body).decode().rstrip("=")
        + "."
        + sig
    )


def verify(settings: SecuritySettings, token: str, now: datetime | None = None) -> bool:
    try:
        body_b64, sig = token.split(".", 1)
        body = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
        expected = hmac.new(
            settings.visitor_token_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        payload = json.loads(body)
        exp = datetime.fromisoformat(payload["exp"])
        return (now or datetime.now(timezone.utc)) < exp
    except Exception:  # noqa: BLE001 - any malformed token is invalid
        return False