/* PC camera relay client (ADR-0015).
 *
 * Captures the operator's webcam with getUserMedia, draws the latest frame
 * into a canvas at the server's target size, and sends a complete JPEG per
 * interval over the operator-authenticated relay WebSocket
 * /ws/sessions/{id}/camera. The server keeps only the latest frame and serves
 * viewers as an MJPEG stream, so the projector / phones never hold their own
 * capture connection.
 *
 * Mirrors the audio-ingest requirements: a secure context (https/localhost)
 * and a user gesture (pressing "Start") are required by getUserMedia.
 * Failures surface as thrown errors - honest, never faked (§0 rule 8).
 */

export type CameraRelayOptions = {
  width?: number;
  height?: number;
  fps?: number;
  quality?: number;
};

export class CameraRelayClient {
  private ws: WebSocket | null = null;
  private stream: MediaStream | null = null;
  private video: HTMLVideoElement | null = null;
  private canvas: HTMLCanvasElement | null = null;
  private ctx: CanvasRenderingContext2D | null = null;
  private interval = 0;
  private pushing = false;
  private running = false;
  private onClose: ((code: number) => void) | null = null;
  private readonly opts: Required<CameraRelayOptions>;

  constructor(opts: CameraRelayOptions = {}) {
    this.opts = {
      width: 640,
      height: 360,
      fps: 10,
      quality: 0.6,
      ...opts,
    };
  }

  isRunning(): boolean {
    return this.running;
  }

  /** The live local preview stream (bind to a <video srcObject>). */
  getStream(): MediaStream | null {
    return this.stream;
  }

  /** Notify the caller when the server closes the relay (e.g. session ended). */
  setOnClose(cb: ((code: number) => void) | null): void {
    this.onClose = cb;
  }

  async start(sessionId: string, token: string): Promise<void> {
    if (this.running) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error(
        "Camera capture needs a secure context (https or localhost)."
      );
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      // PC camera: no facingMode hint (that is phone-oriented; a PC webcam is
      // user-facing and an "environment" ideal can pick the wrong camera on
      // multi-camera Windows devices).
      video: {
        width: { ideal: this.opts.width },
        height: { ideal: this.opts.height },
      },
      audio: false,
    });

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws/sessions/${encodeURIComponent(
      sessionId
    )}/camera?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(url);
    try {
      await new Promise<void>((resolve, reject) => {
        const timer = window.setTimeout(() => {
          reject(new Error("Camera relay connection timed out"));
        }, 8000);
        ws.onopen = () => {
          window.clearTimeout(timer);
          resolve();
        };
        ws.onerror = () => {
          window.clearTimeout(timer);
          reject(new Error("Camera relay connection failed"));
        };
        // Rejection codes (4401 token, 4404 session, 4409 not LIVE, 4429 rate
        // limit, 4431 bad frames, 4432 relay disabled) arrive as a CLOSE, not
        // an error — without this the await above would hang forever.
        ws.onclose = (ev) => {
          window.clearTimeout(timer);
          reject(
            new Error(
              ev.code === 1000 || ev.code === 1006
                ? "Camera relay connection closed"
                : `Camera relay rejected (code ${ev.code})`
            )
          );
        };
      });
    } catch (err) {
      // Release the webcam so a failed relay never leaves it turned on.
      stream.getTracks().forEach((t) => t.stop());
      ws.close();
      throw err;
    }

    this.ws = ws;
    this.stream = stream;
    this.running = true;
    // From here the close hook is the STREAM's (session ended → notify caller),
    // not the handshake's.
    ws.onclose = (ev) => {
      if (this.running) this.onClose?.(ev.code);
    };

    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.srcObject = stream;
    await video.play().catch(() => undefined);
    this.video = video;

    this.canvas = document.createElement("canvas");
    this.canvas.width = this.opts.width;
    this.canvas.height = this.opts.height;
    this.ctx = this.canvas.getContext("2d");
    this.pushing = false;

    // Timer-driven cadence (not requestAnimationFrame): rAF is throttled or
    // paused in hidden tabs, which froze the camera whenever the operator
    // switched windows mid-sermon.
    this.interval = window.setInterval(
      () => this.tick(),
      Math.max(50, Math.round(1000 / this.opts.fps))
    );
  }

  private tick(): void {
    if (!this.running || this.pushing) return;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.pushFrame();
    }
  }

  private pushFrame(): void {
    const ctx = this.ctx;
    const canvas = this.canvas;
    if (!ctx || !canvas || !this.video || !this.ws) return;
    // HAVE_CURRENT_DATA (2): no fresh camera frame to draw yet.
    if (this.video.readyState < 2) return;
    this.pushing = true;
    ctx.drawImage(this.video, 0, 0, this.opts.width, this.opts.height);
    canvas.toBlob(
      (blob) => {
        this.pushing = false;
        if (blob && this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(blob);
        }
      },
      "image/jpeg",
      this.opts.quality
    );
  }

  stop(): void {
    if (!this.running) return;
    this.running = false;
    if (this.interval) {
      window.clearInterval(this.interval);
      this.interval = 0;
    }
    this.pushing = false;
    this.stream?.getTracks().forEach((t) => t.stop());
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      try {
        ws.send(JSON.stringify({ type: "stop" }));
      } catch {
        /* already closed */
      }
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    }
    this.stream = null;
    this.video = null;
    this.canvas = null;
    this.ctx = null;
  }
}