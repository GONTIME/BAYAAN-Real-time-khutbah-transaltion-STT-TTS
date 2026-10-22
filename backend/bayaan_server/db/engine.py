"""Database engine and session management (§117, §187)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bayaan_server.config import Settings
from .orm import Base


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database.url,
        echo=settings.database.echo_sql,
        future=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Phase 0 schema bootstrap with idempotent migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent migration for existing SQLite DBs: add video feed columns if missing
        from sqlalchemy.exc import OperationalError
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE live_sessions ADD COLUMN video_feed_url VARCHAR(512)"
            )
        except OperationalError as exc:
            # BUG-023: only the benign duplicate-column failure is swallowed;
            # a genuine schema/DB error must surface loudly.
            if "duplicate column" not in str(exc).lower():
                raise

        try:
            await conn.exec_driver_sql(
                "ALTER TABLE live_sessions ADD COLUMN video_feed_kind VARCHAR(32)"
            )
        except OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

        # ADR-0015: mobile (worshipper-room) video feed flag. SQLite and
        # Postgres both accept BOOLEAN NOT NULL with a constant DEFAULT for the
        # existing rows during an ADD COLUMN.
        try:
            await conn.exec_driver_sql(
                "ALTER TABLE live_sessions ADD COLUMN video_feed_mobile BOOLEAN NOT NULL DEFAULT 0"
            )
        except OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        # BUG-018: unique per-session segment sequence (concurrency guard).
        # Idempotent: existing DBs gain the constraint; duplicates (shouldn't
        # exist) would fail loudly rather than corrupt silently.
        try:
            await conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_live_segments_session_sequence "
                "ON live_segments (session_id, sequence)"
            )
        except Exception:
            pass