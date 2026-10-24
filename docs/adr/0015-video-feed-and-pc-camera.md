# ADR-0015: Video feed formats + PC camera relay

**Date:** 2026-10-17
**Status:** Accepted
**Relates to:** BAYAAN_MASTER_SPEC.md §0 rule 5 (no weaker path), §114 (security
headers), §116 (visitor flow), §149 (bounded resources), §184 (no silent
assumptions), §191 (public client), BUG-008 (CSP), ADR-0014 (PC input).

## Context

The projector screen and, optionally, worshipper phones display a live video
feed alongside captions. Two problems existed:

1. **Format support was wrong.** The projector rendered every non-mjpeg/iframe
   feed as a single `<video src>`. HLS (`.m3u8`) — the format mosque CCTV /
   streaming boxes produce — only plays natively in Safari, so Chrome/Edge/
   Firefox showed a silent black box. The BUG-008 hardening
   (`frame-src 'self'`) also broke third-party `iframe` embeds, and the API
   accepted any `kind` string, silently mis-rendering typos.
2. **There was no PC-camera path.** The only way to get a video feed was an
   external URL (HLS/MP4/MJPEG/embed). A mosque running the app from one PC
   (the ADR-0014 scenario, extended to video) had nothing to show on the
   projector beyond captions.

## Decision

### 1. One adaptive renderer, explicit kinds

`frontend/src/video.tsx` (`AdaptiveVideo`) renders every feed the way it needs
to be rendered, with honest error + retry surfaces:

| kind     | render                                | notes |
|----------|---------------------------------------|-------|
| `hls`    | native HLS (Safari) else `hls.js` MSE | `hls.js` is dynamic-imported (separate lazy chunk; the initial bundle only grows by the small import site) |
| `mp4`    | native `<video>`                      | |
| `webm`   | native `<video>`                      | |
| `mjpeg`  | `<img>` on a `multipart/x-mixed-replace` URL | |
| `camera` | `<img>` on `/api/v1/rooms/<CODE>/camera.mjpeg` | PC camera relay (below) |
| `iframe` | `<iframe>` embed                      | |

Kinds are validated server-side (`domain/video.py::VIDEO_FEED_KINDS`); anything
else is a 422. Invalid/`fatal` streams show "Video feed unavailable" + Retry
instead of a silent black box.

### 2. PC camera relay

The operator's browser captures its webcam and the server fans the feed out —
no camera app, no codec setup, works even when the projector/phones are a
different device on the LAN Wi-Fi.

- **Capture** (`frontend/src/camera.ts`): `getUserMedia({video})` → canvas →
  JPEG (~5 f/s at 640×360, `CameraSettings`) → operator-authenticated WebSocket
  `/ws/sessions/{id}/camera` (same trust model as the audio ingest WS: Bearer
  header or query token, admin rate-limit bucket, LIVE-session gate, close codes
  4401/4404/4409/4429/4431/4432).
- **Relay** (`backend/bayaan_server/camera/relay.py`): per-session **latest
  frame only** (bounded, §149); viewer cap per session (503 beyond);
  slot dropped when the last publisher leaves or the session ends.
- **Distribution** (`api/routes/camera.py`): `GET /api/v1/rooms/{code}/camera.mjpeg`
  streams `multipart/x-mixed-replace`; visitor-token gated like the room WS;
  `Cache-Control: no-store`.
- **Phone flag**: `video_feed_mobile` (persisted on the session) opts the feed
  in to worshipper phones. Default `false` — phones stay captions-only, so the
  data cost of relaying video to a room full of phones is an explicit operator
  choice.

### 3. Security posture adjustments (documented trade-offs)

- `Permissions-Policy: camera=(self)` (was `camera=()`, which would have blocked
  the same-origin capture) — the BUG-008 mic fix, applied to the camera.
- `Content-Security-Policy frame-src`: `'self' https: http:` — restores
  `iframe` embeds. `data:`/`blob:`/`file:` frames remain blocked. Operators must
  only embed trusted player hosts; this mirrors the pre-BUG-008 posture while
  dropping the wildcard.
- The visitor token travels in the MJPEG URL query string (`<img>` can't set
  headers) — same documented trade-off as the room WS token (hardening note D-1).

## Consequences

- `.m3u8` feeds now work on all modern browsers (hls.js, MSE).
- A PC camera can reach the projector and (opt-in) phones with zero extra
  software on the mosque's side.
- The relay is in-process, like the room broker (§55): a multi-worker
  deployment must pin it to one worker (`max_active_sessions` bounds slot
  memory regardless of frame rate because only the latest frame is kept).
- Bandwidth: ~150–300 KB/s per viewer at 5 f/s 640×360 on the LAN; the phone
  flag + viewer cap keep fan-out bounded.
- `AdaptiveVideo` replaced the projector's bespoke branch; the room page
  reuses it when the mobile flag is set.

## Follow-up (same day): cross-origin HLS media proxy + camera-forward fixes

Field testing found two problems with the initial implementation:

1. **`.m3u8` links showed "Video feed unavailable" on Chrome/Edge/Firefox even
   after the hls.js renderer landed.** Two compounding causes:
   - the BUG-008-tightened CSP only allowed `connect-src 'self'` and
     `img-src`/`media-src 'self' data:`, so no *external* feed URL was readable
     at all (neither hls.js's fetches nor a direct `<img src>`/`<video src>`
     pointing at a second host);
   - and hls.js reads cross-origin resources with browser `fetch`, which
     requires the upstream CCTV server to send CORS headers — most never do.

2. **Starting the PC camera never put the feed on the projector.** The `camera`
   kind stores no URL (it is derived from the room code), but the projector and
   room page only rendered the feed when a non-null `video_feed_url` was set;
   and starting the camera did not configure the session feed at all (a hidden
   two-step flow).

