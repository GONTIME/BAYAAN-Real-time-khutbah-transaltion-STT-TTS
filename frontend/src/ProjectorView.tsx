import { useCallback, useEffect, useRef, useState } from "react";
import {
  CaptionEvent,
  CaptionTarget,
  ConnectionState,
  RoomSocket,
  VideoFeedEvent,
} from "./ws";
import {
  AdaptiveVideo,
  VideoFeedKind,
} from "./video";
import { Icon } from "./components/Icon";

type Caption = {
  sequence: number;
  arabic: string;
  targets: Record<string, CaptionTarget>;
  createdAt: string;
};

const UNAVAILABLE_TARGET: CaptionTarget = { text: "", state: "unavailable", speak: false };

function roomCodeFromPath(): string | null {
  const match = location.pathname.match(/\/projector\/([A-Za-z0-9]+)/);
  if (match) return match[1];
  const rMatch = location.pathname.match(/\/r\/([A-Za-z0-9]+)/);
  if (rMatch) return rMatch[1];
  return new URLSearchParams(location.search).get("room");
}

export default function ProjectorView() {
  const [code, setCode] = useState<string | null>(null);
  const [lang, setLang] = useState<string>("it");
  const [availableLangs, setAvailableLangs] = useState<string[]>(["it", "en"]);
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [connState, setConnState] = useState<ConnectionState>("closed");
  const [fontSize, setFontSize] = useState<number>(36);
  const [videoFeedUrl, setVideoFeedUrl] = useState<string | null>(null);
  const [videoFeedKind, setVideoFeedKind] = useState<VideoFeedKind | null>("hls");
  const [visitorToken, setVisitorToken] = useState<string>("");
  const socketRef = useRef<RoomSocket | null>(null);

  const pushCaption = useCallback((event: CaptionEvent) => {
    setCaptions((prev) => [
      ...prev.filter((c) => c.sequence !== event.sequence),
      {
        sequence: event.sequence,
        arabic: event.arabic,
        targets: event.targets,
        createdAt: event.created_at,
      },
    ]);
  }, []);

  const join = useCallback((roomCode: string, token: string, targetLang: string) => {
    socketRef.current?.close();
    const socket = new RoomSocket(roomCode, targetLang, token, {
      onState: setConnState,
      onEvent: (event: VideoFeedEvent | any) => {
        if (event.type === "caption") {
          pushCaption(event);
        } else if (event.type === "video_feed") {
          setVideoFeedUrl(event.video_feed_url || null);
          setVideoFeedKind((event.video_feed_kind as VideoFeedKind) || "hls");
        }
      },
    });
    socketRef.current = socket;
    socket.connect();
    setLang(targetLang);
  }, [pushCaption]);

  useEffect(() => {
    const rc = roomCodeFromPath();
    setCode(rc);
    if (!rc) return;

    let disposed = false;
    fetch(`/api/v1/rooms/${rc}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((info) => {
        if (disposed) return;
        setAvailableLangs(info.languages);
        if (info.video_feed_url || info.video_feed_kind) {
          setVideoFeedUrl(info.video_feed_url || null);
          setVideoFeedKind(
            (info.video_feed_kind as VideoFeedKind) || "hls"
          );
        }
        setVisitorToken(info.visitor_token);
        const preferred = info.languages.includes("it") ? "it" : info.languages[0];
        join(rc, info.visitor_token, preferred);
      })
      .catch(() => {});

    return () => {
      disposed = true;
      socketRef.current?.close();
    };
  }, [join]);

  const latest = captions.length > 0 ? captions[captions.length - 1] : null;
  const history = captions.slice(-4, -1).reverse();
  const currentTarget = latest ? (latest.targets[lang] ?? UNAVAILABLE_TARGET) : null;

  const toggleFullscreen = () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
    } else {
      document.exitFullscreen().catch(() => {});
    }
  };

  if (!code) {
    return (
      <div className="projector-screen center">
        <h1>BAYAAN Projector Display</h1>
        <p className="muted">No room code specified. Use /projector/&lt;ROOM_CODE&gt;</p>
      </div>
    );
  }

  return (
    <div className="projector-screen">
      <header className="projector-header">
        <div className="projector-title">
          <span className={`dot ${connState === "open" ? "live" : "offline"}`} />
          <span>BAYAAN LIVE</span>
          <span className="room-code-tag">Room {code}</span>
        </div>
        <div className="projector-controls">
          <div className="lang-switcher">
            {availableLangs.map((l) => (
              <button
                key={l}
                className={`btn-sm ${lang === l ? "active" : ""}`}
                onClick={() => {
                  setLang(l);
                  socketRef.current?.setLanguage(l);
                }}
              >
                {l.toUpperCase()}
              </button>
            ))}
          </div>
          <div className="font-scaler">
            <button className="btn-icon" onClick={() => setFontSize((s) => Math.max(24, s - 4))}>A-</button>
            <button className="btn-icon" onClick={() => setFontSize((s) => Math.min(64, s + 4))}>A+</button>
          </div>
          <button className="btn-icon" onClick={toggleFullscreen} title="Toggle Fullscreen">
            <Icon name="maximize" size={18} />
          </button>
        </div>
      </header>

      <main className="projector-main">
        <div className="projector-layout">
          <div className="projector-content">
            {latest ? (
              <div className="projector-current" style={{ fontSize: `${fontSize}px` }}>
                <div className="projector-arabic" dir="rtl">{latest.arabic}</div>
                <div className="projector-divider" />
                <div className={`projector-translation ${currentTarget?.state === "unavailable" ? "fallback" : ""}`}>
                  {currentTarget?.text || "..."}
                </div>
              </div>
            ) : (
              <div className="projector-waiting">
                <div className="pulse-ring" />
                <h2>In attesa dell'inizio della khutbah...</h2>
                <p className="muted">Waiting for sermon to begin / في انتظار بدء الخطبة</p>
              </div>
            )}

            {history.length > 0 && (
              <div className="projector-history">
                {history.map((c) => {
                  const t = c.targets[lang] ?? UNAVAILABLE_TARGET;
                  return (
                    <div key={c.sequence} className="projector-history-item">
                      <div className="hist-ar" dir="rtl">{c.arabic}</div>
                      <div className="hist-tr">{t.text}</div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {(videoFeedUrl || videoFeedKind === "camera") && videoFeedKind && (
            <aside className="projector-video-side">
              <div className="projector-video-wrapper">
                <AdaptiveVideo
                  url={videoFeedUrl ?? ""}
                  kind={videoFeedKind}
                  code={code}
                  visitorToken={visitorToken}
                  style={{ width: "100%" }}
                />
              </div>
            </aside>
          )}
        </div>
      </main>
    </div>
  );
}
