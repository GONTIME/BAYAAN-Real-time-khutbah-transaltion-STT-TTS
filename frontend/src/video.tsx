/**
 * Adaptive video feed renderer (ADR-0015).
 *
 * One component for every accepted ``kind``:
 *  - hls        -> native HLS where the browser supports it (Safari),
 *                  otherwise hls.js (MSE) which makes .m3u8 feeds work on
 *                  Chrome/Edge/Firefox (the original "not all formats work"
 *                  bug: a bare <video src="...m3u8"> only ever played there).
 *  - mp4/webm   -> native <video> element.
 *  - mjpeg      -> <img> on an HTTP multipart/x-mixed-replace stream.
 *  - camera     -> <img> on /api/v1/rooms/<code>/camera.mjpeg (pc-camera relay).
 *  - iframe     -> <iframe> embed for third-party players.
 *
 * Every path surfaces an honest error + Retry button instead of the old
 * silent black box, and the element is keyed by (url + retry attempt) so a
 * retry actually forces a fresh load.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { Icon } from "./components/Icon";

export type VideoFeedKind =
  | "hls"
  | "mp4"
  | "webm"
  | "mjpeg"
  | "camera"
  | "iframe";

export const VIDEO_FEED_KINDS: readonly VideoFeedKind[] = [
  "hls",
  "mp4",
  "webm",
  "mjpeg",
  "camera",
  "iframe",
];

/** Same-origin MJPEG URL for the pc-camera relay (ADR-0015). The visitor
 *  token must ride in the query string because an <img> cannot set headers
 *  (same documented trade-off as the room WebSocket token). */
export function mjpegUrl(roomCode: string, visitorToken: string): string {
  return `/api/v1/rooms/${encodeURIComponent(roomCode)}/camera.mjpeg?token=${encodeURIComponent(visitorToken)}`;
}

/** Same-origin media proxy URL for CROSS-origin feeds (ADR-0015 follow-up).
 *  hls.js plays HLS via browser fetch of the playlist + segments; most CCTV/HLS
 *  servers send no CORS headers, so the browser refuses to read them. Loading
 *  through our own /api/v1/media/proxy makes those fetches same-origin: no CORS
 *  needed, and the strict connect-src 'self' CSP stays untouched. Same-origin
 *  URLs pass through untouched. */
export function mediaProxyUrl(u: string): string {
  if (!/^[a-z][a-z0-9+.-]*:/i.test(u)) return u; // relative / already-proxied
  const url = new URL(u);
  if (url.origin === location.origin) return u;
  return `/api/v1/media/proxy?url=${encodeURIComponent(url.href)}`;
}

function isHlsUrl(kind: VideoFeedKind, url: string): boolean {
  return kind === "hls" || /\.m3u8(\?.*)?$/i.test(url);
}

/** Attach an HLS stream (native or hls.js) to the <video> element. Also used
 *  for plain mp4/webm by falling back to a plain `src`. Always called so React
 *  hook-order stays stable even when the element is not mounted (e.g. a room
 *  that switches to the camera feed); the effect re-runs on kind/url change. */
