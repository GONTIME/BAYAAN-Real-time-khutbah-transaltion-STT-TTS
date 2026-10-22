"""Video feed configuration (ADR-0015).

Single source of truth for the accepted ``kind`` values of a session video
feed. The backend rejects anything else with 422 so a typo can never silently
fall into the wrong renderer on the client (§184: explicit, no silent
assumptions).
"""

from __future__ import annotations

# hls     Apple HLS (.m3u8)             rendered with hls.js MSE by the frontend
# mp4     progressive MP4/video file    native <video>
# webm    WebM/video file               native <video>
# mjpeg   MJPEG image stream            <img> on an HTTP multipart stream
# camera  PC camera relay (ADR-0015)    <img> on /api/v1/rooms/<code>/camera.mjpeg
# iframe  third-party embed             <iframe>
VIDEO_FEED_KINDS = frozenset({"hls", "mp4", "webm", "mjpeg", "camera", "iframe"})

# Output formats produced by the export service (§ of ADR-0015).
VIDEO_FEED_LABELS: dict[str, str] = {
    "hls": "HLS (.m3u8)",
    "mp4": "MP4",
    "webm": "WebM",
    "mjpeg": "MJPEG stream",
    "camera": "PC camera (relay)",
    "iframe": "Embed",
}


def validate_video_feed_kind(kind: str | None) -> bool:
    """True when ``kind`` is None (clear the feed) or one of the known kinds."""
    return kind is None or kind in VIDEO_FEED_KINDS