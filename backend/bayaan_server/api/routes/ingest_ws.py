"""Operator browser ingest WebSocket (ADR-0014, BAYAAN_MASTER_SPEC.md §190).

Phase 2, ``browser`` PC-input mode: the operator's browser captures the PC
microphone with ``getUserMedia`` and streams 16 kHz mono int16 PCM frames to
this channel. The server reframes the byte stream to fixed 20 ms frames and
calls ``LiveIngestService.ingest_stream_frames`` - the SAME pipeline a WAV
upload and a server-side device capture use, so a live browser stream is never
a second, weaker path (§0 rule 5).

Protocol (client -> server), all on one accepted WebSocket:
  binary frames : raw PCM16 bytes, concatenated by the server (§184: the client
                  sends exactly 16 kHz mono int16; the server never resamples)
  {"type":"stop"}: end the stream gracefully (server flushes + closes)

Trusted-operator assumption (docs/adr/0004): no admin auth in the MVP, same as
the operator console. The channel is gated on ``audio.capture_mode == "browser"``
and ``audio.ingest_ws_enabled``, and only accepts a session that exists and is
LIVE (§190, §110).

Close codes:
  4404 session not found | 4409 session not LIVE | 4429 rate limit
  4430 PC ingest disabled (browser mode off / ingest channel locked)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bayaan_server.audio.format import PcmFormat
from bayaan_server.config import Settings
from bayaan_server.db.repository import LiveSessionRepository
from bayaan_server.domain.enums import SessionStatus
from bayaan_server.security import operator_tokens

router = APIRouter(tags=["ingest-ws"])

# Bound the receive buffer so a stuck/bursting client cannot grow it without
# limit (§149 bounded resources). 20 ms mono @ 16 kHz int16 = 640 bytes/frame.
_MAX_BUFFER_BYTES = 256 * 1024


def _ws_operator_token(websocket: WebSocket) -> str:
    auth = websocket.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    try:
        params = dict(websocket.query_params)
    except Exception:
        params = {}
    return (params.get("token") or "").strip()


@router.websocket("/ws/sessions/{session_id}/ingest")
async def session_ingest_ws(websocket: WebSocket, session_id: uuid.UUID) -> None:
    state = websocket.app.state.service
    settings: Settings = state.settings
    tok = _ws_operator_token(websocket)
    if not tok or not operator_tokens.verify(settings.auth, tok):
        await websocket.close(code=4401)
        return

    # Rate-limit operator ingest connects on the same admin bucket (§115).
    address = websocket.client.host if websocket.client else "unknown"
    if not state.rate_limiter.check("admin", address):
        await websocket.close(code=4429)
        return

    # §184: browser PC input only when explicitly configured.
    audio = settings.audio
    if (
        not audio.capture_enabled
        or audio.capture_mode != "browser"
        or not audio.ingest_ws_enabled
    ):
        await websocket.close(code=4430)
        return

    fmt = PcmFormat(
        sample_rate=audio.sample_rate, channels=audio.channels
    )
    frame_bytes = fmt.frame_bytes

    async with state.session_factory() as db:
        session = await LiveSessionRepository(db).get(session_id)
    if session is None:
        await websocket.close(code=4404)
        return
    if session.status is not SessionStatus.LIVE:
        await websocket.close(code=4409)
        return

    await websocket.accept()

    async def frames():
        buffer = bytearray()
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
                buffer.extend(data)
                if len(buffer) > _MAX_BUFFER_BYTES:
                    # Drop the oldest excess rather than grow unbounded (§149);
                    # keep a whole-frame-aligned tail so timing stays honest.
                    excess = len(buffer) - _MAX_BUFFER_BYTES
                    drop = excess - (excess % frame_bytes)
                    del buffer[:drop]
                while len(buffer) >= frame_bytes:
                    yield bytes(buffer[:frame_bytes])
                    del buffer[:frame_bytes]

    try:
        await state.ingest_service.ingest_stream_frames(
            session_id, state.session_factory, frames()
        )
        # Best-effort ack so a test/operator knows the stream was fully drained.
        try:
            from starlette.websockets import WebSocketState as _WSS
            if websocket.client_state == _WSS.CONNECTED and websocket.application_state == _WSS.CONNECTED:
                await websocket.send_json({"type": "stopped"})
        except Exception:
            pass
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        try:
            from starlette.websockets import WebSocketState as _WSS2
            if websocket.client_state == _WSS2.CONNECTED and websocket.application_state == _WSS2.CONNECTED:
                await websocket.close()
        except Exception:
            pass
