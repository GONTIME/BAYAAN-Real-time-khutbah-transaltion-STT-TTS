"""PC camera relay tests (ADR-0015).

The operator's browser streams JPEG frames over an operator-authenticated
WebSocket; the server keeps only the latest frame per session and serves it to
viewers (projector / phones) as an ``multipart/x-mixed-replace`` MJPEG stream.
These tests exercise:
  1. The relay itself (latest-frame semantics, viewer cap, drop on stop/end).
  2. The camera WebSocket (auth, LIVE gate, invalid-frame closing).
  3. The MJPEG HTTP stream (visitor-token gate, real multipart framing).
  4. The video-feed kind validation + mobile flag (ADR-0015).
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi.testclient import TestClient
import pytest
from starlette.testclient import WebSocketDisconnect

from bayaan_server.camera.relay import CameraRelay
from bayaan_server.config import (
    CameraSettings,
    DatabaseSettings,
    Settings,
)

from .conftest import make_client

# A minimal but complete JPEG frame: SOI ... EOI. The server only validates
# the two magic pairs (frame payloads are opaque to the relay).
JPEG = b"\xff\xd8" + b"fake-jpeg-payload" + b"\xff\xd9"
JPEG2 = b"\xff\xd8" + b"second-frame" + b"\xff\xd9"


def _settings(tmp_path) -> Settings:
    return Settings(
        database=DatabaseSettings(
            url=f"sqlite+aiosqlite:///{tmp_path}/{uuid.uuid4()}.db"
        ),
    )


def _operator_token(client: TestClient) -> str:
    res = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _camera_ws_url(client: TestClient, session_id: str) -> str:
    return f"/ws/sessions/{session_id}/camera?token={_operator_token(client)}"


def _goto_live(client: TestClient) -> dict:
    room = client.post("/api/v1/rooms", json={}).json()
    session = client.post(
        "/api/v1/sessions",
        json={"room_id": room["id"], "active_languages": ["it", "en"]},
    ).json()
    client.post(f"/api/v1/sessions/{session['id']}/status", json={"status": "READY"})
    client.post(f"/api/v1/sessions/{session['id']}/status", json={"status": "LIVE"})
    return session


def _start_live(client: TestClient) -> dict:
    """rooms/start gives us the room code directly (needed for the MJPEG URL)."""
    start = client.post(
        "/api/v1/rooms/start", json={"active_languages": ["it"]}
    ).json()
    client.post(f"/api/v1/sessions/{start['session_id']}/status", json={"status": "READY"})
    client.post(f"/api/v1/sessions/{start['session_id']}/status", json={"status": "LIVE"})
    return start
# --------------------------------------------------------------------------- #
# 1. CameraRelay semantics (pure unit, deterministic).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_camera_relay_latest_frame_and_drop_on_stop() -> None:
    relay = CameraRelay(CameraSettings())
    sid = uuid.uuid4()

    # Nothing to serve before a publisher registers.
    assert await relay.active_sessions() == []

    await relay.start_stream(sid)
    assert await relay.write_frame(sid, JPEG) is True

    # A viewer sees the latest frame.
    seen: list[bytes] = []
    viewer = await relay.acquire_viewer(sid)
    assert viewer is not None
    async for frame in viewer:
        seen.append(frame)
        break
    assert seen == [JPEG]

    # Writing again replaces the last frame (never a backlog).
    assert await relay.write_frame(sid, JPEG2) is True
    await relay.stop_stream(sid)  # last publisher leaves -> slot dropped
    tail: list[bytes] = []
    end = await relay.acquire_viewer(sid)  # waiter creates a fresh slot
    await relay.stop_stream(sid)
    async for frame in end:
        tail.append(frame)
    assert tail == []  # stream ends once the slot is dropped


@pytest.mark.asyncio
async def test_camera_relay_viewer_cap_and_item_cap() -> None:
    relay = CameraRelay(
        CameraSettings(max_viewers_per_session=1, max_frame_bytes=8 * 1024)
    )
    sid = uuid.uuid4()
    await relay.start_stream(sid)
    first = await relay.acquire_viewer(sid)
    assert first is not None
    # Second viewer is refused while the cap is held.
    assert await relay.acquire_viewer(sid) is None
    # Oversized frame is rejected (bounded, §149).
    assert await relay.write_frame(sid, JPEG * 2000) is False
    await relay.stop_stream(sid)


@pytest.mark.asyncio
async def test_camera_viewer_wakes_immediately_on_new_frame() -> None:
    """Latency regression: a new frame must reach the viewer at once, not after
    the poll fallback. The fallback is deliberately slow (2 s); the viewer must
    still see the second frame within well under a second — the old polling
    implementation would take up to 2 s here."""
    relay = CameraRelay(CameraSettings(viewer_poll_interval_seconds=2.0))
    sid = uuid.uuid4()
    await relay.start_stream(sid)
    viewer = await relay.acquire_viewer(sid)
    assert viewer is not None

    await relay.write_frame(sid, JPEG)
    first = await asyncio.wait_for(viewer.__anext__(), timeout=1.0)
    assert first == JPEG

    await relay.write_frame(sid, JPEG2)
    second = await asyncio.wait_for(viewer.__anext__(), timeout=0.5)
    assert second == JPEG2
    await viewer.aclose()


@pytest.mark.asyncio
async def test_camera_viewer_ends_promptly_when_publisher_leaves() -> None:
    """A blocked viewer must wake and end when the last publisher leaves
    (previously it lingered until its next poll)."""
    relay = CameraRelay(CameraSettings(viewer_poll_interval_seconds=2.0))
    sid = uuid.uuid4()
    await relay.start_stream(sid)
    viewer = await relay.acquire_viewer(sid)
    assert viewer is not None
    await relay.write_frame(sid, JPEG)
    assert await asyncio.wait_for(viewer.__anext__(), timeout=1.0) == JPEG

    pending = asyncio.create_task(viewer.__anext__())
    await asyncio.sleep(0.01)  # let it block in the wait
    await relay.stop_stream(sid)  # last publisher leaves -> slot dropped
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, timeout=0.5)


@pytest.mark.asyncio
async def test_camera_viewer_ends_promptly_on_drop() -> None:
    """Same wake-up guarantee for session-end cleanup (``drop``)."""
    relay = CameraRelay(CameraSettings(viewer_poll_interval_seconds=2.0))
    sid = uuid.uuid4()
    await relay.start_stream(sid)
    viewer = await relay.acquire_viewer(sid)
    assert viewer is not None
    await relay.write_frame(sid, JPEG)
    assert await asyncio.wait_for(viewer.__anext__(), timeout=1.0) == JPEG

    pending = asyncio.create_task(viewer.__anext__())
    await asyncio.sleep(0.01)
    await relay.drop(sid)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, timeout=0.5)


# --------------------------------------------------------------------------- #
# 2. Camera WebSocket.
# --------------------------------------------------------------------------- #


def test_camera_ws_rejects_missing_operator_token(tmp_path) -> None:
    with make_client(_settings(tmp_path)) as client:
        session = _goto_live(client)
        try:
            with client.websocket_connect(
                f"/ws/sessions/{session['id']}/camera"
            ) as ws:
                ws.receive()
            raise AssertionError("expected 4401 rejection")
        except WebSocketDisconnect as exc:
            assert exc.code == 4401


def test_camera_ws_requires_live_session(tmp_path) -> None:
    with make_client(_settings(tmp_path)) as client:
        room = client.post("/api/v1/rooms", json={}).json()
        session = client.post(
            "/api/v1/sessions", json={"room_id": room["id"], "active_languages": ["it"]}
        ).json()
        try:
            with client.websocket_connect(_camera_ws_url(client, session["id"])) as ws:
                ws.receive()
            raise AssertionError("expected 4409 rejection")
        except WebSocketDisconnect as exc:
            assert exc.code == 4409


def test_camera_ws_closes_on_too_many_invalid_frames(tmp_path) -> None:
    with make_client(_settings(tmp_path)) as client:
        session = _goto_live(client)
        with client.websocket_connect(_camera_ws_url(client, session["id"])) as ws:
            for _ in range(3):  # max_invalid_frames default = 3
                ws.send_bytes(b"this-is-not-a-jpeg")
            # Starlette TestClient surfaces post-accept closes as a `websocket.close`
            # receive-frame dict (pre-accept rejections raise WebSocketDisconnect).
            msg = ws.receive()
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 4431


def test_camera_ws_closed_when_relay_disabled(tmp_path) -> None:
    settings = Settings(
        database=DatabaseSettings(
            url=f"sqlite+aiosqlite:///{tmp_path}/{uuid.uuid4()}.db"
        ),
        camera=CameraSettings(capture_enabled=False),
    )
    with make_client(settings) as client:
        session = _goto_live(client)
        try:
            with client.websocket_connect(_camera_ws_url(client, session["id"])) as ws:
                ws.receive()
            raise AssertionError("expected 4432 rejection")
        except WebSocketDisconnect as exc:
            assert exc.code == 4432
# --------------------------------------------------------------------------- #
# 3. MJPEG distribution.
# --------------------------------------------------------------------------- #


def test_camera_mjpeg_requires_visitor_token(tmp_path) -> None:
    with make_client(_settings(tmp_path)) as client:
        start = _start_live(client)
        err = client.get(f"/api/v1/rooms/{start['code']}/camera.mjpeg")
        assert err.status_code == 401


def _http_scope(app, method, path, query="", body=b"", authed=False) -> dict:
    """Scope for a direct ASGI call. ``body``/``authed`` cover the API setup
    helpers below; the MJPEG viewers are unauthenticated-to-the-server (they
    carry only the visitor token in the query string)."""
    import base64

    headers = [(b"host", b"test")]
    if authed:
        headers.append(
            (b"authorization", b"Basic " + base64.b64encode(b"admin:admin"))
        )
    if body:
        headers.append((b"content-type", b"application/json"))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("test", 80),
        "app": app,
        "state": {},
        "extensions": {"http.response.debug": {}, "http.response.trailers": {}},
    }


async def _call_json(app, method, path, payload=None, authed=True) -> dict:
    """Direct ASGI request decoding JSON responses (app runs on this loop)."""
    body = b"" if payload is None else json.dumps(payload).encode()
    result: dict = {}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            result["status_code"] = message["status"]
        elif message["type"] == "http.response.body":
            result.setdefault("body", bytearray()).extend(message.get("body", b""))

    await app(_http_scope(app, method, path, body=body, authed=authed), receive, send)
    return result


async def _setup_http_api(app, tmp_path) -> dict:
    """Create a room + get it LIVE via the API; return {code, sid, visitor}."""
    room_payload = await _call_json(app, "POST", "/api/v1/rooms", payload={})
    room = json.loads(bytes(room_payload["body"]).decode())
    session_payload = await _call_json(
        app,
        "POST",
        "/api/v1/sessions",
        payload={"room_id": room["id"], "active_languages": ["it"]},
    )
    session = json.loads(bytes(session_payload["body"]).decode())
    await _call_json(
        app, "POST", f"/api/v1/sessions/{session['id']}/status", payload={"status": "READY"}
    )
    await _call_json(
        app, "POST", f"/api/v1/sessions/{session['id']}/status", payload={"status": "LIVE"}
    )
    disc = await _call_json(app, "GET", f"/api/v1/rooms/{room['code']}", authed=False)
    info = json.loads(bytes(disc["body"]).decode())
    return {"code": room["code"], "sid": session["id"], "visitor": info["visitor_token"]}


async def _start_stream(app, url: str):
    """Drive an endless MJPEG response with a chunk-forwarding consumer.

    ``body`` is a bytearray the producer appends to as chunks arrive — the same
    incremental delivery a real HTTP server gives a client, which httpx's ASGI
    transports cannot (they drain the body to completion).
    """
    path, _, query = url.partition("?")
    body = bytearray()
    headers: dict[str, str] = {}
    status = 0
    done = asyncio.Event()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            headers.update(
                {k.decode(): v.decode() for k, v in message.get("headers", [])}
            )
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                body.extend(chunk)
            if not message.get("more_body"):
                done.set()

    task = asyncio.create_task(
        app(_http_scope(app, "GET", path, query=query, authed=False), receive, send)
    )
    return task, lambda: status, headers, body, done


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    import time as _time

    start = _time.monotonic()
    while _time.monotonic() - start < timeout:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout}s")


async def _async_setup(tmp_path, ac: httpx.AsyncClient) -> dict:
    """Create + go LIVE via the API (admin Basic auth), return the details.

    The MJPEG stream is genuinely infinite, so it cannot be exercised through
    the sync TestClient (its transport drains the full body before returning).
    These tests drive the app directly on the test loop with a real async
    client, which streams like a real ASGI server.
    """
    auth = ("admin", "admin")
    room = (await ac.post("/api/v1/rooms", auth=auth, json={})).json()
    session = (
        await ac.post(
            "/api/v1/sessions",
            auth=auth,
            json={"room_id": room["id"], "active_languages": ["it"]},
        )
    ).json()
    await ac.post(f"/api/v1/sessions/{session['id']}/status", auth=auth, json={"status": "READY"})
    await ac.post(f"/api/v1/sessions/{session['id']}/status", auth=auth, json={"status": "LIVE"})
    visitor = (await ac.get(f"/api/v1/rooms/{room['code']}")).json()["visitor_token"]
    return {"code": room["code"], "sid": session["id"], "visitor": visitor}


@pytest.mark.asyncio
async def test_camera_mjpeg_streams_latest_frame(tmp_path) -> None:
    from bayaan_server.api.app import create_app

    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        # The MJPEG response is a genuine endless stream. httpx's transports
        # (sync TestClient AND AsyncClient+ASGITransport) both drain the body to
        # completion before yielding, so they can never exercise it. Drive the
        # ASGI app directly with a chunk-forwarding consumer instead - exactly
        # what a real HTTP server does (chunks arrive as produced).
        info = await _setup_http_api(app, tmp_path)
        sid_uuid = uuid.UUID(info["sid"])
        relay = app.state.service.camera_relay
        await relay.start_stream(sid_uuid)
        await relay.write_frame(sid_uuid, JPEG)

        task, status, headers, body, done = await _start_stream(
            app, f"/api/v1/rooms/{info['code']}/camera.mjpeg?token={info['visitor']}"
        )
        try:
            await _wait_for(lambda: b"\xff\xd8" in body, timeout=5.0)
            assert status() == 200
            assert "multipart/x-mixed-replace" in headers.get(
                "content-type", ""
            )
            assert "no-store" in headers.get("cache-control", "")
            assert b"\xff\xd8" in body and b"\xff\xd9" in body
            assert b"Content-Length:" in body
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert info["sid"] not in await relay.active_sessions()


@pytest.mark.asyncio
async def test_camera_mjpeg_ends_when_session_ends(tmp_path) -> None:
    """ADR-0015: ending the session drops the slot so the stream terminates."""
    from bayaan_server.api.app import create_app

    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        info = await _setup_http_api(app, tmp_path)
        sid_uuid = uuid.UUID(info["sid"])
        relay = app.state.service.camera_relay
        await relay.start_stream(sid_uuid)
        await relay.write_frame(sid_uuid, JPEG)

        task, status, headers, body, done = await _start_stream(
            app, f"/api/v1/rooms/{info['code']}/camera.mjpeg?token={info['visitor']}"
        )
        try:
            await _wait_for(lambda: b"\xff\xd8" in body, timeout=5.0)
            # Ending the session drops the slot; viewers then end.
            await _call_json(
                app,
                "POST",
                f"/api/v1/sessions/{info['sid']}/status",
                payload={"status": "ENDED"},
            )
            assert info["sid"] not in await relay.active_sessions()
            await _wait_for(lambda: done.is_set(), timeout=5.0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_camera_mjpeg_unknown_room(tmp_path) -> None:
    from bayaan_server.security import visitor_tokens

    with make_client(_settings(tmp_path)) as client:
        tok = visitor_tokens.issue(client.app.state.service.settings.security)
        err = client.get(
            f"/api/v1/rooms/ZZZZ999/camera.mjpeg", params={"token": tok}
        )
        assert err.status_code == 404


# --------------------------------------------------------------------------- #
# 4. Video-feed kind validation + mobile flag (ADR-0015).
# --------------------------------------------------------------------------- #


def test_video_feed_kind_validation_and_mobile(tmp_path) -> None:
    with make_client(_settings(tmp_path)) as client:
        start = _start_live(client)
        sid = start["session_id"]

        # Valid kind + mobile flag round-trips through the session + discovery.
        res = client.put(
            f"/api/v1/sessions/{sid}/video-feed",
            json={"url": "http://cam.local/live.m3u8", "kind": "hls", "mobile": True},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["video_feed_kind"] == "hls"
        assert body["video_feed_mobile"] is True

        discovered = client.get(f"/api/v1/rooms/{start['code']}").json()
        assert discovered["video_feed_kind"] == "hls"
        assert discovered["video_feed_mobile"] is True

        # Every advertised kind is accepted.
        for kind in ("hls", "mp4", "webm", "mjpeg", "camera", "iframe"):
            r = client.put(
                f"/api/v1/sessions/{sid}/video-feed",
                json={"url": "http://x/feed", "kind": kind},
            )
            assert r.status_code == 200, (kind, r.text)

        # A typo'd kind is rejected up front (422), never silently mis-mapped.
        bad = client.put(
            f"/api/v1/sessions/{sid}/video-feed",
            json={"url": "http://x/feed", "kind": "banana"},
        )
        assert bad.status_code == 422


def test_session_end_removes_camera_feed(tmp_path) -> None:
    """ADR-0015: after ENDED the MJPEG feed is gone (404, terminating request).

    The relay slot drop itself is asserted loop-aligned in
    ``test_camera_mjpeg_ends_when_session_ends`` (the sync TestClient runs the
    app on a portal loop, so it cannot await relay coroutines here).
    """
    from bayaan_server.security import visitor_tokens

    with make_client(_settings(tmp_path)) as client:
        start = _start_live(client)
        sid, code = start["session_id"], start["code"]
        tok = visitor_tokens.issue(client.app.state.service.settings.security)
        client.post(f"/api/v1/sessions/{sid}/status", json={"status": "ENDED"})
        r = client.get(f"/api/v1/rooms/{code}/camera.mjpeg", params={"token": tok})
        assert r.status_code == 404


def test_security_headers_allow_same_origin_camera(tmp_path) -> None:
    """ADR-0015: camera=(self) + frame-src https/http on every response."""
    with make_client(_settings(tmp_path)) as client:
        resp = client.get("/health")
        pp = resp.headers.get("permissions-policy", "")
        assert "camera=(self)" in pp
        csp = resp.headers.get("content-security-policy", "")
        assert "frame-src 'self' https: http:" in csp