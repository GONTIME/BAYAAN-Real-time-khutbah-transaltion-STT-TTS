import { useCallback, useEffect, useRef, useState } from "react";
import {
  ConnectionState,
  CaptionEvent,
  CaptionTarget,
  RoomSocket,
  ServerEvent,
} from "./ws";
import { AudioPlayer, isSilentAudioEvent } from "./audio";
import { Icon } from "./components/Icon";
import {
  AdaptiveVideo,
  VideoFeedKind,
} from "./video";

type Phase =
  | { kind: "loading" }
  | { kind: "unavailable" } // §188: room unavailable
  | { kind: "ready"; languages: string[]; status: string | null };

type Caption = {
  sequence: number;
  arabic: string;
  // §134: keep EVERY language the server sent, then resolve the visible text
  // for whichever language is selected at render time. Storing a pre-resolved
  // single string here is what broke language switching (captions kept showing
  // the language that was selected when they arrived).
  targets: Record<string, CaptionTarget>;
  createdAt: string;
};

const UNAVAILABLE_TARGET: CaptionTarget = { text: "", state: "unavailable", speak: false };

// Resolve one caption for the currently selected language. Falls back to the
// §134 "unavailable" placeholder when the server did not send this language
// (never fabricates text).
function resolveCaption(caption: Caption, lang: string): CaptionTarget {
  return caption.targets[lang] ?? UNAVAILABLE_TARGET;
}

function roomCodeFromLocation(): string | null {
  const match = location.pathname.match(/\/r\/([A-Za-z0-9]+)/);
  if (match) return match[1];
  const param = new URLSearchParams(location.search).get("room");
  return param && /^[A-Za-z0-9]+$/.test(param) ? param : null;
}

