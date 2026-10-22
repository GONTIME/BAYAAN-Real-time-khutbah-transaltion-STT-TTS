"""PC audio input tests (ADR-0014, BAYAAN_MASTER_SPEC.md §190, §0 rule 5).

Both PC-input paths must feed the SAME pipeline as a WAV upload - there is
never a second, weaker path. These tests exercise:

  1. ``ingest_stream_frames`` directly (the shared streaming entry point), and
  2. the operator browser ingest WebSocket (``/ws/sessions/{id}/ingest``) in
     ``browser`` capture mode.

The server-side ``server_device`` path only differs in where frames come from
(``DeviceAudioSource`` vs the socket); it is covered by the shared method plus
``CaptureService`` config-gating tests. No test needs ``sounddevice``.
"""

from __future__ import annotations

import asyncio
import struct
import uuid

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDisconnect

from bayaan_server.audio.capture import CaptureService
from bayaan_server.config import (
    AudioSettings,
    DatabaseSettings,
    ProviderSettings,
    Settings,
)

from .conftest import make_client

SR = 16000
FRAME = 640  # 20 ms mono @ 16 kHz int16


def _pcm_frames(segments: list[tuple[str, float]]) -> list[bytes]:
    """Build 20 ms int16 PCM frames from (kind, seconds) parts."""
    blobs: list[bytes] = []
    for kind, seconds in segments:
        n = int(SR * seconds)
        if kind == "silence":
            blobs.append(b"\x00\x00" * n)
        else:  # loud constant amplitude (RMS well above the VAD threshold)
            blobs.append(b"".join(struct.pack("<h", 3000) for _ in range(n)))
    # Split into exact 20 ms frames the VAD/pipeline expects.
    out: list[bytes] = []
    for blob in blobs:
        for off in range(0, len(blob) - FRAME + 1, FRAME):
            out.append(blob[off : off + FRAME])
    return out


async def _frame_iter(blobs: list[bytes]):
    for b in blobs:
        yield b


def _pc_settings(tmp_path, mode: str = "browser") -> Settings:
    return Settings(
        database=DatabaseSettings(
            url=f"sqlite+aiosqlite:///{tmp_path}/{uuid.uuid4()}.db"
        ),
        providers=ProviderSettings(
            mock_asr_text="مرحبا بكم في مسجدنا",
            mock_asr_confidence=0.99,
            mock_translation_confidence=0.99,
        ),
        audio=AudioSettings(
            capture_enabled=True, capture_mode=mode, ingest_ws_enabled=True
        ),
    )


def _goto_live(client: TestClient) -> dict:
    room = client.post("/api/v1/rooms", json={}).json()
    session = client.post(
        "/api/v1/sessions",
        json={"room_id": room["id"], "active_languages": ["it", "en"]},
    ).json()
    client.post(f"/api/v1/sessions/{session['id']}/status", json={"status": "READY"})
    client.post(f"/api/v1/sessions/{session['id']}/status", json={"status": "LIVE"})
    return session

def _run(coro):
    return asyncio.run(coro)


def _ws_url(client: TestClient, session_id: str) -> str:
    """Build an authed ingest-WS URL (BUG-001): login -> operator token -> query param."""
    res = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert res.status_code == 200, res.text
    tok = res.json()["access_token"]
    return f"/ws/sessions/{session_id}/ingest?token={tok}"




# --------------------------------------------------------------------------- #
# 1. Shared streaming entry point (used by BOTH PC-input paths).
# --------------------------------------------------------------------------- #


def test_ingest_stream_frames_persists_segment(tmp_path) -> None:
    """ingest_stream_frames runs the full pipeline for streamed PCM (§190)."""
    with make_client(_pc_settings(tmp_path)) as client:
        session = _goto_live(client)
        state = client.app.state.service
        frames = _pcm_frames(
            [("silence", 0.5), ("speech", 0.6), ("silence", 1.0)]
        )
        _run(
            state.ingest_service.ingest_stream_frames(
                uuid.UUID(session["id"]),
                state.session_factory,
                _frame_iter(frames),
            )
        )
        listed = client.get(
            f"/api/v1/sessions/{session['id']}/segments"
        ).json()
        assert len(listed) == 1
        assert listed[0]["sequence"] == 0
        assert listed[0]["raw_arabic"] == "مرحبا بكم في مسجدنا"