### Decision (follow-up)

- **`GET /api/v1/media/proxy?url=<http(s)>`** (`api/routes/media_proxy.py`) —
  a same-origin, bounded media relay. hls.js routes *cross-origin* playlist and
  segment reads through it via `hls.js` `fetchSetup` (relative `.m3u8` entries
  keep resolving because hls.js absolutizes every sub-resource first). The
  browser fetch targets our own origin: no CORS dependency, and the strict
  `connect-src 'self' ws: wss:` CSP stays untouched. Bounds: http/https only
  (400 otherwise), capped streamed bytes (default 512 MiB), 30 s read timeout,
  viewer rate bucket, `no-store`.
- **CSP** (`api/middleware.py`) — `img-src 'self' data: https: http:` and
  `media-src 'self' blob: data: https: http:`. Media elements and `<img>` load
  cross-origin without CORS, so these directives only need to *permit* the
  origin for directly-playable feeds (MP4/WebM/MJPEG, and Safari's native HLS).
  `connect-src` stays `'self' ws: wss:` (no exfiltration widening — hls.js
  goes through the proxy).
- **Camera forward** — starting the camera now atomically configures the
  session feed (`kind=camera` + current phone-flag) so the projector shows it
  with no second "Save" step; the projector/room pages render a `camera` feed
  even though its URL is null (it derives from the room code + visitor token);
  `CameraRelayClient.start()` rejects cleanly (releasing the webcam) on the
  relay close codes instead of hanging forever.

### Tests

`backend/tests/test_media_proxy.py` (6 tests) covers the proxy using a tiny
async transport that streams a canned body (httpx.MockTransport cannot be used:
it pre-reads responses, so streaming `aiter_raw` raises `StreamConsumed`).
`backend/tests/test_camera_relay.py` (16 tests) covers the relay, the camera
upload WebSocket, real MJPEG framing via a chunk-forwarding ASGI consumer, kind
validation + mobile round-trip, session-end 404 and the CSP/Permissions-Policy
headers.

### Follow-up: camera latency fix (2026-10)

Reported as "camera is a bit lagging". Two root causes were verified in code:

1. The MJPEG viewer **polled** the relay every `viewer_poll_interval_seconds`
   (250 ms), so every frame could wait up to a quarter second on the server
   before reaching the projector/phone.
2. The browser captured at only **5 fps** via `requestAnimationFrame`, which
   browsers throttle or pause in hidden tabs — the camera froze whenever the
   operator switched windows mid-sermon.

Fixes:

- Viewers are now woken by a per-slot `asyncio.Event` the moment a frame
  lands (all state transitions — frame write, last-publisher leave, drop —
  set the event under the relay lock, so no wake-up can be lost).
  `viewer_poll_interval_seconds` is demoted to a defensive fallback timeout
  so a missed wake can never hang a stream.
- The capture cadence is timer-driven (`setInterval`, background-tab proof)
  at 10 fps (640×360, JPEG q0.6 ≈ 200–400 KB/s per viewer, still bounded by
  the viewer cap and the phone opt-in flag); an in-flight guard prevents
  overlapping `toBlob` encodes, and frames are skipped until the camera
  actually produces data (`readyState >= HAVE_CURRENT_DATA`).
- The phone-centric `facingMode: "environment"` constraint was dropped
  (this is a PC camera feature; the hint could select the wrong camera on
  multi-camera Windows devices).

Regression tests (`test_camera_relay.py`) pin sub-500 ms wake latency and
prompt stream teardown with a deliberately slow 2 s poll fallback, proving the
viewer no longer depends on polling.

### Follow-up: "the camera starts and closes after a second" (2026-10)

Reported by the operator. Verified against a real server: the relay itself is
correct — the failure is a **UX gap**, not a relay bug.

Reproduction (real WebSocket client, real server):

| State when "Start PC Camera" is pressed | Server behaviour |
|---|---|
| session `CREATED` / `READY` | camera WS **rejected before accept** (LIVE gate, 4409) |
| session `LIVE`, valid JPEG frames | stays open (15 frames / 1.5 s verified) |
| 3 non-JPEG frames | closes `4431` (as designed) |

So the sequence the operator saw was: `getUserMedia` switches the webcam on
(~1 s) → the relay refuses the non-LIVE session → the client releases the camera.
The only message the page could show was *"Camera relay connection closed"*,
because a rejection before `accept()` surfaces to the browser as close code
**1006** (abnormal closure) — the real code never reaches the page.

Fixes (honest UI, §0 rule 8):

1. **Pre-flight before touching the webcam** — new
   `GET /api/v1/sessions/{id}/camera/ready` (operator token) returns
   `{ready, code, reason}` with the same verdicts the WS enforces (4432 disabled,
   4404 unknown session, 4409 not LIVE). The dashboard calls it *before*
   `getUserMedia`, so a refusal never switches the camera on.
2. **The button mirrors the rule** — "Start PC Camera" is disabled until the
   session is LIVE, with an explicit "click **Go LIVE** first" hint, and the whole
   card is hidden when `camera.capture_enabled` is false (published as
   `config.camera_enabled` on `/diagnostics/system-summary`).
3. **Codes are translated to operator words** — `CAMERA_CLOSE_REASONS` in
   `frontend/src/camera.ts` maps 1006/4401/4404/4409/4429/4431/4432 to a
   sentence, used both for handshake rejections and for mid-stream closes.

The relay's own behaviour (LIVE gate, frame validation, close codes) is
unchanged; `tests/test_camera_ready.py` (5 tests) pins the new pre-flight.