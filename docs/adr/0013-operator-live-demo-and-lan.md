# ADR-0013: Operator live demo (no mic) + LAN reachable QR

- **Status:** Accepted
- **Date:** 2026-10-14
- **Phase:** Pilot readiness (follows ADR-0012 operator quick-start)

## Context

During pilot setup an operator hit five concrete gaps:

1. A room created on `/operator` was not visible at `http://localhost:5173/`
   (the room page needs a code in the URL; the bare home URL is not a room).
2. "Generate drafts" produced no useful translation.
3. A room could not be opened from a phone on the same Wi-Fi (only from the
   laptop).
4. Tapping "🔊 Audio on" produced no audio, with no explanation.
5. The room page showed no captions/translation at all.

Root causes found in code:

- Captions are produced only when audio is ingested. The **only** audio input
  was a manual `POST /sessions/{id}/ingest` WAV upload; nothing fed audio into a
  running session automatically, so a browser room sat at "Waiting for the
  khutbah to start…" forever.
- TTS is **locked by default** (§201 text-first), so the server never sends
  `audio` events, and the bundled TTS provider is a mock that emits silence.
- The QR/join URL was built from `request.base_url`. Behind the Vite dev proxy
  (`changeOrigin: true`) the Host is always `127.0.0.1`, so a phone scanning the
  QR was sent to an address it cannot resolve.
- `Settings` (pydantic-settings) had **no `env_file`**, so the documented
  `backend/.env` workflow silently did nothing — provider config only applied if
  set as real environment variables.
- The room page had no way to tell whether server audio was enabled, so it
  showed a live-looking toggle that could never play.

## Decision

1. **"Send a test line" (§200).** `POST /sessions/{id}/demo-line` pushes a
   scripted Arabic utterance through the **existing** live pipeline
   (`LiveIngestService.ingest_text` → the shared `_emit_transcript`), reusing the
   identical normalize → route → translate → validate → safety-gate → broadcast +
   TTS path as real audio. It adds **no** second, weaker path and bypasses **no**
   safety gate (§0 rule 5). Requires a LIVE session (409 otherwise), mirroring
   `/ingest` (§190). Exposed as a "Send a test line" box on `/operator`.
   *This is the mic-free way to demonstrate and troubleshoot the live room; real
   microphone capture (via the optional `sounddevice` extra) remains the
   production path (§184).*
2. **Honest audio UI (§201).** The WS `hello` event now carries `tts_enabled`.
   The room page shows the audio toggle only when the server has TTS enabled;
   otherwise it shows "🔇 Audio off (server)". No dead controls.
3. **LAN-reachable QR (ADR-0013/§197).** New `BAYAAN_PUBLIC_BASE_URL` setting.
   When set, the QR code and operator join link are built from it instead of the
   request Host, so phones on the same Wi-Fi reach the app. Empty = previous
   behaviour (request base URL).
4. **`.env` actually loads.** `get_settings()` now loads `backend/.env` via
   `python-dotenv` (`override=False`, so real env vars win). Loading happens
   **only** in `get_settings()`, never in a bare `Settings(...)`, so the test
   suite stays hermetic (validated: a developer `.env` cannot perturb tests).
   A working `backend/.env` ships the recommended MT chain:
   `ollama_cloud` (gemma4:31b) primary → local `ollama` fallback (see ADR-0011).
5. **Room-join UX.** The bare home URL now says "No room selected" and links to
   `/operator`, instead of a dead "Room unavailable".

## Consequences

- An operator can start a room and **see real, safety-checked translations**
  on the room page in seconds, with no microphone and no ASR backend — the demo
  and troubleshooting path finally works end to end.
- Phones on the same Wi-Fi can open the room once `BAYAAN_PUBLIC_BASE_URL` is set
  and the servers are bound to `0.0.0.0`/`--host` (documented in
  HOW_TO_RUN_AND_USE_BAYAAN).
- Spoken audio remains **off by default** (§201 text-first). Enabling it
  (`BAYAAN_SAFETY__TTS_ENABLED=true`) is an operator opt-in; the bundled TTS is a
  **mock that emits silence** until a real voice engine lands — text captions are
  the product.
- New tests: `tests/test_operator_demo_line.py` (demo-line broadcast + LIVE/404
  guards, `PUBLIC_BASE_URL` QR/join, fallback to request base, hello
  `tts_enabled` true/false). Full suite: 251 passing.

## Alternatives considered

- **Auto-start mic capture when a session goes LIVE.** Rejected as the default:
  it needs the optional `sounddevice` extra and a configured input device (§184),
  and silently opening a microphone is worse than an explicit operator action.
  The demo-line button covers the no-hardware case; real capture stays opt-in.
- **Trusting the proxy Host for the QR.** Rejected: it always yields `127.0.0.1`
  behind the dev proxy, which phones cannot resolve.
- **Loading `.env` inside `Settings` itself.** Rejected: it would make the test
  suite non-hermetic (a developer `.env` would change provider behaviour under
  test).
