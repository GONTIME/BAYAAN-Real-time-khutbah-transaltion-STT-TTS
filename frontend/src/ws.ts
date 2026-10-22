/* Reconnecting WebSocket client (§188: public room
   websocket -> reconnect -> room unavailable). */

export type CaptionTarget = { text: string; state: "ok" | "unavailable"; speak?: boolean };

/* §194 Phase 5 audio client: the server broadcasts spoken audio as raw PCM
   (base64) only for captions that passed the §80 TTS gate. The client plays
   it and applies its own staleness drop (§88) so late audio is never heard. */
export type AudioEvent = {
  type: "audio";
  sequence: number;
  created_at?: string;
  language: string;
  content_type: string;
  audio_format: string; // "pcm_s16le"
  sample_rate: number;
  duration_ms: number;
  voice: string;
  audio_b64: string;
};

export type CaptionEvent = {
  type: "caption";
  sequence: number;
  content_type: string;
  created_at: string;
  arabic: string;
  targets: Record<string, CaptionTarget>;
};

export type HelloEvent = {
  type: "hello";
  room_code: string;
  session_id: string | null;
  status: string | null;
  languages: string[];
  // §201: whether the server will emit spoken audio at all. When false the
  // room page shows "Audio disabled on the server" instead of a dead toggle.
  tts_enabled?: boolean;
};

export type VideoFeedEvent = {
  type: "video_feed";
  video_feed_url: string | null;
  video_feed_kind: string | null;
  // ADR-0015: whether worshipper phones should also render the feed.
  video_feed_mobile?: boolean;
};

export type ServerEvent =
  | HelloEvent
  | CaptionEvent
  | AudioEvent
  | VideoFeedEvent
  | { type: "session"; status: string }
  | { type: "language"; lang: string }
  | { type: "pong" };

export type ConnectionState = "connecting" | "open" | "closed" | "unavailable";

export type RoomCallbacks = {
  onState: (state: ConnectionState) => void;
  onEvent: (event: ServerEvent) => void;
};

export class RoomSocket {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private closedByUser = false;
  private reconnectTimer: number | null = null;

  constructor(
    private code: string,
    private lang: string,
    private token: string,
    private callbacks: RoomCallbacks
  ) {}

  connect(): void {
    this.closedByUser = false;
    this.open();
  }

  setLanguage(lang: string): void {
    this.lang = lang;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "set_language", lang }));
    }
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  private open(): void {
    this.callbacks.onState("connecting");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws/rooms/${this.code}?lang=${this.lang}&token=${this.token}`;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => {
      this.attempt = 0;
      this.callbacks.onState("open");
    };

    ws.onmessage = (msg) => {
      try {
        this.callbacks.onEvent(JSON.parse(msg.data));
      } catch {
        /* ignore malformed frames */
      }
    };

    ws.onclose = (ev) => {
      this.ws = null;
      if (this.closedByUser) return;
      // 4404 = room unavailable: do not retry forever (§188).
      if (ev.code === 4404 || ev.code === 4401 || ev.code === 4400) {
        this.callbacks.onState("unavailable");
        return;
      }
      this.scheduleReconnect();
    };

    ws.onerror = () => {
      /* onclose follows */
    };
  }

  private scheduleReconnect(): void {
    // Exponential backoff with cap: 0.5s, 1s, 2s, 4s, ... max 5s.
    const delay = Math.min(500 * 2 ** this.attempt, 5000);
    this.attempt += 1;
    this.callbacks.onState("closed");
    this.reconnectTimer = window.setTimeout(() => this.open(), delay);
  }
}
