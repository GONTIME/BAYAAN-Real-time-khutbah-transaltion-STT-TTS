# ADR-0014: PC audio input (beyond the mixer-only spec)

- **Status:** Accepted
- **Date:** 2026-10-14
- **Phase:** Pilot readiness (extends ADR-0002 audio pipeline)

## Context

The master spec (`BAYAAN_MASTER_SPEC.md`) constrains audio input to the digital
mixer: *Microphone → Digital Mixer → Audio Capture → VAD/DSP → ASR …*. In real
mosques the mixer is the intended production path, but many deployments run the
BAYAAN server on an ordinary PC that is itself the audio source — there is no
separate mixer, or the operator simply wants to use the PC's own microphone to
get started.

Before this ADR the only *wired* input paths were:

- a **WAV upload** to `POST /sessions/{id}/ingest` (`ingest_pcm`), and
- the operator **"Send a test line"** (`ingest_text`, ADR-0013, no ASR).

`DeviceAudioSource` (`audio/sources.py`) existed but was **never instantiated** —
dead code — and the room WebSocket was broadcast-only (server → clients), with
no client → server ingest channel. So there was no practical way to feed live
audio from a PC into a running session without a mixer.

The single integration point for *all* input is
`LiveIngestService.ingest_pcm`, which owns the entire safety chain:
`frames → EnergyVAD → ASR → normalize → §78 route → §77 align → persist → §79
validate → §95/§167 gate → broadcast → §80 TTS`. Any new input **must** funnel
through that same machinery — never a second, weaker path (§0 rule 5).

## Decision

Add two explicit, **config-gated** PC-input paths that both call one new shared
streaming method, `LiveIngestService.ingest_stream_frames`, which reuses the
exact `_process_utterance` / `_emit_transcript` safety + translation path.

New `audio.*` settings (`AudioSettings`, all with safe defaults so behaviour is
unchanged unless explicitly enabled — §184):

| Setting | Default | Meaning |
|---|---|---|
| `audio.capture_enabled` | `false` | Master switch. OFF = mixer/WAV-only, exactly as before. |
| `audio.capture_mode` | `server_device` | Which PC path: `server_device` or `browser`. |
| `audio.ingest_ws_enabled` | `true` | Allow the browser ingest WebSocket. |

### 1. `server_device` — the server captures the PC

- New `audio/capture.py` `CaptureService` holds one capture `asyncio.Task` per
  LIVE session (bounded, §149). It opens the configured `input_device` via the
  existing `DeviceAudioSource` (`sounddevice`) and streams frames into
  `ingest_stream_frames`.
- Wired to the session lifecycle (§110) in `sessions.py`: going **LIVE** starts
  capture, **ENDED/FAILED** stops it. `stop_all()` runs on app shutdown.
- Inert unless `capture_enabled` **and** `capture_mode == "server_device"`.
- A missing `sounddevice` extra surfaces as `AudioFormatError`, logged and
  never crashing the server (honest failure, §0 rule 8). Requires
  `pip install -e ".[audio]"` (extra already declared in `pyproject.toml`).

### 2. `browser` — the operator's browser captures the PC microphone

- New route `api/routes/ingest_ws.py`: `ws/sessions/{session_id}/ingest`. The
  browser streams raw 16 kHz mono int16 PCM16 frames as binary messages; the
  server buffers, reframes to the canonical 20 ms frames and calls
  `ingest_stream_frames`. A `{"type":"stop"}` text message ends the stream
  cleanly (server flushes + acks `{"type":"stopped"}` + closes).
- Frontend `frontend/src/capture.ts` `MicCapture`: `getUserMedia` +
  `AudioContext({ sampleRate: 16000 })` (the browser resamples to the exact
  pipeline rate, so the server never resamples — §184) + an `AudioWorklet` that
  emits int16 PCM16. Exposed on `/operator` as a **"Use this PC's microphone"**
  card, shown only when the server reports browser mode via
  `StartRoomResponse.capture_enabled / capture_mode / ingest_ws_enabled`.
- Gated on `capture_mode == "browser"` and `ingest_ws_enabled`; requires the
  session to exist and be LIVE; rate-limited on the `admin` bucket (§115).
- Close codes: `4404` unknown session, `4409` not LIVE, `4429` rate limit,
  `4430` PC ingest disabled.

### Guarantees preserved

- **No weaker path (§0 rule 5):** both PC paths call `ingest_stream_frames`,
  which runs the identical VAD → ASR → normalize → route → align → persist →
  validate → safety-gate → broadcast → TTS chain as a WAV upload.
- **No silent format conversion (§184):** the browser sends exactly 16 kHz mono
  int16; the server never resamples and rejects a wrong format loudly.
- **Bounded (§149):** one capture task per session; the socket receive buffer is
  capped; per-utterance DB sessions never span a whole khutbah.
- **Operator-only ingest (BUG-001):** the ingest WebSocket requires a valid
  operator token (Bearer header, or query param for browser clients) and closes
  with `4401` otherwise. HTTP WAV ingest requires operator authentication too.
- PC audio is raw PCM with no identity (§135).

## Consequences

- A mosque with no mixer can run BAYAAN from a single PC, either with the server
  capturing the PC line/mic (`server_device`) or the operator's browser feeding
  it (`browser`), without touching the safety design.
- The default build is unchanged: `capture_enabled=false` ⇒ no PC path
  activates and the mixer/WAV spec still governs.
- `server_device` needs the `audio` extra and runs capture on the server host;
  `browser` works even when the server is remote, but needs a secure context
  (https/localhost) for `getUserMedia`.
- Tests (`tests/test_pc_input.py`) cover the shared method and the browser WS
  without requiring `sounddevice`.


### Privacy disclosure: browser transcription (BUG-022)

The dashboard's browser transcription mode uses the Web Speech API, which in
Chrome/Edge streams live microphone audio to the browser vendor's cloud
servers. This differs from the app's local-first posture (server-side ASR /
Whisper keeps audio on mosque infrastructure). Since the BUG-022 fix the UI:

- discloses this next to the audio card, and
- asks for explicit operator consent before starting browser transcription,
  defaulting to the server-side ASR path when consent is declined.
