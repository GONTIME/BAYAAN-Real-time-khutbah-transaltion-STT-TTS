"""FastAPI application factory (BAYAAN_MASTER_SPEC.md §189 Phase 0)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

import bayaan_server
from bayaan_server.audio.capture import CaptureService
from bayaan_server.config import Settings, get_settings
from bayaan_server.db.engine import create_engine, create_session_factory, init_db
from bayaan_server.logging_setup import configure_logging, get_logger
from bayaan_server.metrics import MetricsRegistry
from bayaan_server.notify.telegram import TelegramNotifier
from bayaan_server.ops.alerts import AlertBus
from bayaan_server.ops.health import ServiceHealth
from bayaan_server.ops.watchdog import WatchdogService
from bayaan_server.pipeline.live_pipeline import LiveIngestService
from bayaan_server.pipeline.tts_service import TtsService
from bayaan_server.providers.registry import build_providers
from bayaan_server.utils.ratelimit import RateLimiter
from bayaan_server.camera import CameraRelay
from bayaan_server.ws.broker import RoomBroker
from .middleware import SecurityHeadersMiddleware
from .routes import (
    auth,
    camera,
    camera_ws,
    diagnostics,
    glossary,
    health,
    ingest,
    ingest_ws,
    media_proxy,
    metrics,
    preflight,
    rooms,
    sermons,
    sessions,
    scripture,
    ws,
)

logger = get_logger(__name__)


class ServiceState:
    """Holds runtime singletons. Explicit wiring - no hidden globals (§184)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_engine(settings)
        self.session_factory = create_session_factory(self.engine)
        self.metrics = MetricsRegistry()
        # §195 Phase 6: one alert bus + one health tracker for the whole app.
        self.alerts = AlertBus(settings.alerts, self.metrics)
        self.health = ServiceHealth(settings, self.alerts, self.metrics)
        self.rate_limiter = RateLimiter(settings.rate_limit)
        self.broker = RoomBroker(settings, self.metrics)
        # ADR-0016: Telegram caption mirror. Inert unless telegram.enabled is set
        # with both a bot token and a chat id, so the default deployment is
        # unaffected (§184: no silent assumptions).
        self.notifier = TelegramNotifier(settings, self.metrics)
        # ADR-0015: latest-frame relay for the PC camera feeds. Inert unless
        # camera.capture_enabled (the camera WS closes 4432 and the MJPEG route
        # 404s), so the default deployment is unaffected.
        self.camera_relay = CameraRelay(settings.camera)
        # §87/§54: provider registry returns failover-wrapped ports + runners.
        provider_set = build_providers(settings, self.alerts, self.metrics)
        self.provider_set = provider_set
        asr, translation, tts = (
            provider_set.asr,
            provider_set.translation,
            provider_set.tts,
        )
        # §194 Phase 5: TTS service is inert unless safety.tts_enabled (§201).
        self.tts_service = TtsService(
            settings, tts, self.broker, self.metrics,
            health=self.health, alerts=self.alerts,
        )
        self.ingest_service = LiveIngestService(
            settings, asr, translation, self.metrics, self.tts_service,
            health=self.health, alerts=self.alerts, notifier=self.notifier,
        )
        self.ingest_service.set_broker(self.broker)
        # ADR-0014: PC audio input. Inert unless audio.capture_enabled and
        # capture_mode == "server_device"; started/stopped with the session
        # lifecycle by the API layer (§110).
        self.capture_service = CaptureService(settings)
        # §111 Phase 6: watchdog polls health heartbeats and soft-reboots the
        # provider layer on staleness (never reopens sessions).
        self.watchdog = WatchdogService(
            settings, self.health, self.alerts, provider_set.runners, self.metrics
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = ServiceState(settings)
        await init_db(state.engine)
        app.state.service = state
        # §111 Phase 6: watchdog runs always (poll-based; idle mode has no
        # heartbeats yet, so it cannot false-trip on a booted-but-unused box).
        state.watchdog.start()
        # §194: start the TTS worker only when TTS is enabled (§201 text-first).
        state.tts_service.start()
        # ADR-0016: start the mirror worker only when Telegram is configured.
        state.notifier.start()
        logger.info(
            "startup complete",
            extra={
                "event": "startup",
                "data": {
                    "version": bayaan_server.__version__,
                    "environment": settings.environment,
                },
            },
        )
        yield
        await state.tts_service.stop()
        await state.notifier.stop()
        await state.capture_service.stop_all()
        await state.watchdog.stop()
        await state.engine.dispose()
        logger.info("shutdown complete", extra={"event": "shutdown", "data": {}})

    app = FastAPI(
        title=settings.app_name,
        version=bayaan_server.__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(auth.router)
    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(rooms.router)
    app.include_router(sermons.router)
    app.include_router(glossary.router)
    app.include_router(scripture.router)
    app.include_router(ingest.router)
    app.include_router(ingest_ws.router)
    app.include_router(camera.router)
    app.include_router(camera_ws.router)
    app.include_router(media_proxy.router)
    app.include_router(preflight.router)
    app.include_router(metrics.router)
    app.include_router(ws.router)
    app.include_router(diagnostics.router)

    # Serve the built PWA if present (production mode; §191 public client).
    # SPA fallback: real files (assets, sw.js, manifest, icon) are served
    # directly; any other GET path (e.g. /r/<CODE>, /admin) returns
    # index.html so the client router can take over (§191, §96).
    frontend_dist = Path(__file__).resolve().parents[2].parent / "frontend" / "dist"
    if frontend_dist.is_dir():
        index_html = frontend_dist / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str, request: Request) -> FileResponse:
            if full_path.startswith("admin"):
                from bayaan_server.auth.admin_auth import verify_admin
                await verify_admin(request)
            from urllib.parse import unquote
            decoded = unquote(full_path)
            dist_root = frontend_dist.resolve()
            candidate = (frontend_dist / decoded).resolve()
            # Guard against path traversal outside the dist directory
            # (BUG-004): decoded, absolute, and relative containment check.
            try:
                candidate.relative_to(dist_root)
            except ValueError:
                if index_html.is_file():
                    return FileResponse(index_html)
                raise HTTPException(status_code=404, detail="frontend not built")
            if (
                full_path
                and candidate.is_file()
                and candidate != dist_root
            ):
                return FileResponse(candidate)
            if index_html.is_file():
                return FileResponse(index_html)
            raise HTTPException(status_code=404, detail="frontend not built")

    return app