function useHlsPlayback(
  videoRef: React.RefObject<HTMLVideoElement | null>,
  url: string,
  kind: VideoFeedKind,
  attempt: number,
  onError: () => void
): void {
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    if (!isHlsUrl(kind, url)) {
      video.src = url;
      return;
    }

    // Safari/iPhone can play HLS natively, cross-origin included (no CORS
    // requirement for the native HLS stack). Everyone else needs hls.js + MSE.
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      return;
    }

    let hls: any = null;
    let cancelled = false;
    import("hls.js")
      .then((mod) => {
        if (cancelled) return;
        const Hls = (mod as any).default ?? mod;
        if (!Hls.isSupported()) {
          onError();
          return;
        }
        hls = new Hls({
          enableWorker: true,
          liveDurationInfinity: true,
          backBufferLength: 15,
          // Non-fatal network glitches retry before we ever surface an error.
          manifestLoadingMaxRetry: 3,
          levelLoadingMaxRetry: 3,
          fragLoadingMaxRetry: 3,
          manifestLoadingRetryDelay: 500,
          levelLoadingRetryDelay: 500,
          fragLoadingRetryDelay: 500,
          // Cross-origin feeds go through the same-origin media proxy so hls.js
          // can read playlists/segments even when the CCTV server sends no CORS
          // headers. hls.js resolves every sub-resource to an ABSOLUTE url
          // first, so relative .m3u8 entries keep working.
          fetchSetup: (context: any, initParams: any) => ({
            url: mediaProxyUrl(context.url),
            init: initParams,
          }),
        });
        hls.loadSource(url);
        hls.attachMedia(video);
        hls.on(Hls.Events.ERROR, (_evt: unknown, data: any) => {
          if (data?.fatal) {
            try {
              hls?.destroy();
            } catch {
              /* already torn down */
            }
            hls = null;
            onError();
          }
        });
      })
      .catch(() => onError());

    return () => {
      cancelled = true;
      if (hls) {
        try {
          hls.destroy();
        } catch {
          /* already torn down */
        }
      }
      video.removeAttribute("src");
    };
  }, [videoRef, url, kind, attempt, onError]);
}
function VideoErrorOverlay({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="video-error-overlay">
      <span>
        <Icon name="alert" size={16} /> Video feed unavailable
      </span>
      <button
        type="button"
        className="btn btn-sm"
        onClick={onRetry}
      >
        Retry
      </button>
    </div>
  );
}

export type AdaptiveVideoProps = {
  url: string;
  kind: VideoFeedKind;
  /** Room code + visitor token — required only when kind === "camera". */
  code?: string | null;
  visitorToken?: string | null;
  className?: string;
  style?: CSSProperties;
  autoPlay?: boolean;
  muted?: boolean;
  showControls?: boolean;
};

export function AdaptiveVideo({
  url,
  kind,
  code,
  visitorToken,
  className,
  style,
  autoPlay = true,
  muted = true,
  showControls = true,
}: AdaptiveVideoProps) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  const onError = useCallback(() => setFailed(true), []);
  const retry = useCallback(() => {
    setFailed(false);
    setAttempt((a) => a + 1);
  }, []);

  // Reset the error state whenever the feed identity changes.
  useEffect(() => {
    setFailed(false);
  }, [url, kind, code]);

  // HLS/native attach — unconditional so hook order is stable.
  useHlsPlayback(videoRef, url, kind, attempt, onError);

  const wrapperStyle: CSSProperties = {
    position: "relative",
    display: "flex",
    justifyContent: "center",
    ...style,
  };

  if (kind === "mjpeg" || kind === "camera") {
    const src =
      kind === "camera"
        ? code && visitorToken
          ? mjpegUrl(code, visitorToken)
          : ""
        : url;
    if (!src) return null;
    return (
      <div className={className} style={wrapperStyle}>
        <img
          key={`${src}#${attempt}`}
          src={src}
          alt="Live video feed"
          style={{ maxWidth: "100%", maxHeight: "45vh", objectFit: "contain" }}
          onError={onError}
        />
        {failed && <VideoErrorOverlay onRetry={retry} />}
      </div>
    );
  }

  if (kind === "iframe") {
    return (
      <div className={className} style={wrapperStyle}>
        <iframe
          key={`${url}#${attempt}`}
          src={url}
          title="Live Video Feed"
          style={{ width: "100%", height: "45vh", border: "none" }}
          allow="autoplay; fullscreen"
          onError={onError}
        />
        {failed && <VideoErrorOverlay onRetry={retry} />}
      </div>
    );
  }

  return (
    <div className={className} style={wrapperStyle}>
      <video
        ref={videoRef}
        autoPlay={autoPlay}
        muted={muted}
        playsInline
        controls={showControls}
        onError={onError}
        style={{ maxWidth: "100%", maxHeight: "45vh" }}
      />
      {failed && <VideoErrorOverlay onRetry={retry} />}
    </div>
  );
}