# ADR-0006: Phase 5 TTS adapter, queue, gate, and audio client

**Status:** Accepted

**Context:** §194 Phase 5 adds the spoken-audio path that every earlier phase
deliberately deferred behind `safety.tts_enabled = false` (§201 text-first):
a TTS adapter, a bounded TTS queue, the §80 TTS gate, a Web Audio client, and
stale-audio dropping. The acceptance criterion is absolute: **"unsafe outputs
never reach TTS."** Because turning a mistranslation into audible speech is
the highest-consequence failure mode in the whole product (§201), the entire
phase is built so that reaching the synthesiser requires passing §80 twice.

**Decisions:**

1. **The §80 gate is a single pure chokepoint, applied twice.**
   `domain/tts.py::allow_tts` encodes §80 exactly (safety PASS, validation
   PASS, `confidence >= tts_min_confidence`, QURAN/HADITH carry an approved
   reference, `age_ms <= max_staleness_ms`, non-empty text). It runs once at
   `TtsService.enqueue_for_room` (refuse to queue) and again at dequeue inside
   `_speak_one` with the *current* age (drop stale/unsafe). Nothing calls
   `provider.synthesize` unless the second gate passes. Pure and provider-free,
   so it is unit-tested in isolation (ADR-0005 decision 1 carried forward).

2. **The queue is bounded and drops the newest, never blocks the imam.**
   `pipeline/tts_queue.py::TtsQueue` is a fixed-`maxlen` FIFO (§149: never
   unbounded). A full queue rejects the incoming job (`dropped_full`), because
   dropping the freshest line is safer than growing memory or delaying the live
   caption path. `enqueue` only appends; the async worker drains, so the live
   pipeline never awaits synthesis (§107 reliability).

3. **Stale audio is dropped on both server and client.**
   §80/§88: a job older than `max_staleness_ms` is discarded before it is
   spoken. The server drops it at dequeue; the Web Audio client
   (`frontend/src/audio.ts`) independently drops any chunk older than its own
   staleness window when scheduling. Speaking a line the imam finished seconds
   ago is worse than silence, so lateness is suppressed, not played late.

4. **TTS stays off by default; audio is a per-user opt-in.**
   §201: the single switch remains `safety.tts_enabled` (default `false`,
   surfaced by preflight). The worker only starts when it is true. On the
   client the AudioContext is created lazily on the first user gesture
   (`AudioPlayer.enable`) so the browser autoplay policy is satisfied and the
   worshipper chooses to listen. `TtsSettings` holds only operational tuning
   (staleness, queue bound, sample rate, max length) - no second on/off switch.

5. **The mock adapter emits real silent PCM, not fake speech.**
   `MockTTSProvider` returns 16-bit mono silence sized by a bounded, configured
   heuristic (`max_audio_seconds`), with `sample_rate`/`duration_ms` so the
   audio path is exercisable end-to-end without a backend. All-zero bytes are
   unmistakably silence - a mock must not pretend to speak (§0 rule 8). The
   `TtsResult` port gained `sample_rate`/`duration_ms` for playback scheduling;
   `TTSProvider.synthesize` keeps its contract that callers MUST have passed
   the gate first.

6. **Only §80-approved audio is broadcast, per language, with no identity.**
   The live pipeline builds a `TtsCandidate` per target language, sets the
   caption's `speak` flag from the §80 gate (not the looser text gate, §95),
   and enqueues only the speakable ones. The worker broadcasts an `audio` event
   carrying base64 PCM + metadata (§135: bytes only, no raw transcript, no
   identity). Clients play audio for their selected language only.

**Consequences:**
- §194 acceptance is exercised live: an approved high-confidence general
  caption is synthesized and broadcast as an `audio` event; an unverified Quran
  line and a low-confidence caption are shown as the §134 fallback with
  `speak=false` and emit **no** audio event; with TTS locked nothing is spoken.
- Full suite grows to 145 tests (28 new: §80 gate rules, queue bounding +
  stale drop, service-level spy-provider proof that unsafe output never reaches
  `synthesize`, and the end-to-end WebSocket acceptance).
- The PWA gains an opt-in "Audio on/off" toggle and a `AudioPlayer`; the build
  (`tsc && vite build`) is clean.
- Still a pilot blocker (unchanged): admin auth (§116) — `/admin` relies on
  operator network trust until it lands (ADR-0004).