def test_ingest_stream_frames_sequence_continues(tmp_path) -> None:
    """A second stream continues segment numbering (§190 stream continuity)."""
    with make_client(_pc_settings(tmp_path)) as client:
        session = _goto_live(client)
        state = client.app.state.service

        async def run(frames):
            await state.ingest_service.ingest_stream_frames(
                uuid.UUID(session["id"]),
                state.session_factory,
                _frame_iter(frames),
            )

        _run(
            run(_pcm_frames([("silence", 0.4), ("speech", 0.5), ("silence", 1.0)]))
        )
        _run(
            run(_pcm_frames([("silence", 0.4), ("speech", 0.5), ("silence", 1.0)]))
        )
        listed = client.get(
            f"/api/v1/sessions/{session['id']}/segments"
        ).json()
        assert [s["sequence"] for s in listed] == [0, 1]


def test_stream_ingest_keeps_consuming_during_slow_translation(tmp_path) -> None:
    """§216: a slow utterance (ASR + translation) must not stall frame receipt.

    Regression: ``ingest_stream_frames`` used to await the whole per-utterance
    pipeline inside the frame loop, so the live WebSocket was not drained while
    a chunk was being translated - voice spoken during that window was lost.
    The producer/consumer split keeps frames flowing while the first utterance
    is still being processed, and both utterances still persist, in order.
    """
    with make_client(_pc_settings(tmp_path)) as client:
        session = _goto_live(client)
        state = client.app.state.service

        # Slow down the pipeline for the first utterance only, and record how
        # many frames were consumed at the start/end of that slow window.
        original = state.ingest_service._process_utterance
        slow = {"done": False, "consumed_start": 0, "consumed_end": 0}
        consumed: list[int] = []

        async def slow_first_utterance(*args, **kwargs):
            if not slow["done"]:
                slow["done"] = True
                slow["consumed_start"] = len(consumed)
                await asyncio.sleep(0.5)
                slow["consumed_end"] = len(consumed)
            return await original(*args, **kwargs)

        async def paced_frames(blobs):
            # ~real-time delivery so "frames consumed" tracks stream draining.
            for idx, blob in enumerate(blobs):
                consumed.append(idx)
                yield blob
                await asyncio.sleep(0.02)

        state.ingest_service._process_utterance = slow_first_utterance
        try:
            frames = _pcm_frames(
                [
                    ("silence", 0.3),  # VAD pre-speech padding
                    ("speech", 0.5),   # utterance 1 (the slow one)
                    ("silence", 0.8),  # closes utterance 1, rest keeps flowing
                    ("speech", 0.5),   # utterance 2 arrives mid-translate
                    ("silence", 1.2),  # closes utterance 2 / end of stream
                ]
            )

            async def run():
                await state.ingest_service.ingest_stream_frames(
                    uuid.UUID(session["id"]),
                    state.session_factory,
                    paced_frames(frames),
                )

            _run(run())
            assert slow["consumed_end"] > slow["consumed_start"], (
                "the receive loop stalled while the first utterance was being "
                "translated (live voice during that window would be lost)"
            )
        finally:
            state.ingest_service._process_utterance = original

        listed = client.get(
            f"/api/v1/sessions/{session['id']}/segments"
        ).json()
        assert len(listed) == 2
        assert [s["sequence"] for s in listed] == [0, 1]


# --------------------------------------------------------------------------- #
# 2. Browser ingest WebSocket (browser capture mode).
# --------------------------------------------------------------------------- #


def test_ingest_ws_persists_segment(tmp_path) -> None:
    """Binary PCM frames over the ingest WS become a persisted segment."""
    with make_client(_pc_settings(tmp_path, mode="browser")) as client:
        session = _goto_live(client)
        with client.websocket_connect(_ws_url(client, session['id'])) as ws:
            for frame in _pcm_frames(
                [("silence", 0.4), ("speech", 0.6), ("silence", 1.0)]
            ):
                ws.send_bytes(frame)
            ws.send_text('{"type":"stop"}')
            # Server acks once the stream is fully drained (best-effort: the
            # TestClient may race close vs ack, so accept either).
            try:
                assert ws.receive_json() == {"type": "stopped"}
            except WebSocketDisconnect:
                pass
        listed = client.get(
            f"/api/v1/sessions/{session['id']}/segments"
        ).json()
        assert len(listed) == 1


