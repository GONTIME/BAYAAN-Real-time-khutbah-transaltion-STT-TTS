/* PC audio input from the browser (ADR-0014, BAYAAN_MASTER_SPEC.md §190).

   Lets the operator use THIS PC's microphone as the live khutbah source when
   the server is configured for ``browser`` capture mode. The captured audio is
   resampled by the browser to 16 kHz mono (``AudioContext({sampleRate:16000})``
   requests the exact rate the pipeline expects, §184 - the server never
   resamples), converted to signed 16-bit little-endian PCM by an AudioWorklet,
   and streamed as raw binary frames over the operator ingest WebSocket
   ``/ws/sessions/{id}/ingest``. The server reframes the bytes and runs the SAME
   safety/translation pipeline as any other input (§0 rule 5 - no weaker path).

   Requirements mirrored from the browser platform:
   - a secure context (https or localhost) is required for getUserMedia (§194);
   - a user gesture (the operator pressing "Start") is required both for mic
     permission and to satisfy autoplay policy.
   Failures (permission denied, no secure context, no worklet support) surface
   as an ``error`` string - honest, never faked (§0 rule 8).
*/

// PCM worklet: converts the mic's Float32 samples to int16 LE frames and posts
// them to the main thread. Defined as a string so it needs no separate file
// (loaded via a Blob URL), keeping the module self-contained.
const WORKLET_SRC = `
class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length) {
      const f32 = input[0];
      const pcm = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) {
        const s = Math.max(-1, Math.min(1, f32[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm.buffer);
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCaptureProcessor);
`;

function ingestWsUrl(sessionId: string, token?: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const base = `${proto}://${location.host}/ws/sessions/${sessionId}/ingest`;
  // BUG-001/020: operator ingest requires a token. Browsers cannot set WS
  // headers, so the token travels as a query param on this same-origin
  // operator-only channel (wss in production). Server also accepts headers.
  if (token) return `${base}?token=${encodeURIComponent(token)}`;
  return base;
}

export class MicCapture {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private worklet: AudioWorkletNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private socket: WebSocket | null = null;
  private running = false;

  isRunning(): boolean {
    return this.running;
  }

  private onLevelCallback: ((rms: number) => void) | null = null;

  setLevelCallback(cb: ((rms: number) => void) | null): void {
    this.onLevelCallback = cb;
  }

  /** Capture the PC mic and stream PCM frames to the session ingest channel. */
  async start(
    sessionId: string,
    sampleRate = 16000,
    onLevel?: ((rms: number) => void) | string,
    token?: string
  ): Promise<void> {
    if (this.running) return;
    // Allow start(sessionId, 16000, "<token>") overload.
    let levelCb: ((rms: number) => void) | undefined;
    let authToken = token;
    if (typeof onLevel === "string") {
      authToken = onLevel;
    } else if (onLevel) {
      levelCb = onLevel;
    }
    if (levelCb) this.onLevelCallback = levelCb;
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error(
        "Microphone capture needs a secure context (https or localhost)."
      );
    }

    // 1. Ask the browser for exactly the pipeline's rate (§184: no resampling).
    const ctx = new AudioContext({ sampleRate });
    this.ctx = ctx;