export default function RoomPage() {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [language, setLanguage] = useState<string | null>(null);
  const [showArabic, setShowArabic] = useState(false);
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [connState, setConnState] = useState<ConnectionState>("closed");
  const [audioOn, setAudioOn] = useState(false);
  // §201: server tells us whether spoken audio is enabled at all. Defaults to
  // true until the hello event arrives, then reflects the real server policy.
  const [ttsEnabled, setTtsEnabled] = useState(true);
  // ADR-0015: video feed surfaced only when the operator enables it for the
  // room page (video_feed_mobile). Hidden by default -> phones stay
  // captions-only unless the operator opts a session in.
  const [videoFeedUrl, setVideoFeedUrl] = useState<string | null>(null);
  const [videoFeedKind, setVideoFeedKind] = useState<VideoFeedKind | null>(null);
  const [videoFeedMobile, setVideoFeedMobile] = useState(false);
  const [visitorToken, setVisitorToken] = useState<string>("");
  const [roomCode, setRoomCode] = useState<string | null>(null);
  const socketRef = useRef<RoomSocket | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const langRef = useRef<string>("ar");
  const audioRef = useRef<AudioPlayer | null>(null);
  if (audioRef.current === null) audioRef.current = new AudioPlayer();
  const speechRef = useRef<SpeechSynthesisUtterance | null>(null);
  const lastSpokenSequenceRef = useRef<number | null>(null);
  // Live mirrors of state that the memoized socket handler must read without
  // re-creating the socket. The socket's onEvent closure is created once
  // (§188 reconnect logic owns it), so pushing UI state through refs keeps
  // pushCaption correct on the latest audioOn/ttsEnabled without a stale
  // first-render capture (the bug that made browser speech never fire).
  const audioOnRef = useRef(false);
  const ttsEnabledRef = useRef(true);
  const realAudioSeqRef = useRef<number | null>(null);

  const pushCaption = useCallback((event: CaptionEvent) => {
    // §134: keep every language's translation on the caption so the visible
    // text can be re-resolved whenever the visitor switches language. Do NOT
    // collapse to a single language here (that froze captions at the language
    // selected when they arrived).
    setCaptions((prev) => [
      ...prev.filter((c) => c.sequence !== event.sequence),
      {
        sequence: event.sequence,
        arabic: event.arabic,
        targets: event.targets,
        createdAt: event.created_at,
      },
    ]);
    setCaptions((prev) => prev.slice(-30));

    // §194/§201: speak §80-approved captions using the browser's built-in
    // SpeechSynthesis when the user has opted in, the server policy allows
    // audio, and the server marked the line as speakable. This works with any
    // backend TTS provider (including the mock, which emits silence by design)
    // so spoken audio is actually heard in the room. State is read through the
    // refs below, NOT captured state, so the stable first-render pushCaption
    // that the socket handler holds always sees the current audio policy.
    if (!audioOnRef.current || !ttsEnabledRef.current) return;
    if (!("speechSynthesis" in window)) return;
    const speakLang = langRef.current;
    const target = event.targets[speakLang];
    if (!target || !target.speak) return;
    // A real (non-silent) audio event has already voiced this line: never
    // double-speak it with browser speech (§194).
    if (realAudioSeqRef.current === event.sequence) return;
    if (lastSpokenSequenceRef.current === event.sequence) return;
    const text = (target.text || "").trim();
    if (!text) return;
    lastSpokenSequenceRef.current = event.sequence;
    try {
      window.speechSynthesis.cancel();
      const utt = new SpeechSynthesisUtterance(text);
      utt.lang = speakLang;
      utt.rate = 1;
      speechRef.current = utt;
      window.speechSynthesis.speak(utt);
    } catch {
      /* best-effort: never break captions if speech fails */
    }
  }, []);

  const join = useCallback((code: string, token: string, lang: string) => {
    langRef.current = lang;
    audioRef.current?.setLanguage(lang);
    socketRef.current?.close();
    const socket = new RoomSocket(code, lang, token, {
      onState: setConnState,
      onEvent: (event: ServerEvent) => {
        if (event.type === "caption") {
          pushCaption(event);
        } else if (event.type === "audio") {
          // §194: play §80-approved audio for the selected language; the
          // player applies its own stale drop (§88).
          const played = audioRef.current?.handle(event, Date.now()) === true;
          // A real (non-silent) TTS provider voiced this line: record it and
          // cancel any browser-speech fallback for the same line so it is
          // never double-spoken (§194). Silent events (the bundled mock emits
          // silence by design, ADR-0006) leave the fallback alone.
          if (played && !isSilentAudioEvent(event)) {
            realAudioSeqRef.current = event.sequence;
            if ("speechSynthesis" in window) window.speechSynthesis.cancel();
          }
        } else if (event.type === "hello") {
          // §201: reflect the server's real audio policy so the toggle is
          // honest (disabled on the server => show that, not a dead switch).
          const enabled = event.tts_enabled !== false;
          setTtsEnabled(enabled);
          ttsEnabledRef.current = enabled;
        } else if (event.type === "video_feed") {
          // ADR-0015: the operator may change the feed (or its mobile flag)
          // live; track it so the room page follows without a reload.
          setVideoFeedUrl(event.video_feed_url || null);
          setVideoFeedKind(
            (event.video_feed_kind as VideoFeedKind) || null
          );
          setVideoFeedMobile(event.video_feed_mobile === true);
        } else if (event.type === "session") {
          setPhase((p) =>
            p.kind === "ready" ? { ...p, status: event.status } : p
          );
        }
      },
    });
    socketRef.current = socket;
    socket.connect();
    setLanguage(lang);
    localStorage.setItem("bayaan_lang", lang);
  }, []);

  // Discover the room, then join with the selected language.
  useEffect(() => {
    const code = roomCodeFromLocation();
    if (!code) {
      setPhase({ kind: "unavailable" });
      return;
    }
    setRoomCode(code);
    let disposed = false;

    fetch(`/api/v1/rooms/${code}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((info) => {
        if (disposed) return;
        setPhase({
          kind: "ready",
          languages: info.languages,
          status: info.session_status,
        });
        // ADR-0015: reflect the operator-configured feed (surfaced only when
        // the mobile flag is set; the projector uses the same source).
        setVideoFeedUrl(info.video_feed_url || null);
        setVideoFeedKind((info.video_feed_kind as VideoFeedKind) || null);
        setVideoFeedMobile(info.video_feed_mobile === true);
        setVisitorToken(info.visitor_token);
        const remembered = localStorage.getItem("bayaan_lang");
        const lang =
          remembered && info.languages.includes(remembered)
            ? remembered
            : info.languages[0];
        join(code, info.visitor_token, lang);
      })
      .catch(() => {
        if (!disposed) setPhase({ kind: "unavailable" });
      });

    return () => {
      disposed = true;
      socketRef.current?.close();
    };
  }, [join]);

  const switchLanguage = (lang: string) => {
    langRef.current = lang;
    setLanguage(lang);
    localStorage.setItem("bayaan_lang", lang);
    socketRef.current?.setLanguage(lang);
    audioRef.current?.setLanguage(lang);
    // §134: each caption stores every language's translation, and the visible
    // text is resolved from the current language at render time. Changing
    // `language` re-renders the whole list in the newly selected language.
    setCaptions((prev) => [...prev]);
  };

  // §201 text-first: audio is a deliberate opt-in behind a user gesture so the
  // browser autoplay policy is satisfied and the worshipper chooses to listen.
  const toggleAudio = async () => {
    const player = audioRef.current;
    if (!player) return;
    if (audioOn) {
      player.disable();
      setAudioOn(false);
      audioOnRef.current = false;
    } else {
      await player.enable();
      const on = player.isEnabled();
      setAudioOn(on);
      audioOnRef.current = on;
    }
  };

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [captions]);

  if (phase.kind === "loading") {
    return <div className="center muted">Loading room…</div>;
  }
  if (phase.kind === "unavailable") {
    // §188: explicit "room unavailable", never a blank screen. If there is no
    // room code at all (landed on the bare home URL), point the visitor to the
    // operator console rather than implying a broken code.
    const hasCode = roomCodeFromLocation() !== null;
    return (
      <div className="center">
        <div className="badge red">
          {hasCode ? "Room unavailable" : "No room selected"}
        </div>
        <p className="muted">
          {hasCode
            ? "Check the QR code and try again."
            : "Scan a room QR code, or open the operator console to start a room."}
        </p>
        {!hasCode && (
          <p>
            <a href="/operator">Open the operator console →</a>
          </p>
        )}
      </div>
    );
  }

  const latest = captions[captions.length - 1];
  const live = phase.status === "LIVE";

  return (
    <main className="room">
      <header className="topbar">
        <span className={`badge ${live ? "green" : "gray"}`}>
          {live ? "LIVE" : (phase.status ?? "IDLE")}
        </span>
        <span className={`conn ${connState}`}>{connState}</span>
        {/* §194/§201: optional spoken audio. When the server has TTS locked
            (the shipped default) the toggle is replaced by an honest note, so a
            worshipper is never left tapping a switch that can never play. */}
        {ttsEnabled ? (
          <button
            type="button"
            className={audioOn ? "audio-toggle on" : "audio-toggle"}
            aria-pressed={audioOn}
            onClick={toggleAudio}
            title="Listen to the translation (spoken audio)"
          >
            {audioOn ? (
              <>
                <Icon name="volume-2" size={14} /> Audio on
              </>
            ) : (
              <>
                <Icon name="volume-x" size={14} /> Audio off
              </>
            )}
          </button>
        ) : (
          <span className="audio-toggle" title="Spoken audio is disabled on the server; text captions are the main output.">
            <Icon name="volume-x" size={14} /> Audio off (server)
          </span>
        )}
        <label className="ar-toggle">
          <input
            type="checkbox"
            checked={showArabic}
            onChange={(e) => setShowArabic(e.target.checked)}
          />
          العربية
        </label>
      </header>

      <section className="langbar">
        {phase.languages.map((lang) => (
          <button
            key={lang}
            className={lang === language ? "lang active" : "lang"}
            onClick={() => switchLanguage(lang)}
          >
            {lang === "it" ? "Italiano" : lang === "en" ? "English" : lang}
          </button>
        ))}
      </section>

      {/* ADR-0015: operator-configured video feed, shown on phones only when
          explicitly enabled for this session (video_feed_mobile). */}
      {(videoFeedUrl || videoFeedKind === "camera") &&
        videoFeedKind &&
        videoFeedMobile && (
          <section className="room-video" aria-label="Live video feed">
            <AdaptiveVideo
              url={videoFeedUrl ?? ""}
              kind={videoFeedKind}
              code={roomCode}
              visitorToken={visitorToken}
              muted
              autoPlay
              showControls={false}
              style={{ width: "100%", maxHeight: "28vh" }}
            />
          </section>
        )}

      <section className="current" aria-live="polite">
        {latest ? (
          <>
            {showArabic && (
              <p className="arabic" dir="rtl" lang="ar">
                {latest.arabic}
              </p>
            )}
            {/* §134: fallback message, never internal diagnostics. Resolve the
                text for the language currently selected, not the one that was
                active when the caption arrived. */}
            <p
              className={
                resolveCaption(latest, language ?? "ar").state === "ok"
                  ? "text"
                  : "text fallback"
              }
            >
              {resolveCaption(latest, language ?? "ar").text}
            </p>
          </>
        ) : (
          <p className="muted">Waiting for the khutbah to start…</p>
        )}
      </section>

      <section className="history" ref={listRef}>
        {captions
          .slice(0, -1)
          .reverse()
          .map((c) => {
            const target = resolveCaption(c, language ?? "ar");
            return (
              <div key={c.sequence} className="history-item">
                {showArabic && (
                  <span className="arabic small" dir="rtl" lang="ar">
                    {c.arabic}
                  </span>
                )}
                <span className={target.state === "ok" ? "" : "fallback"}>
                  {target.text}
                </span>
              </div>
            );
          })}
      </section>
    </main>
  );
}

