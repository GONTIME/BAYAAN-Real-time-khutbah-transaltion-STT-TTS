"""Operator PC-camera WebSocket (ADR-0015).

The operator's browser captures its webcam with ``getUserMedia`` and streams
complete JPEG frames (binary WebSocket messages) to this channel. The server
stores only the latest frame per session in ``CameraRelay`` and distributes it
as an MJPEG stream to the projector / room page (see ``camera.py``).

Mirrors the ingest WebSocket trust model (BUG-001/020): operator token in the
Authorization header or (browser fallback) the query string, same admin rate
limit bucket, LIVE-session gate, explicit close codes.

Protocol (client -> server), one accepted WebSocket:
  binary frame : one complete JPEG image (FFD8 .. FFD9)
  {"type":"stop"} : end the stream gracefully (server releases the relay slot)

Close codes:
  4401 invalid token | 4404 session not found | 4409 session not LIVE
  4429 operator rate limit | 4431 too many consecutive invalid frames
  4432 camera relay disabled (camera.capture_enabled is false)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bayaan_server.config import Settings
from bayaan_server.db.repository import LiveSessionRepository
from bayaan_server.domain.enums import SessionStatus
from bayaan_server.security import operator_tokens

router = APIRouter(tags=["camera-ws"])


def _ws_operator_token(websocket: WebSocket) -> str:
    auth = websocket.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    try:
        params = dict(websocket.query_params)
    except Exception:  # noqa: BLE001 - malformed params means no token
        params = {}
    return (params.get("token") or "").strip()


def _is_complete_jpeg(data: bytes) -> bool:
    """Heuristic: a complete JPEG frame starts with FFD8 and ends with FFD9."""
    return len(data) >= 4 and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"


@router.websocket("/ws/sessions/{session_id}/camera")
async def session_camera_ws(websocket: WebSocket, session_id: uuid.UUID) -> None:
    state = websocket.app.state.service
    settings: Settings = state.settings
    tok = _ws_operator_token(websocket)
    if not tok or not operator_tokens.verify(settings.auth, tok):
        await websocket.close(code=4401)
        return

    # Same operator bucket as ingest/auth (§115).
    address = websocket.client.host if websocket.client else "unknown"
    if not state.rate_limiter.check("admin", address):
        await websocket.close(code=4429)
        return

    if not settings.camera.capture_enabled:
        await websocket.close(code=4432)
        return

    async with state.session_factory() as db:
        session = await LiveSessionRepository(db).get(session_id)
    if session is None:
        await websocket.close(code=4404)
        return
    if session.status is not SessionStatus.LIVE:
        await websocket.close(code=4409)
        return

    await websocket.accept()
    await state.camera_relay.start_stream(session_id)

    invalid = 0
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message["type"] != "websocket.receive":
                continue
            if text := message.get("text"):
                if '"stop"' in text:
                    break
                continue
            if data := message.get("bytes"):
                if _is_complete_jpeg(data):
                    # Oversized frames are silently dropped (bounded, §149); the
                    # client downsizes via canvas before sending.
                    await state.camera_relay.write_frame(session_id, data)
                    invalid = 0
                else:
                    invalid += 1
                    if invalid >= settings.camera.max_invalid_frames:
                        await websocket.close(code=4431)
                        return
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await state.camera_relay.stop_stream(session_id)
        except Exception:  # noqa: BLE001 - cleanup must never raise
            pass
        try:
            from starlette.websockets import WebSocketState as _WSS

            if (
                websocket.client_state == _WSS.CONNECTED
                and websocket.application_state == _WSS.CONNECTED
            ):
                await websocket.close()
        except Exception:  # noqa: BLE001 - disconnect race (BUG-012 pattern)
            pass