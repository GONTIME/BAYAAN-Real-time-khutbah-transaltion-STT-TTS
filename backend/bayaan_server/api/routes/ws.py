"""Room WebSocket (BAYAAN_MASTER_SPEC.md §55, §116, §188, §191).

Protocol:
  server -> client:
    {"type": "hello", room_code, session_id, status, languages, tts_enabled}
    {"type": "session", status}                    lifecycle changes
    {"type": "caption", sequence, content_type, created_at,
     arabic, targets: {<lang>: {text, state}}}     live output (§55)
    {"type": "language", lang}                     ack of client switch
  client -> server:
    {"type": "set_language", lang}
    {"type": "ping"}

Close codes (§188 reconnect semantics):
  4400 invalid language | 4401 invalid token | 4404 room unavailable
  4429 connect rate limit
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bayaan_server.db.repository import LiveSessionRepository, RoomRepository
from bayaan_server.security import visitor_tokens
from bayaan_server.ws.broker import Connection

router = APIRouter(tags=["ws"])


@router.websocket("/ws/rooms/{code}")
async def room_ws(websocket: WebSocket, code: str, lang: str = "ar", token: str = "") -> None:
    state = websocket.app.state.service
    settings = state.settings

    address = websocket.client.host if websocket.client else "unknown"
    if not state.rate_limiter.check("ws", address):
        await websocket.close(code=4429)
        return

    async with state.session_factory() as db:
        room = await RoomRepository(db).get_by_code(code)
        if room is None or not room.active:
            await websocket.close(code=4404)
            return
        session = None
        if room.session_id is not None:
            session = await LiveSessionRepository(db).get(room.session_id)

    if not visitor_tokens.verify(settings.security, token):
        await websocket.close(code=4401)
        return

    allowed = {"ar", *settings.languages.target_languages}
    if lang not in allowed:
        await websocket.close(code=4400)
        return

    await websocket.accept()
    conn = Connection(
        websocket=websocket,
        language=lang,
        address=address,
        session_id=room.session_id,
    )
    state.broker.subscribe(room.id, conn)
    try:
        await websocket.send_json(
            {
                "type": "hello",
                "room_code": room.code,
                "session_id": str(room.session_id) if room.session_id else None,
                "status": session.status.value if session else None,
                "languages": settings.languages.target_languages,
                # §201: tell the client whether the server will emit spoken
                # audio at all, so the room page can show "Audio disabled on
                # the server" instead of a dead toggle (honest UI, §0 rule 8).
                "tts_enabled": settings.safety.tts_enabled,
            }
        )
        for event in state.broker.history(room.id):
            await websocket.send_json(event)
        # §80/§88: replay recent spoken audio too, so a phone that connects or
        # re-connects mid-line still hears the current line (the room client
        # applies its 8s staleness drop, so only fresh audio actually sounds).
        for event in state.broker.recent_audio(room.id):
            await websocket.send_json(event)

        while True:
            message = await websocket.receive_json()
            match message.get("type"):
                case "set_language":
                    requested = str(message.get("lang", ""))
                    if requested in allowed:
                        conn.language = requested
                        await websocket.send_json(
                            {"type": "language", "lang": requested}
                        )
                case "ping":
                    await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        state.broker.unsubscribe(room.id, conn)