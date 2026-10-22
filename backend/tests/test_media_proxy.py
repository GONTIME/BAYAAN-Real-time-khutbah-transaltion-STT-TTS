"""Media proxy tests (ADR-0015 follow-up).

The projector/room pages load CROSS-origin HLS through the same-origin
/api/v1/media/proxy so hls.js reads can work even when the CCTV server sends
no CORS headers (and without widening connect-src in the CSP).

These tests inject a tiny async transport that streams a canned body back in
chunks. Note we deliberately cannot use ``httpx.MockTransport``: it pre-reads
the handler's response, so ``send(stream=True)`` + ``aiter_raw()`` raises
``StreamConsumed``.
"""

from __future__ import annotations

import uuid

import httpx

from bayaan_server.api.routes import media_proxy as mp
from bayaan_server.config import DatabaseSettings, Settings

M3U8 = (
    b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.0,\nseg1.ts\n"
    b"#EXT-X-ENDLIST\n"
)


class _TestStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self._data = data

    async def __aiter__(self):
        if not self._data:
            return
        yield self._data[:3]
        if len(self._data) > 3:
            yield self._data[3:]

    async def close(self) -> None:
        pass


class _StreamTransport(httpx.AsyncBaseTransport):
    """Streams a canned body without httpx pre-consuming it."""

    def __init__(
        self, status: int = 200, body: bytes = b"", content_type: str | None = None
    ):
        self._status = status
        self._body = body
        self._content_type = content_type

    async def handle_async_request(self, request):
        headers = {}
        if self._content_type:
            headers["content-type"] = self._content_type
        return httpx.Response(
            self._status,
            headers=headers,
            stream=_TestStream(self._body),
            request=request,
        )


def _settings(tmp_path) -> Settings:
    return Settings(
        database=DatabaseSettings(
            url=f"sqlite+aiosqlite:///{tmp_path}/{uuid.uuid4()}.db"
        )
    )


def _install_transport(client, transport) -> None:
    client.app.state.service.http_transport = transport


def test_media_proxy_streams_upstream_content(tmp_path) -> None:
    from .conftest import make_client

    with make_client(_settings(tmp_path)) as client:
        _install_transport(
            client,
            _StreamTransport(
                body=M3U8,
                content_type="application/vnd.apple.mpegurl",
            ),
        )
        resp = client.get(
            "/api/v1/media/proxy",
            params={"url": "http://cctv.lan/live/stream.m3u8"},
        )
        assert resp.status_code == 200
        assert resp.content == M3U8
        assert resp.headers["content-type"].startswith(
            "application/vnd.apple.mpegurl"
        )
        assert "no-store" in resp.headers.get("cache-control", "")


def test_media_proxy_rejects_non_http_schemes(tmp_path) -> None:
    from .conftest import make_client

    with make_client(_settings(tmp_path)) as client:
        _install_transport(client, _StreamTransport(body=b"never-used"))
        for bad in (
            "file:///etc/passwd",
            "javascript:alert(1)",
            "ftp://host/file",
        ):
            resp = client.get("/api/v1/media/proxy", params={"url": bad})
            assert resp.status_code == 400, bad


def test_media_proxy_caps_stream_bytes(tmp_path, monkeypatch) -> None:
    from .conftest import make_client

    monkeypatch.setattr(mp, "_MAX_STREAM_BYTES", 10)
    with make_client(_settings(tmp_path)) as client:
        _install_transport(client, _StreamTransport(body=b"x" * 1000))
        resp = client.get(
            "/api/v1/media/proxy", params={"url": "http://cctv.lan/live.ts"}
        )
        assert resp.status_code == 200
        assert resp.content == b"x" * 10  # truncated at the cap


def test_media_proxy_upstream_errors_are_502(tmp_path) -> None:
    from .conftest import make_client

    with make_client(_settings(tmp_path)) as client:
        _install_transport(client, _StreamTransport(status=404, body=b"nope"))
        resp = client.get(
            "/api/v1/media/proxy", params={"url": "http://cctv.lan/missing.m3u8"}
        )
        assert resp.status_code == 502


def test_media_proxy_missing_url_is_422(tmp_path) -> None:
    from .conftest import make_client

    with make_client(_settings(tmp_path)) as client:
        assert client.get("/api/v1/media/proxy").status_code == 422


def test_csp_keeps_connect_self_but_allows_remote_media(tmp_path) -> None:
    """connect-src stays tight (hls.js goes through the proxy); img/media-src
    allow the operator-configured remote feed hosts directly."""
    from .conftest import make_client

    with make_client(_settings(tmp_path)) as client:
        resp = client.get("/health")
        csp = resp.headers.get("content-security-policy", "")
        assert "connect-src 'self' ws: wss:" in csp
        assert "img-src 'self' data: https: http:" in csp
        assert "media-src 'self' blob: data: https: http:" in csp