def test_ingest_ws_rejects_non_live_session(tmp_path) -> None:
    """A session that is not LIVE is refused with close code 4409 (§110)."""
    with make_client(_pc_settings(tmp_path, mode="browser")) as client:
        room = client.post("/api/v1/rooms", json={}).json()
        session = client.post(
            "/api/v1/sessions",
            json={"room_id": room["id"], "active_languages": ["it"]},
        ).json()
        try:
            with client.websocket_connect(_ws_url(client, session['id'])) as ws:
                ws.receive()
            raise AssertionError("expected rejection")
        except WebSocketDisconnect as exc:
            assert exc.code == 4409


def test_ingest_ws_rejects_unknown_session(tmp_path) -> None:
    """An unknown session id is refused with close code 4404."""
    with make_client(_pc_settings(tmp_path, mode="browser")) as client:
        try:
            with client.websocket_connect(f"/ws/sessions/{uuid.uuid4()}/ingest?token=" + client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"}).json()["access_token"]) as ws:
                ws.receive()
            raise AssertionError("expected rejection")
        except WebSocketDisconnect as exc:
            assert exc.code == 4404


def test_ingest_ws_closed_when_not_browser_mode(tmp_path) -> None:
    """When the server is not in browser mode the channel is locked (4430)."""
    # capture_enabled True but mode is server_device -> browser ingest locked.
    with make_client(_pc_settings(tmp_path, mode="server_device")) as client:
        session = _goto_live(client)
        try:
            with client.websocket_connect(_ws_url(client, session['id'])) as ws:
                ws.receive()
            raise AssertionError("expected the ingest channel to be locked")
        except WebSocketDisconnect as exc:
            assert exc.code == 4430


def test_ingest_ws_closed_when_capture_disabled(tmp_path) -> None:
    """With capture disabled entirely the channel is locked (4430, §184)."""
    settings = Settings(
        database=DatabaseSettings(
            url=f"sqlite+aiosqlite:///{tmp_path}/{uuid.uuid4()}.db"
        ),
        providers=ProviderSettings(mock_translation_confidence=0.99),
        audio=AudioSettings(capture_enabled=False, capture_mode="browser"),
    )
    with make_client(settings) as client:
        session = _goto_live(client)
        try:
            with client.websocket_connect(_ws_url(client, session['id'])) as ws:
                ws.receive()
            raise AssertionError("expected the ingest channel to be locked")
        except WebSocketDisconnect as exc:
            assert exc.code == 4430


# --------------------------------------------------------------------------- #
# 3. CaptureService config gating (server_device path wiring, no sounddevice).
# --------------------------------------------------------------------------- #


def test_capture_service_disabled_is_noop(tmp_path) -> None:
    """capture_enabled=false -> start() never launches a capture task (§184)."""
    settings = Settings(
        database=DatabaseSettings(
            url=f"sqlite+aiosqlite:///{tmp_path}/{uuid.uuid4()}.db"
        ),
        audio=AudioSettings(capture_enabled=False, capture_mode="server_device"),
    )
    service = CaptureService(settings)
    service.start(uuid.uuid4(), ingest_service=None, session_factory=None)
    assert service.active_sessions == set()


def test_capture_service_browser_mode_does_not_capture(tmp_path) -> None:
    """In browser mode the server-side capture must NOT auto-start (§184)."""
    settings = Settings(
        database=DatabaseSettings(
            url=f"sqlite+aiosqlite:///{tmp_path}/{uuid.uuid4()}.db"
        ),
        audio=AudioSettings(capture_enabled=True, capture_mode="browser"),
    )
    service = CaptureService(settings)
    service.start(uuid.uuid4(), ingest_service=None, session_factory=None)
    assert service.active_sessions == set()


def test_capture_service_stop_is_idempotent(tmp_path) -> None:
    """Stopping a session that is not capturing is a safe no-op."""
    settings = Settings(
        database=DatabaseSettings(
            url=f"sqlite+aiosqlite:///{tmp_path}/{uuid.uuid4()}.db"
        ),
        audio=AudioSettings(capture_enabled=True, capture_mode="server_device"),
    )
    service = CaptureService(settings)
    service.stop(uuid.uuid4())  # never started
    assert service.active_sessions == set()



def test_ingest_ws_rejects_missing_operator_token(tmp_path) -> None:
    """BUG-001: ingest WS without operator credentials is rejected (4401)."""
    from .conftest import make_client as _mk
    with _mk(_pc_settings(tmp_path, mode="browser")) as client:
        session = _goto_live(client)
        try:
            with client.websocket_connect(f"/ws/sessions/{session['id']}/ingest") as ws:
                ws.receive()
            raise AssertionError("expected 4401 rejection")
        except WebSocketDisconnect as exc:
            assert exc.code == 4401
