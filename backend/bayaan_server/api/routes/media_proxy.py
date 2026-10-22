"""Same-origin media proxy for external video feeds (ADR-0015 follow-up).

Why this exists
---------------
hls.js plays HLS through browser ``fetch``/XHR reads of the .m3u8 playlist and
its .ts/.m4s segments. Cross-origin, the browser refuses to *read* those
responses unless the upstream sends CORS headers — and most mosque CCTV / HLS
servers send none. The CSP also deliberately keeps ``connect-src 'self'``.
So the projector/room pages load cross-origin HLS through THIS endpoint:
the browser fetch targets our own origin (allowed by ``connect-src 'self'``,
no CORS involved), and the server does the upstream read and streams the bytes
back unchanged.

Scope & bounds
--------------
- Only ``http``/``https`` URLs are accepted (400 otherwise); httpx never follows
  redirects to non-http schemes.
- The streamed body is capped (``_MAX_STREAM_BYTES``) so one bad URL cannot tie
  up an unbounded amount of traffic; a stuck upstream dies on the read timeout.
- Rate limited on the same viewer bucket as the room WebSocket.
- This is an explicit, operator-configured capability on a LAN app; it can
  reach any http(s) host (an SSRF-style surface) — documented in ADR-0015.

The httpx client is created OUTSIDE the streaming context manager and closed in
the generator's ``finally``; if it were created with ``async with`` the client
would shut down before the first body chunk is ever produced.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["media-proxy"])

# Cap the total streamed bytes per request (§149: bounded resources). Referenced
# at call time so tests can monkeypatch it.
_MAX_STREAM_BYTES = 512 * 1024 * 1024  # 512 MiB


def _is_allowed_url(url: str) -> bool:
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


@router.get("/api/v1/media/proxy")
async def media_proxy(url: str, request: Request) -> StreamingResponse:
    if not _is_allowed_url(url):
        raise HTTPException(
            status_code=400,
            detail="only http/https URLs are allowed",
        )

    state = request.app.state.service
    ip = request.client.host if request.client else "unknown"
    if not state.rate_limiter.check("ws", ip):
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    # Overridable in tests (MockTransport); None means real network.
    transport = getattr(state, "http_transport", None)
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers={"user-agent": "bayaan-media-proxy/0.1"},
    )
    try:
        upstream = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as exc:  # noqa: BLE001
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"upstream unavailable: {exc.__class__.__name__}",
        ) from exc

    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"upstream error: {upstream.status_code}",
        )

    content_type = upstream.headers.get("content-type") or "application/octet-stream"

    async def generate():
        sent = 0
        try:
            async for chunk in upstream.aiter_raw():
                remaining = _MAX_STREAM_BYTES - sent
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    yield chunk[:remaining]
                    break
                sent += len(chunk)
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        generate(),
        status_code=upstream.status_code,
        media_type=content_type,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )