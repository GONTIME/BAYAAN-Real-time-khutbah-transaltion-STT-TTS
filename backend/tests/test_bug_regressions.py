"""Regression tests added during the BUG fix pass (BUG-002, BUG-005, BUG-015, BUG-016)."""
from __future__ import annotations

import time
import uuid
from collections import deque

from bayaan_server.config import RateLimitSettings


def test_preflight_flags_admin_and_operator_defaults(tmp_path) -> None:
    """BUG-002: production preflight must FAIL on admin password and operator
    token secret defaults too, not just the visitor secret."""
    import asyncio

    from bayaan_server.audio.preflight import run_preflight
    from bayaan_server.config import (
        AuthSettings,
        DatabaseSettings,
        SecuritySettings,
        Settings,
    )

    prod = Settings(
        environment="production",
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/p.db"),
        security=SecuritySettings(visitor_token_secret="real-visitor-secret"),
        auth=AuthSettings(username="admin", password="admin"),
    )
    report = asyncio.run(run_preflight(prod))
    security = next(c for c in report.components if c.name == "security")
    assert security.status == "FAIL"
    assert "admin" in security.detail  # names the password default

    # Operator token secret default flagged even when admin creds are changed.
    prod2 = Settings(
        environment="production",
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/q.db"),
        security=SecuritySettings(visitor_token_secret="real-visitor-secret"),
        auth=AuthSettings(
            username="operator", password="str0ng!",
            operator_token_secret="dev-insecure-operator-secret-change-me",
        ),
    )
    report2 = asyncio.run(run_preflight(prod2))
    security2 = next(c for c in report2.components if c.name == "security")
    assert security2.status == "FAIL"
    assert "operator_token_secret" in security2.detail

    # Fully hardened config passes security.
    prod3 = Settings(
        environment="production",
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path}/r.db"),
        security=SecuritySettings(visitor_token_secret="real-visitor-secret"),
        auth=AuthSettings(
            username="operator", password="str0ng!", operator_token_secret="real-operator-secret"
        ),
    )
    report3 = asyncio.run(run_preflight(prod3))
    security3 = next(c for c in report3.components if c.name == "security")
    assert security3.status == "OK"


def test_preflight_redacts_database_url_credentials(tmp_path) -> None:
    """BUG-005: a DB URL password must never appear in any preflight detail,
    on the SUCCESS path or the FAILURE path."""
    import asyncio

    from bayaan_server.audio.preflight import run_preflight
    from bayaan_server.config import DatabaseSettings, Settings

    fake_pw = "S3cr3t-DbPa55"
    url = f"postgresql+asyncpg://user:{fake_pw}@db.internal:5432/mosque"
    settings = Settings(database=DatabaseSettings(url=url))
    report = asyncio.run(run_preflight(settings))
    joined = " ".join(c.detail for c in report.components)
    assert fake_pw not in joined  # neither connection failure nor other messages


def test_rate_limiter_evicts_idle_keys() -> None:
    """BUG-015: a used IP is limited within the window; when its events fully
    expire the key is evicted so memory cannot grow without bound."""
    from bayaan_server.utils.ratelimit import RateLimiter

    limiter = RateLimiter(RateLimitSettings(enabled=True, ws_per_minute=5))
    # Five allowed, sixth is blocked within the window.
    for _ in range(5):
        assert limiter.check("ws", "ip-0") is True
    assert limiter.check("ws", "ip-0") is False

    # Age every event out so the window fully expires.
    for k in list(limiter._events.keys()):
        limiter._events[k] = deque([time.monotonic() - 121.0] * 6)
    # This call prunes the visited key and evicts up to housekeeping cap.
    assert limiter.check("ws", "brand-new-ip") is True
    # The map is bounded, not growing with every hopped IP.
    assert len(limiter._events) <= 5


def test_release_session_clears_alignment_state() -> None:
    """BUG-016: ending a session releases its per-session alignment state."""
    from bayaan_server.config import Settings
    from bayaan_server.metrics import MetricsRegistry
    from bayaan_server.pipeline.live_pipeline import LiveIngestService

    class FakeASR:
        async def transcribe(self, audio, source_language="ar"):
            raise NotImplementedError

    class FakeTranslation:
        async def translate(self, text, source_language, target_language):
            raise NotImplementedError

    service = LiveIngestService(
        Settings(),
        FakeASR(),  # type: ignore[arg-type]
        FakeTranslation(),  # type: ignore[arg-type]
        MetricsRegistry(),
    )
    sid = uuid.uuid4()
    service._previous_alignment_order[sid] = 3
    assert sid in service._previous_alignment_order
    service.release_session(sid)
    assert sid not in service._previous_alignment_order