    // 2. Open the microphone (must follow the operator's click gesture).
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });

    // 3. Load the PCM worklet.
    const blob = new Blob([WORKLET_SRC], { type: "application/javascript" });
    const url = URL.createObjectURL(blob);
    try {
      await ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    this.worklet = new AudioWorkletNode(ctx, "pcm-capture");
    this.source = ctx.createMediaStreamSource(this.stream);
    this.source.connect(this.worklet);
    // NOTE: worklet is NOT connected to ctx.destination - capture only, no
    // monitoring feedback loop into the mic.

    // 4. Open the operator ingest WebSocket and wait until it is OPEN before
    //    capturing, so no audio is dropped before the channel is ready.
    await this.openSocket(sessionId, authToken);

    this.running = true;
    this.worklet.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
      if (this.socket && this.socket.readyState === WebSocket.OPEN) {
        this.socket.send(event.data);
      }
      if (this.onLevelCallback && event.data.byteLength > 0) {
        const samples = new Int16Array(event.data);
        let sum = 0;
        for (let i = 0; i < samples.length; i++) {
          const s = samples[i] / 32768;
          sum += s * s;
        }
        const rms = Math.min(1, Math.sqrt(sum / samples.length) * 3);
        this.onLevelCallback(rms);
      }
    };
  }

  /**
   * Open the mic for VU-meter level feedback ONLY — no WebSocket is opened
   * and no audio is sent to the server.  Use this when the browser's own
   * Speech Recognition API handles transcription so the server ASR path
   * must stay silent.
   */
  async startLevelOnly(
    sampleRate = 16000,
    onLevel?: (rms: number) => void
  ): Promise<void> {
    if (this.running) return;
    if (onLevel) this.onLevelCallback = onLevel;
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error(
        "Microphone access needs a secure context (https or localhost)."
      );
    }

    const ctx = new AudioContext({ sampleRate });
    this.ctx = ctx;

    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });

    const blob = new Blob([WORKLET_SRC], { type: "application/javascript" });
    const url = URL.createObjectURL(blob);
    try {
      await ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    this.worklet = new AudioWorkletNode(ctx, "pcm-capture");
    this.source = ctx.createMediaStreamSource(this.stream);
    this.source.connect(this.worklet);

    // No socket — level-only mode.
    this.running = true;
    this.worklet.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
      if (this.onLevelCallback && event.data.byteLength > 0) {
        const samples = new Int16Array(event.data);
        let sum = 0;
        for (let i = 0; i < samples.length; i++) {
          const s = samples[i] / 32768;
          sum += s * s;
        }
        const rms = Math.min(1, Math.sqrt(sum / samples.length) * 3);
        this.onLevelCallback(rms);
      }
    };
  }

  private openSocket(sessionId: string, token?: string): Promise<void> {
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(ingestWsUrl(sessionId, token));
      socket.binaryType = "arraybuffer";
      this.socket = socket;
      socket.onopen = () => resolve();
      socket.onerror = () =>
        reject(new Error("Could not open the audio stream to the server."));
      socket.onclose = () => {
        if (this.running) this.stop();
      };
    });
  }

  /** Stop capture and release the mic + audio resources. */
  async stop(): Promise<void> {
    this.running = false;
    if (this.worklet) this.worklet.port.onmessage = null;
    try {
      if (this.socket && this.socket.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify({ type: "stop" }));
      }
    } catch {
      /* socket may already be closed */
    }
    this.socket?.close();
    this.socket = null;
    this.source?.disconnect();
    this.worklet?.disconnect();
    this.source = null;
    this.worklet = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    await this.ctx?.close().catch(() => undefined);
    this.ctx = null;
  }
}

/**
 * Live Arabic speech recognition in the browser using the Web Speech API.
 * Transcribes the speaker's Arabic voice in real-time with zero backend latency.
 */
export class LiveArabicRecognizer {
  private recognition: any = null;
  private running = false;
  private onTranscriptCallback: ((text: string, isFinal: boolean) => void) | null = null;
  private onErrorCallback: ((err: string) => void) | null = null;
  private lastFinalText = "";
  private restartTimer: number | null = null;
  /** Consecutive failed restart attempts since the last successful capture
      session. The Web Speech API ends its session after every final chunk, so
      we must restart eagerly; back off when the browser refuses to start, and
      reset on the next successful `onstart`. Previously a fixed 250ms single
      retry left a ~0.5s blind window after every transcribed chunk (voice
      spoken during that window was lost) and a throwing start() killed
      recognition silently until the operator restarted the mic. */
  private restartAttempts = 0;
  private readonly restartDelaysMs = [100, 250, 500, 1000];

