"""PC camera MJPEG distribution (ADR-0015).

Viewers (projector screen, optionally worshipper phones) render the relayed
camera as ``<img src="/api/v1/rooms/<CODE>/camera.mjpeg?token=...">``. The
server streams an ``multipart/x-mixed-replace`` body of the latest frame per
session from ``CameraRelay`` - a plain, universally-supported image stream
that needs no HLS/WebRTC machinery on the viewing device.

Security mirrors the room WebSocket (§116 visitor token, §115 rate limits):
the token travel in the query string because an ``<img>`` element cannot set
headers (same documented trade-off as the room WS token - hardening note D-1).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from bayaan_server.db.repository import LiveSessionRepository, RoomRepository
from bayaan_server.domain.enums import SessionStatus
from bayaan_server.security import operator_tokens, visitor_tokens

router = APIRouter(tags=["camera"])

_BOUNDARY = "bayaanframe"


def _jpeg_part(frame: bytes) -> bytes:
    return (
        f"--{_BOUNDARY}\r\n"
        f"Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(frame)}\r\n\r\n"
    ).encode("ascii") + frame + b"\r\n"


@router.get("/api/v1/sessions/{session_id}/camera/ready")
async def camera_ready(session_id: uuid.UUID, request: Request) -> dict:
    """ADR-0015: can this session relay a PC camera right now - and if not, why?

    The camera WebSocket rejects a relay that cannot work (4404/4409/4432), but
    the browser only learns *that* it failed, and only after ``getUserMedia`` has
    already switched the webcam on - which the operator experiences as "the
    camera starts and closes again after a second". This pre-flight answers the
    question BEFORE the webcam is touched, so the UI can show the exact reason
    instead of a mystery (§0 rule 8: never a silent assumption).

    Always HTTP 200 with an explicit verdict; the caller decides what to display.
    """
    state = request.app.state.service
    settings = state.settings

    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    else:
        # Same fallback as the camera WS: an <img>/WS client can only pass the
        # token in the query string.
        token = (request.query_params.get("token") or "").strip()
    if not token or not operator_tokens.verify(settings.auth, token):
        raise HTTPException(status_code=401, detail="invalid operator token")

    if not settings.camera.capture_enabled:
        return {
            "ready": False,
            "code": 4432,
            "reason": (
                "PC camera relay is disabled on the server "
                "(set BAYAAN_CAMERA__CAPTURE_ENABLED=true and restart the backend)."
            ),
        }

    async with state.session_factory() as db:
        session = await LiveSessionRepository(db).get(session_id)
    if session is None:
        return {
            "ready": False,
            "code": 4404,
            "reason": "Session not found - create or start a room first.",
        }
    if session.status is not SessionStatus.LIVE:
        return {
            "ready": False,
            "code": 4409,
            "reason": (
                "The room must be LIVE before the camera can be relayed "
                f"(current status: {session.status.value}). Click \"Go LIVE\" first."
            ),
        }
    return {"ready": True, "code": None, "reason": ""}


@router.get("/api/v1/rooms/{code}/camera.mjpeg")
async def camera_mjpeg(code: str, request: Request):
    state = request.app.state.service
    settings = state.settings

    if not settings.camera.capture_enabled:
        raise HTTPException(status_code=404, detail="camera relay disabled")

    address = request.client.host if request.client else "unknown"
    if not state.rate_limiter.check("ws", address):
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    token = request.query_params.get("token", "")
    if not visitor_tokens.verify(settings.security, token):
        raise HTTPException(status_code=401, detail="invalid visitor token")

    async with state.session_factory() as db:
        room = await RoomRepository(db).get_by_code(code)
        if room is None or not room.active or room.session_id is None:
            raise HTTPException(status_code=404, detail="room not found")
        session = await LiveSessionRepository(db).get(room.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if session.status in (SessionStatus.ENDED, SessionStatus.FAILED):
            raise HTTPException(status_code=404, detail="session has ended")

    stream = await state.camera_relay.acquire_viewer(room.session_id)
    if stream is None:
        raise HTTPException(
            status_code=503,
            detail="camera viewer limit reached for this session",
        )

    async def generate():
        try:
            async for frame in stream:
                yield _jpeg_part(frame)
        finally:
            await stream.aclose()

    return StreamingResponse(
        generate(),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )