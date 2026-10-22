"""Live session API (BAYAAN_MASTER_SPEC.md §189 "Basic API").

Phase 0 scope: create / get / list / status transition with concurrency
bound (§149) and audit trail (§98). No AI logic here.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field, model_validator

from bayaan_server.db.repository import (
    AuditLogRepository,
    LiveSegmentRepository,
    LiveSessionRepository,
)
from bayaan_server.domain.enums import AuditAction, SessionStatus
from bayaan_server.domain.models import LiveSession
from bayaan_server.domain.video import VIDEO_FEED_KINDS, validate_video_feed_kind
from bayaan_server.auth.admin_auth import verify_admin

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

_ALLOWED_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.CREATED: {SessionStatus.READY, SessionStatus.FAILED},
    SessionStatus.READY: {SessionStatus.LIVE, SessionStatus.FAILED},
    # §111: watchdog must never silently reopen a live session.
    SessionStatus.LIVE: {SessionStatus.ENDED, SessionStatus.FAILED},
    SessionStatus.ENDED: set(),
    SessionStatus.FAILED: set(),
}


class CreateSessionRequest(BaseModel):
    room_id: uuid.UUID
    sermon_version_id: Optional[uuid.UUID] = None
    active_languages: list[str] = Field(default_factory=list)


class SessionResponse(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    sermon_version_id: Optional[uuid.UUID]
    status: SessionStatus
    started_at: Optional[str]
    ended_at: Optional[str]
    active_languages: list[str]
    video_feed_url: Optional[str] = None
    video_feed_kind: Optional[str] = None
    video_feed_mobile: bool = False


class StatusTransitionRequest(BaseModel):
    status: SessionStatus


class VideoFeedRequest(BaseModel):
    url: Optional[str] = None
    kind: Optional[str] = "hls"
    # ADR-0015: also show the feed on the worshipper room page (phones).
    mobile: bool = False

    @model_validator(mode="after")
    def _kind_must_be_supported(self) -> "VideoFeedRequest":
        # ADR-0015: reject a typo'd kind up front (422) instead of letting the
        # client fall into the wrong renderer silently (§184).
        if self.kind is not None and not validate_video_feed_kind(self.kind):
            raise ValueError(
                f"unsupported video feed kind: {self.kind!r} "
                f"(supported: {', '.join(sorted(VIDEO_FEED_KINDS))})"
            )
        return self


class DemoLineRequest(BaseModel):
    """§200 operator "Send a test line" - drive a scripted utterance through the
    real pipeline without a microphone/ASR, so captions + live translation
    appear on the room page for a demo or troubleshooting. It does NOT bypass
    any safety gate: the line runs through the identical validate -> safety
    path as live audio (§0 rule 5). Requires a LIVE session (§190)."""

    arabic: str = Field(min_length=1)


def _to_response(domain: LiveSession) -> SessionResponse:
    return SessionResponse(
        id=domain.id,
        room_id=domain.room_id,
        sermon_version_id=domain.sermon_version_id,
        status=domain.status,
        started_at=domain.started_at.isoformat() if domain.started_at else None,
        ended_at=domain.ended_at.isoformat() if domain.ended_at else None,
        active_languages=domain.active_languages,
        video_feed_url=domain.video_feed_url,
        video_feed_kind=domain.video_feed_kind,
        video_feed_mobile=domain.video_feed_mobile,
    )


@router.get("/active", response_model=list[SessionResponse])
async def list_active_sessions(request: Request, _=Depends(verify_admin)) -> list[SessionResponse]:
    """§200: operator dashboard view of running sessions (READY/LIVE).

    Bounded by ``max_active_sessions`` (§149) - the same bound that caps
    creation - so this can never return an unbounded list. Registered before
    ``/{session_id}`` so the literal ``active`` path is never shadowed.
    """
    state = request.app.state.service
    async with state.session_factory() as db:
        sessions = await LiveSessionRepository(db).list_active(
            state.settings.max_active_sessions
        )
    return [_to_response(s) for s in sessions]


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    payload: CreateSessionRequest, request: Request, _=Depends(verify_admin)
) -> SessionResponse:
    state = request.app.state.service
    async with state.session_factory() as db:
        # §1.2: only PUBLISHED versions may be attached for live retrieval.
        if payload.sermon_version_id is not None:
            from bayaan_server.db.repository import SermonRepository
            from bayaan_server.domain.enums import SermonStatus

            version = await SermonRepository(db).get_version(
                payload.sermon_version_id
            )
            if version is None:
                raise HTTPException(status_code=404, detail="sermon version not found")
            if version.status is not SermonStatus.PUBLISHED:
                raise HTTPException(
                    status_code=422,
                    detail=f"sermon version must be PUBLISHED for live use "
                    f"(current: {version.status.value})",
                )
        repo = LiveSessionRepository(db)
        active = await repo.count_active()
        if active >= state.settings.max_active_sessions:
            raise HTTPException(
                status_code=409,
                detail="maximum number of concurrent active sessions reached",
            )
        session = await repo.create(
            LiveSession(
                room_id=payload.room_id,
                sermon_version_id=payload.sermon_version_id,
                active_languages=payload.active_languages,
            )
        )
        # Keep the room<->session graph consistent when the room exists;
        # sessions may still reference a bare room_id (Phase 0 behavior).
        from bayaan_server.db.repository import RoomRepository

        room_row = await RoomRepository(db).get(payload.room_id)
        if room_row is not None:
            await RoomRepository(db).link_session(payload.room_id, session.id)
        await AuditLogRepository(db).record(
            AuditAction.SESSION_STARTED, target_id=session.id
        )
        await db.commit()
    return _to_response(session)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: uuid.UUID, request: Request, _=Depends(verify_admin)) -> SessionResponse:
    state = request.app.state.service
    async with state.session_factory() as db:
        session = await LiveSessionRepository(db).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _to_response(session)


class SegmentResponse(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    sequence: int
    raw_arabic: str
    normalized_arabic: str
    content_type: str
    source_segment_id: Optional[uuid.UUID]
    asr_confidence: float
    alignment_confidence: float


@router.get("/{session_id}/segments", response_model=list[SegmentResponse])
async def list_segments(
    session_id: uuid.UUID, request: Request, _=Depends(verify_admin)
) -> list[SegmentResponse]:
    state = request.app.state.service
    async with state.session_factory() as db:
        repo = LiveSessionRepository(db)
        if await repo.get(session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        segments = await LiveSegmentRepository(db).list_for_session(session_id)
    return [
        SegmentResponse(
            id=s.id,
            session_id=s.session_id,
            sequence=s.sequence,
            raw_arabic=s.raw_arabic,
            normalized_arabic=s.normalized_arabic,
            content_type=s.content_type.value,
            source_segment_id=s.source_segment_id,
            asr_confidence=s.asr_confidence,
            alignment_confidence=s.alignment_confidence,
        )
        for s in segments
    ]


@router.post("/{session_id}/demo-line", response_model=SegmentResponse)
async def send_demo_line(
    session_id: uuid.UUID, payload: DemoLineRequest, request: Request, _=Depends(verify_admin)
) -> SegmentResponse:
    """§200: push a scripted utterance through the real pipeline for a demo or
    troubleshooting, with no microphone/ASR. The line runs through the identical
    validate -> safety-gate -> broadcast + TTS path as live audio (§0 rule 5),
    so the room page shows a real, safety-checked translation. Requires the
    session to be LIVE (409 otherwise), mirroring /ingest (§190)."""
    state = request.app.state.service
    # Same operator rate-limit bucket as the other /operator actions (§115).
    ip = request.client.host if request.client else "unknown"
    if not state.rate_limiter.check("admin", ip):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    try:
        segment = await state.ingest_service.ingest_text(
            session_id, state.session_factory, payload.arabic
        )
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail else 409
        raise HTTPException(status_code=status, detail=detail) from exc
    if segment is None:  # pragma: no cover - empty text is rejected by the model
        raise HTTPException(status_code=422, detail="nothing to translate")
    return SegmentResponse(
        id=segment.id,
        session_id=segment.session_id,
        sequence=segment.sequence,
        raw_arabic=segment.raw_arabic,
        normalized_arabic=segment.normalized_arabic,
        content_type=segment.content_type.value,
        source_segment_id=segment.source_segment_id,
        asr_confidence=segment.asr_confidence,
        alignment_confidence=segment.alignment_confidence,
    )


@router.post("/{session_id}/status", response_model=SessionResponse)
async def transition_status(
    session_id: uuid.UUID, payload: StatusTransitionRequest, request: Request, _=Depends(verify_admin)
) -> SessionResponse:
    state = request.app.state.service
    async with state.session_factory() as db:
        repo = LiveSessionRepository(db)
        current = await repo.get(session_id)
        if current is None:
            raise HTTPException(status_code=404, detail="session not found")
        allowed = _ALLOWED_TRANSITIONS.get(current.status, set())
        if payload.status not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"transition {current.status.value} -> {payload.status.value} not allowed",
            )
        updated = await repo.update_status(session_id, payload.status)
        if payload.status is SessionStatus.ENDED:
            await AuditLogRepository(db).record(
                AuditAction.SESSION_ENDED, target_id=session_id
            )
        await db.commit()
    assert updated is not None
    # ADR-0014: PC audio input follows the session lifecycle (§110). Going LIVE
    # starts server-side device capture (only when capture_mode ==
    # "server_device"); ending or failing the session stops it. No-op unless
    # audio.capture_enabled (§184).
    if payload.status is SessionStatus.LIVE:
        state.capture_service.start(
            session_id, state.ingest_service, state.session_factory
        )
    elif payload.status in (SessionStatus.ENDED, SessionStatus.FAILED):
        state.capture_service.stop(session_id)
        try:
            state.ingest_service.release_session(session_id)
        except Exception:
            pass
        # ADR-0015: ending the session drops the camera relay slot, ending any
        # in-flight MJPEG streams (the feed must not outlive the session).
        try:
            await state.camera_relay.drop(session_id)
        except Exception:
            pass
    # Broadcast lifecycle change to room subscribers (§55, §191).
    if updated.room_id is not None:
        await request.app.state.service.broker.publish(
            updated.room_id,
            {"type": "session", "status": updated.status.value},
        )
    return _to_response(updated)


@router.put("/{session_id}/video-feed", response_model=SessionResponse)
async def update_video_feed(
    session_id: uuid.UUID,
    payload: VideoFeedRequest,
    request: Request,
    _=Depends(verify_admin),
) -> SessionResponse:
    """Configure or update video stream feed URL and format kind for this session."""
    state = request.app.state.service
    async with state.session_factory() as db:
        repo = LiveSessionRepository(db)
        updated = await repo.update_video_feed(
            session_id, payload.url, payload.kind, payload.mobile
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="session not found")
        await db.commit()

    if updated.room_id is not None:
        await state.broker.publish(
            updated.room_id,
            {
                "type": "video_feed",
                "video_feed_url": updated.video_feed_url,
                "video_feed_kind": updated.video_feed_kind,
                "video_feed_mobile": updated.video_feed_mobile,
            },
        )
    return _to_response(updated)