  static isSupported(): boolean {
    return (
      typeof window !== "undefined" &&
      ("webkitSpeechRecognition" in window || "SpeechRecognition" in window)
    );
  }

  start(
    onTranscript: (text: string, isFinal: boolean) => void,
    onError?: (err: string) => void,
    lang = "ar-SA"
  ): void {
    if (this.running) return;
    const SpeechRec =
      (window as any).SpeechRecognition ||
      (window as any).webkitSpeechRecognition;

    if (!SpeechRec) {
      onError?.(
        "Browser Speech Recognition is not supported. Please use Google Chrome or Microsoft Edge."
      );
      return;
    }

    try {
      this.lastFinalText = "";
      const rec = new SpeechRec();
      rec.continuous = true;
      rec.interimResults = true;
      rec.lang = lang;

      this.onTranscriptCallback = onTranscript;
      this.onErrorCallback = onError || null;

      rec.onresult = (event: any) => {
        let interim = "";
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const item = event.results[i];
          const transcript = item[0]?.transcript?.trim();
          if (!transcript) continue;

          if (item.isFinal) {
            // Deduplicate: avoid firing duplicate final events
            const normalized = transcript.replace(/\s+/g, " ");
            if (normalized && normalized !== this.lastFinalText) {
              this.lastFinalText = normalized;
              this.onTranscriptCallback?.(normalized, true);
            }
          } else {
            interim += transcript + " ";
          }
        }
        if (interim.trim()) {
          this.onTranscriptCallback?.(interim.trim(), false);
        }
      };

      rec.onerror = (e: any) => {
        // "aborted" and "no-speech" are benign lifecycle events the browser
        // fires around a session end; they are handled by the eager restart.
        if (e.error !== "no-speech" && e.error !== "aborted") {
          this.onErrorCallback?.(`Speech recognition error: ${e.error}`);
        }
      };

      rec.onstart = () => {
        // The service accepted the new session: restore the fast restart path
        // (100ms) so the next chunk boundary re-arms with minimal dead air.
        this.restartAttempts = 0;
      };

      rec.onend = () => {
        // The Web Speech API ends its session after every final chunk; a slow
        // single retry here is exactly the "voice is lost during the chunk
        // being sent" window. Restart eagerly with backoff instead.
        if (this.running) {
          this.scheduleRestart();
        }
      };

      this.recognition = rec;
      this.running = true;
      rec.start();
    } catch (err) {
      onError?.(`Could not start speech recognition: ${String(err)}`);
    }
  }

  stop(): void {
    this.running = false;
    this.lastFinalText = "";
    if (this.restartTimer) {
      window.clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    if (this.recognition) {
      try {
        this.recognition.stop();
      } catch {
        /* ignore */
      }
      this.recognition = null;
    }
  }

  /** Re-arm capture after the browser ends its recognition session (which it
      does after every final chunk). Back off across attempts so a browser that
      is not ready yet does not starve the restart with hot retries. */
  private scheduleRestart(): void {
    if (this.restartTimer) window.clearTimeout(this.restartTimer);
    const attempt = Math.min(
      this.restartAttempts,
      this.restartDelaysMs.length - 1
    );
    this.restartTimer = window.setTimeout(() => {
      this.restartTimer = null;
      this.tryRestart();
    }, this.restartDelaysMs[attempt]);
  }

  private tryRestart(): void {
    if (!this.running || !this.recognition) return;
    try {
      // A throwing start() (e.g. InvalidStateError when the browser is not
      // ready) used to kill recognition silently. Retry with backoff instead,
      // and surface it once so the operator sees a warning, not a dead mic.
      this.recognition.start();
    } catch {
      this.restartAttempts += 1;
      if (this.restartAttempts === this.restartDelaysMs.length) {
        this.onErrorCallback?.(
          `Speech recognition is failing to restart (${this.restartAttempts} ` +
            "attempts) — check the microphone and network, then stop/start the mic."
        );
      }
      this.scheduleRestart();
    }
  }

  isRunning(): boolean {
    return this.running;
  }
}

