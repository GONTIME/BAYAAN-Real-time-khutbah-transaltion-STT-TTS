"""Rooms API (BAYAAN_MASTER_SPEC.md §197 Room/QR, §115, §116).

Room lifecycle: created by the operator, discovered by worshippers via the
short code (QR prints the URL), linked to a live session, deactivated at
cleanup. Discovery issues a §116 visitor token - random, expiring, no
identity - which the WS endpoint verifies.
"""

from __future__ import annotations

import uuid

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel, Field

from bayaan_server.db.repository import (
    AuditLogRepository,
    LiveSessionRepository,
    RoomRepository,
)
from bayaan_server.domain.enums import AuditAction, SessionStatus
from bayaan_server.domain.models import LiveSession
from bayaan_server.security import visitor_tokens
from bayaan_server.utils.ratelimit import RateLimiter
from bayaan_server.auth.admin_auth import verify_admin

router = APIRouter(prefix="/api/v1/rooms", tags=["rooms"])


class CreateRoomRequest(BaseModel):
    session_id: uuid.UUID | None = None


class RoomResponse(BaseModel):
    id: uuid.UUID
    code: str
    active: bool
    session_id: uuid.UUID | None


class RoomDiscoveryResponse(BaseModel):
    room_code: str
    room_id: uuid.UUID
    session_id: uuid.UUID | None
    session_status: str | None
    languages: list[str]
    visitor_token: str
    video_feed_url: str | None = None
    video_feed_kind: str | None = None
    video_feed_mobile: bool = False


def _check_rate(request: Request, category: str) -> None:
    limiter: RateLimiter = request.app.state.service.rate_limiter
    ip = request.client.host if request.client else "unknown"
    if not limiter.check(category, ip):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


def _public_base(request: Request) -> str:
    """The base URL worshippers actually reach (ADR-0013 / §197).

    Prefers ``settings.public_base_url`` (BAYAAN_PUBLIC_BASE_URL) when set, so
    the QR code and join link work from phones on the same Wi-Fi even though the
    backend only sees the dev-proxy Host (127.0.0.1). Falls back to the incoming
    request's base URL (previous behaviour) when unset.
    """
    configured: str = request.app.state.service.settings.public_base_url.strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


@router.post("", response_model=RoomResponse, status_code=201)
async def create_room(payload: CreateRoomRequest, request: Request, _=Depends(verify_admin)) -> RoomResponse:
    _check_rate(request, "admin")
    state = request.app.state.service
    async with state.session_factory() as db:
        if payload.session_id is not None:
            session = await LiveSessionRepository(db).get(payload.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="session not found")
        room_repo = RoomRepository(db)
        room_id, code = await room_repo.create(state.settings.room.code_length)
        if payload.session_id is not None:
            # Link both directions: room.session_id and session.room_id.
            await room_repo.link_session(room_id, payload.session_id)
            await LiveSessionRepository(db).update_room(payload.session_id, room_id)
        await db.commit()
    return RoomResponse(id=room_id, code=code, active=True, session_id=payload.session_id)


class StartRoomRequest(BaseModel):
    """§200 one-click operator start. No khutbah text required.

    ``active_languages`` defaults to the configured target languages (§63)
    when omitted; any supplied value must be a subset of them (rejecting an
    unknown code rather than silently broadcasting a language nobody can
    read). The session is created with ``sermon_version_id=None`` - live
    captions use the MT provider chain, exactly like a session started
    without a prepared sermon (§0 rule 4: no new business logic).
    """

    active_languages: list[str] = Field(default_factory=list)


class StartRoomResponse(BaseModel):
    room_id: uuid.UUID
    code: str
    session_id: uuid.UUID
    status: SessionStatus
    languages: list[str]
    qr_url: str
    join_url: str
    # ADR-0014: tell the operator UI which PC-input path the server is
    # configured for, so it can offer "use this PC's microphone" only when it
    # will actually work (honest UI, §0 rule 8). capture_enabled=false or
    # capture_mode="server_device" hides the browser mic option.
    capture_enabled: bool = False
    capture_mode: str = "server_device"
    ingest_ws_enabled: bool = False


