"""Security headers (BAYAAN_MASTER_SPEC.md §114).

Applied to HTTP responses; inline scripts are forbidden by CSP in the
public app. Swagger docs are exempt (they serve their own bundled assets).
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

CSP = (
    "default-src 'self'; "
    "script-src 'self' blob: 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https: http:; "
    "font-src 'self'; "
    "connect-src 'self' ws: wss:; "
    "media-src 'self' blob: data: https: http:; "
    "worker-src 'self' blob:; "
    "frame-src 'self' https: http:;"
)

# BUG-008: microphone=(self) — the app's own browser mic capture (ADR-0014,
# capture.ts getUserMedia) must work same-origin; ADR-0015 adds camera=(self)
# for the PC camera relay. geolocation stays off.
# ADR-0015 follow-up: img-src and media-src allow http/https because the whole
# point of the feed feature is playing OPERATOR-CONFIGURED remote mosque video
# (HLS/MP4/WebM/MJPEG). Media elements and <img> load cross-origin WITHOUT
# CORS, so those directives only need to permit the origin. connect-src stays
# tight ('self' ws: wss:): hls.js cannot fetch cross-origin (CORS most CCTV
# servers never send), so the frontend routes cross-origin HLS playlist/segment
# reads through the same-origin /api/v1/media/proxy (no CORS, no exfiltration
# widening). frame-src https: http: keeps third-party player embeds working.
_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(self), camera=(self)",
    "X-Frame-Options": "DENY",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        path = request.url.path
        if not path.startswith(("/docs", "/redoc", "/openapi.json")):
            response.headers.setdefault("Content-Security-Policy", CSP)
        for name, value in _HEADERS.items():
            response.headers.setdefault(name, value)
        return response