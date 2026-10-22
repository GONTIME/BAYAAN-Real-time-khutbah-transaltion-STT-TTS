/* Audio client (BAYAAN_MASTER_SPEC.md §194 Phase 5, §80, §88, §201).

   Plays the spoken-audio events the server broadcasts for §80-approved
   captions. Responsibilities that mirror the backend:

   - Only audio for the CURRENTLY selected language is played (the room
     broadcasts every target language; the worshipper hears their own).
   - Client-side STALE DROP (§88): if an audio event is older than the
     staleness window when it is about to be scheduled, it is discarded rather
     than played late - hearing an old line after the imam moved on is worse
     than silence. This is defence-in-depth on top of the server-side drop.
   - Sequential playback: chunks are scheduled back-to-back on one
     AudioContext so overlapping captions do not clobber each other.
   - Opt-in (§201 text-first): audio stays muted until the user enables it and
     a user gesture unlocks the AudioContext (browser autoplay policy).

   The AudioContext is created lazily on first enable so we never trigger the
   browser's autoplay warning before a real gesture.
*/

import type { AudioEvent } from "./ws";

// §88: drop audio older than this (ms) when it reaches the scheduler.
const STALE_MS = 8000;

function base64ToBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

// PCM 16-bit signed little-endian -> Float32 [-1, 1] for the Web Audio API.
function pcm16ToFloat32(bytes: Uint8Array): Float32Array<ArrayBuffer> {
  const samples = bytes.length >> 1;
  const out = new Float32Array(new ArrayBuffer(samples * 4));
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let i = 0; i < samples; i++) {
    out[i] = view.getInt16(i * 2, true) / 32768;
  }
  return out;
}

/**
 * §194: the bundled/mock TTS provider emits silence by design (ADR-0006: "a
 * mock must not pretend to speak"). The room client uses this to tell a real
 * provider's voiced bytes from a placeholder: when a NON-silent audio event is
 * actually scheduled, the client cancels the browser SpeechSynthesis fallback
 * for the same line so it is never double-spoken. Silent (mock) events leave
 * the fallback untouched, so audible speech keeps working with any provider.
 *
 * "Silent" here means the decoded PCM's peak sample magnitude is below ~0.1%
 * of full scale (|s| < 32/32768); real voiced audio invariably exceeds that.
 */
export function isSilentAudioEvent(event: AudioEvent): boolean {
  if (event.audio_format !== "pcm_s16le") return false;
  let peak = 0;
  try {
    const bytes = base64ToBytes(event.audio_b64);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const sampleCount = bytes.length >> 1;
    // Sample a bounded subset; PCM is a monotonic stream so a stride read
    // still catches any voiced burst while bounding cost on long buffers.
    const stride = Math.max(1, Math.floor(sampleCount / 4096));
    for (let i = 0; i < sampleCount; i += stride) {
      const a = Math.abs(view.getInt16(i * 2, true));
      if (a > peak) peak = a;
    }
  } catch {
    // Unparseable payload: treat as non-silent so we favour the real audio
    // path over the fallback rather than risking a double-speak.
    return false;
  }
  return peak < 32;
}

export class AudioPlayer {
  private ctx: AudioContext | null = null;
  private nextStartTime = 0;
  private language = "en";
  private enabled = false;
  private lastError: string | null = null;

  setLanguage(lang: string): void {
    this.language = lang;
  }

  isEnabled(): boolean {
    return this.enabled;
  }

  lastErrorMessage(): string | null {
    return this.lastError;
  }

  /** §201: user opt-in. Resumes the AudioContext after a real gesture. */
  async enable(): Promise<void> {
    this.enabled = true;
    try {
      const ctx = this.ensureContext();
      if (ctx.state === "suspended") await ctx.resume();
      this.nextStartTime = ctx.currentTime;
    } catch (err) {
      this.lastError = err instanceof Error ? err.message : String(err);
    }
  }

  disable(): void {
    this.enabled = false;
  }

  private ensureContext(): AudioContext {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      this.nextStartTime = this.ctx.currentTime;
    }
    return this.ctx;
  }

  /**
   * Handle one server audio event. Returns true if it was scheduled.
   * Ignores audio for a non-selected language, unsupported formats, and any
   * chunk that is already stale (§88).
   */
  handle(event: AudioEvent, nowMs: number): boolean {
    if (!this.enabled) return false;
    if (event.language !== this.language) return false;
    if (event.audio_format !== "pcm_s16le") {
      this.lastError = `unsupported audio format: ${event.audio_format}`;
      return false;
    }

    // §88 client-side staleness drop, BUG-010: age from the EVENT timestamp,
    // never from local receipt order. A fresh event after an idle gap plays;
    // a stale replayed event is dropped even if it arrives "now".
    if (typeof event.created_at === "string") {
      const born = Date.parse(event.created_at);
      if (!Number.isNaN(born) && nowMs - born > STALE_MS) return false;
    } else {
      // No timestamp (old server): fall back to receipt-order guard.
      if (nowMs > STALE_MS + this.acceptedAt) return false;
    }
    this.acceptedAt = nowMs;

    try {
      const ctx = this.ensureContext();
      const samples = pcm16ToFloat32(base64ToBytes(event.audio_b64));
      if (samples.length === 0) return false;
      const buffer = ctx.createBuffer(1, samples.length, event.sample_rate);
      buffer.copyToChannel(samples, 0);
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      // Schedule right after the previous chunk (or now if we fell behind).
      const start = Math.max(ctx.currentTime, this.nextStartTime);
      source.start(start);
      this.nextStartTime = start + buffer.duration;
      return true;
    } catch (err) {
      this.lastError = err instanceof Error ? err.message : String(err);
      return false;
    }
  }

  // Monotonic-ish marker of the last accepted chunk, used for the stale drop.
  private acceptedAt = 0;

  stop(): void {
    this.enabled = false;
    void this.ctx?.close();
    this.ctx = null;
  }
}