@router.post("/start", response_model=StartRoomResponse, status_code=201)
async def start_room(payload: StartRoomRequest, request: Request, _=Depends(verify_admin)) -> StartRoomResponse:
    """§200: start a live room in one step - no sermon preparation needed.

    Creates a room, a CREATED session linked to it (no sermon version),
    records the §98 audit event and returns the code + QR/join URLs so the
    operator can move READY -> LIVE immediately. Reuses the existing room,
    session, concurrency (§149) and audit machinery - adds no new rule.
    """
    _check_rate(request, "admin")
    state = request.app.state.service
    targets = state.settings.languages.target_languages
    languages = payload.active_languages or list(targets)
    unknown = [lang for lang in languages if lang not in targets]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown target language(s): {', '.join(unknown)}",
        )
    async with state.session_factory() as db:
        session_repo = LiveSessionRepository(db)
        active = await session_repo.count_active()
        if active >= state.settings.max_active_sessions:
            raise HTTPException(
                status_code=409,
                detail="maximum number of concurrent active sessions reached",
            )
        room_repo = RoomRepository(db)
        room_id, code = await room_repo.create(state.settings.room.code_length)
        session = await session_repo.create(
            LiveSession(room_id=room_id, active_languages=languages)
        )
        # Link both directions, mirroring create_room (room.session_id and
        # session.room_id already point at each other via the values above).
        await room_repo.link_session(room_id, session.id)
        await AuditLogRepository(db).record(
            AuditAction.SESSION_STARTED, target_id=session.id
        )
        await db.commit()
    base = _public_base(request)
    audio = state.settings.audio
    return StartRoomResponse(
        room_id=room_id,
        code=code,
        session_id=session.id,
        status=session.status,
        languages=languages,
        qr_url=f"{base}/api/v1/rooms/{code}/qr.svg",
        join_url=f"{base}/r/{code}",
        capture_enabled=audio.capture_enabled,
        capture_mode=audio.capture_mode,
        ingest_ws_enabled=audio.ingest_ws_enabled,
    )


@router.get("/{code}", response_model=RoomDiscoveryResponse)
async def discover_room(code: str, request: Request) -> RoomDiscoveryResponse:
    _check_rate(request, "discovery")
    state = request.app.state.service
    async with state.session_factory() as db:
        room = await RoomRepository(db).get_by_code(code)
        if room is None or not room.active:
            raise HTTPException(status_code=404, detail="room not found")
        session_status: str | None = None
        video_feed_url: str | None = None
        video_feed_kind: str | None = None
        video_feed_mobile: bool = False
        if room.session_id is not None:
            session = await LiveSessionRepository(db).get(room.session_id)
            if session:
                session_status = session.status.value
                video_feed_url = session.video_feed_url
                video_feed_kind = session.video_feed_kind
                video_feed_mobile = session.video_feed_mobile
    return RoomDiscoveryResponse(
        room_code=room.code,
        room_id=room.id,
        session_id=room.session_id,
        session_status=session_status,
        languages=state.settings.languages.target_languages,
        visitor_token=visitor_tokens.issue(state.settings.security),
        video_feed_url=video_feed_url,
        video_feed_kind=video_feed_kind,
        video_feed_mobile=video_feed_mobile,
    )


@router.get("/{code}/qr.svg")
async def room_qr(code: str, request: Request) -> Response:
    """§197: QR that encodes the public room URL."""
    _check_rate(request, "discovery")
    state = request.app.state.service
    async with state.session_factory() as db:
        room = await RoomRepository(db).get_by_code(code)
    if room is None or not room.active:
        raise HTTPException(status_code=404, detail="room not found")
    base_url = _public_base(request)
    img = qrcode.make(
        f"{base_url}/r/{room.code}", image_factory=qrcode.image.svg.SvgPathImage
    )
    return Response(
        content=img.to_string(), media_type="image/svg+xml"
    )