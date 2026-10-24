import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  Draft,
  GlossaryTerm,
  RoomStart,
  SafetyBlockEntry,
  ScriptureVerse,
  Sermon,
  SermonSegment,
  SermonVersion,
  SessionInfo,
  SystemSummary,
  TestTranslationResult,
} from "./adminApi";
import { LiveArabicRecognizer, MicCapture } from "./capture";
import { CameraRelayClient } from "./camera";
import { languageLabel } from "./languages";
import { VUMeter } from "./components/audio/VUMeter";
import { buildSrt, captionsToCues, downloadSrtFile } from "./srt";
import { logoutOperator, getStoredUsername, getStoredToken } from "./auth";
import type { VideoFeedKind } from "./video";

import { CaptionTarget, RoomSocket, ServerEvent } from "./ws";
import { Icon } from "./components/Icon";


type ActiveTab = "live" | "sermon" | "diagnostics" | "scripture";

type SegPanel = {
  segment: SermonSegment;
  drafts: Draft[];
  glossary_hits: string[];
  warnings: string[];
};

type LiveFeedItem = {
  sequence: number;
  arabic: string;
  targets: Record<string, CaptionTarget>;
  createdAt: string;
  source?: string;
  contentType?: string;
};

export default function Dashboard() {
  const [tab, setTab] = useState<ActiveTab>("live");
  const [darkMode, setDarkMode] = useState<boolean>(() => {
    return localStorage.getItem("bayaan_dark_mode") === "true";
  });

  // Global status
  const [summary, setSummary] = useState<SystemSummary | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deletingDraftId, setDeletingDraftId] = useState<string | null>(null);

  // Live session state
  const [room, setRoom] = useState<RoomStart | null>(null);
  const [_sessions, setSessions] = useState<SessionInfo[]>([]);
  const [activeSession, setActiveSession] = useState<SessionInfo | null>(null);
  const [demoText, setDemoText] = useState("الحمد لله رب العالمين، الرحمن الرحيم");
  const [liveFeed, setLiveFeed] = useState<LiveFeedItem[]>([]);

  // Audio capture state
  const [micActive, setMicActive] = useState(false);
  const [audioLevel, setAudioLevel] = useState(0); // 0..1
  const [speechText, setSpeechText] = useState("");
  const micRef = useRef<MicCapture | null>(null);
  if (!micRef.current) micRef.current = new MicCapture();
  const recognizerRef = useRef<LiveArabicRecognizer | null>(null);
  if (!recognizerRef.current) recognizerRef.current = new LiveArabicRecognizer();
  const lastSentUtteranceRef = useRef<string>("");
  const sessionStartTimeRef = useRef<number>(Date.now());

  // Video feed stream state
  const [videoFeedUrl, setVideoFeedUrl] = useState("");
  const [videoFeedKind, setVideoFeedKind] = useState<VideoFeedKind>("hls");
  // ADR-0015: also show the feed on worshipper phones (off by default).
  const [videoFeedMobile, setVideoFeedMobile] = useState(false);

  // PC camera relay state (ADR-0015)
  const [cameraActive, setCameraActive] = useState(false);
  const [cameraPreview, setCameraPreview] = useState<MediaStream | null>(null);
  const cameraRef = useRef<CameraRelayClient | null>(null);
  if (!cameraRef.current) cameraRef.current = new CameraRelayClient();
  const cameraPreviewRef = useRef<HTMLVideoElement | null>(null);

  // Bind the operator's local camera preview (srcObject via ref so React 18
  // types are satisfied and the stream is attached without a re-mount).
  useEffect(() => {
    if (cameraPreviewRef.current) {
      cameraPreviewRef.current.srcObject = cameraPreview;
    }
  }, [cameraPreview]);

  // Room WS listener for operator preview
  const operatorSocketRef = useRef<RoomSocket | null>(null);

  // Khutbah / Sermon state
  const [sermons, setSermons] = useState<Sermon[]>([]);
  const [selectedSermon, setSelectedSermon] = useState<string | null>(null);
  const [_versions, setVersions] = useState<SermonVersion[]>([]);

  const [selectedVersion, setSelectedVersion] = useState<string | null>(null);
  const [versionStatus, setVersionStatus] = useState<string | null>(null);
  const [panels, setPanels] = useState<SegPanel[]>([]);
  const [reviewerName, setReviewerName] = useState("operator");
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Diagnostics Playground state
  const [playText, setPlayText] = useState("إن الله مع الصابرين");
  // ADR-0017: the target is one of the CONFIGURED languages, not a hard-coded
  // it/en pair ("" = "use the first configured target").
  const [playLang, setPlayLang] = useState<string>("");
  const [playResult, setPlayResult] = useState<TestTranslationResult | null>(null);
  const [playBusy, setPlayBusy] = useState(false);
  // ADR-0017: operator-only view of WHY lines were held back (§98/§168). The
  // room keeps seeing just the §134 fallback - this is the explainable side.
  const [safetyBlocks, setSafetyBlocks] = useState<SafetyBlockEntry[]>([]);

  // ADR-0017: languages this server is configured for, straight from the
  // backend catalog. The fallback keeps the UI usable for the split second
  // before the first summary arrives (and on an older backend).
  const configuredTargets = summary?.languages?.targets ?? [];
  const targetCodes = configuredTargets.length
    ? configuredTargets.map((t) => t.code)
    : ["it", "en"];
  // Per-room selection (§63): null = "all configured targets" (the default).
  const [roomLangs, setRoomLangs] = useState<string[] | null>(null);
  const effectiveRoomLangs = roomLangs ?? targetCodes;
  const effectivePlayLang = playLang || targetCodes[0];
  // ADR-0015: the PC camera relay only accepts a LIVE session - the UI mirrors
  // that rule so the webcam never switches on for a relay that will be refused.
  const sessionIsLive = (room?.status || activeSession?.status) === "LIVE";
  // Catalog entries the admin has NOT enabled yet - shown as a hint so the
  // picker is a real list, not a code the operator has to remember.
  const notConfiguredCodes = (summary?.languages?.catalog ?? [])
    .filter((entry) => entry.code !== (summary?.languages?.source ?? "ar"))
    .filter((entry) => !targetCodes.includes(entry.code))
    .map((entry) => entry.code);

  // Scripture & Glossary state
  const [scriptureList, setScriptureList] = useState<ScriptureVerse[]>([]);
  const [scriptureSearch, setScriptureSearch] = useState("2:255");
  const [scriptureKind, setScriptureKind] = useState("QURAN");
  const [scriptureLoading, setScriptureLoading] = useState(false);
  const [glossaryTerms, setGlossaryTerms] = useState<GlossaryTerm[]>([]);
  const [newTermAr, setNewTermAr] = useState("");
  const [newTermIt, setNewTermIt] = useState("");
  const [newTermEn, setNewTermEn] = useState("");

  const flash = (msg: string) => {
    setNotice(msg);
    setError(null);
    setTimeout(() => setNotice(null), 6000);
  };

  const toggleDarkMode = () => {
    const next = !darkMode;
    setDarkMode(next);
    localStorage.setItem("bayaan_dark_mode", String(next));
  };

  // Load summary and sessions
  const refreshSummary = useCallback(async () => {
    try {
      const s = await api.systemSummary();
      setSummary(s);
      const active = await api.activeSessions();
      setSessions(active);
      if (active.length > 0) {
        setActiveSession(active[0]);
      } else {
        setActiveSession(null);
      }
    } catch (e) {
      // Keep silent on periodic polling error
    }
  }, []);

  useEffect(() => {
    void refreshSummary();
    const interval = setInterval(refreshSummary, 4000);
    return () => clearInterval(interval);
  }, [refreshSummary]);

  // Connect operator WS to room when room is known
  useEffect(() => {
    const code = room?.code;
    if (!code) return;
    let disposed = false;

    fetch(`/api/v1/rooms/${code}`)
      .then((r) => r.json())
      .then((info) => {
        if (disposed) return;
        operatorSocketRef.current?.close();
        const sock = new RoomSocket(code, "it", info.visitor_token, {
          onState: () => {},
          onEvent: (event: ServerEvent) => {
            if (event.type === "caption") {
              setLiveFeed((prev) => [
                ...prev.filter((c) => c.sequence !== event.sequence),
                {
                  sequence: event.sequence,
                  arabic: event.arabic,
                  targets: event.targets,
                  createdAt: event.created_at,
                },
              ]);
            }
          },
        });
        operatorSocketRef.current = sock;
        sock.connect();
      })
      .catch(() => {});

    return () => {
      disposed = true;
      operatorSocketRef.current?.close();
    };
  }, [room?.code]);

  // ADR-0017: load the operator-facing safety explanations when the Diagnostics
  // tab is opened (and on demand from the card's Refresh button).
  const loadSafetyBlocks = useCallback(async () => {
    try {
      const res = await api.safetyBlocks(20);
      setSafetyBlocks(res.blocks);
    } catch {
      // Diagnostics must never break the cockpit; the card simply stays empty.
    }
  }, []);

  useEffect(() => {
    if (tab === "diagnostics") {
      void loadSafetyBlocks();
    }
  }, [tab, loadSafetyBlocks]);

  // --- LIVE ROOM CONTROL ---
  const handleStartRoom = async () => {
    setBusy(true);
    setError(null);
    try {
      // ADR-0017: start the room with the languages the operator ticked (which
      // default to everything this server is configured for) - never a
      // hard-coded pair that a differently-configured server would reject.
      if (effectiveRoomLangs.length === 0) {
        setError("Pick at least one language for this room.");
        return;
      }
      const started = await api.startRoom(effectiveRoomLangs);
      setRoom(started);
      flash(`Room ${started.code} created in READY state. Share QR code, then click "Go LIVE".`);
      await refreshSummary();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleGoLive = async () => {
    const sid = room?.session_id || activeSession?.id;
    if (!sid) return;
    setBusy(true);
    setError(null);
    try {
      await api.setSessionStatus(sid, "READY").catch(() => {});
      await api.setSessionStatus(sid, "LIVE");
      if (room) setRoom({ ...room, status: "LIVE" });
      flash("Broadcast is now LIVE! Attendees will receive live captions.");
      await refreshSummary();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleExportSrt = () => {
    if (!liveFeed.length) {
      flash("No live captions available to export yet.");
      return;
    }
    const timestampStr = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const roomCode = room?.code || "room";

    // Format feed items for cues
    const cueInput = liveFeed.map((item) => ({
      sequence: item.sequence,
      arabic: item.arabic,
      targets: Object.entries(item.targets).reduce((acc, [k, v]) => {
        acc[k] = { text: v.text, state: v.state };
        return acc;
      }, {} as Record<string, { text: string; state: string }>),
      time: item.createdAt,
    }));

    // ADR-0017: export every language this room actually broadcasts (source +
    // configured targets), instead of a hard-coded ar/it/en triple.
    const languagesToExport = [
      summary?.languages?.source ?? "ar",
      ...(room?.languages ?? targetCodes),
    ];
    let count = 0;
    for (const lang of languagesToExport) {
      const cues = captionsToCues(cueInput, lang, sessionStartTimeRef.current);
      if (cues.length > 0) {
        const srtContent = buildSrt(cues);
        downloadSrtFile(`bayaan_${roomCode}_${timestampStr}_${lang}.srt`, srtContent);
        count++;
      }
    }
    flash(`Exported ${count} SRT broadcast subtitle files.`);
  };

  /**
   * Clear every piece of state tied to a closed room, so the cockpit returns to
   * IDLE and a new room can be started immediately.
   *
   * Without this the room object stayed in state with status ENDED: the header
   * gates "Start New Live Room" on `!room && !activeSession`, "Go LIVE" on
   * READY/CREATED and "End Broadcast" on LIVE - so an ENDED room matched none of
   * them and the operator had NO button at all until a page reload.
   */
  const resetRoomState = () => {
    setRoom(null);
    setActiveSession(null);
    setLiveFeed([]);
    setRoomLangs(null);
    setVideoFeedUrl("");
    setVideoFeedKind("hls");
    setVideoFeedMobile(false);
    setSpeechText("");
    sessionStartTimeRef.current = Date.now();
  };

  const handleEndSession = async (sessionId?: string) => {
    const sid = sessionId || room?.session_id || activeSession?.id;
    if (!sid) return;

    if (liveFeed.length > 0 && window.confirm("Save broadcast subtitles (.SRT) for this sermon before closing?")) {
      handleExportSrt();
    }

    setBusy(true);
    setError(null);
    try {
      if (micActive) {
        await handleStopMic();
      }
      if (cameraActive) {
        handleStopCamera();
      }
      const closedCode = room?.session_id === sid ? room.code : null;
      await api.setSessionStatus(sid, "ENDED");
      // The room is finished: drop its state (the operator socket closes with
      // the room effect's cleanup) so the cockpit offers a fresh room at once.
      resetRoomState();
      flash(
        `Room ${closedCode ? `${closedCode} ` : ""}closed. You can start a new room now.`
      );
      await refreshSummary();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleSaveVideoFeed = async () => {
    const sid = room?.session_id || activeSession?.id;
    if (!sid) {
      setError("No active session to configure video feed for.");
      return;
    }
    try {
      // camera kind derives its URL from the room code on the viewer side, so
      // the url is cleared (nothing to persist).
      const kind = videoFeedKind;
      const url = kind === "camera" ? null : videoFeedUrl.trim() || null;
      await api.setVideoFeed(sid, url, kind, videoFeedMobile);
      flash("Video stream feed configured successfully!");
    } catch (e) {
      setError(`Failed to set video feed: ${String(e)}`);
    }
  };

  const handleStartCamera = async () => {
    const sid = room?.session_id || activeSession?.id;
    if (!sid) {
      setError("Cannot start the camera without an active session. Create or start a room first.");
      return;
    }
    try {
      const token = getStoredToken();
      if (!token) {
        setError("Operator session expired — log in again.");
        return;
      }
      // ADR-0015 pre-flight: ask the server BEFORE getUserMedia switches the
      // webcam on. Without this the relay rejects a non-LIVE session after the
      // webcam is already on, which looked like "camera starts then closes".
      const ready = await api.cameraReady(sid);
      if (!ready.ready) {
        setError(`Camera not started: ${ready.reason}`);
        return;
      }
      cameraRef.current?.setOnClose((_code, reason) => {
        setCameraActive(false);
        setCameraPreview(null);
        flash(reason || "Camera stream ended.");
      });
      await cameraRef.current?.start(sid, token);
      setCameraActive(true);
      setCameraPreview(cameraRef.current?.getStream() ?? null);
      // One-click flow (camera forward): starting the camera makes it the live
      // feed immediately — projector (and phones when the toggle is on) switch
      // to it without a second "Save" step.
      setVideoFeedKind("camera");
      setVideoFeedUrl("");
      flash("PC camera relay active.");
      try {
        await api.setVideoFeed(sid, null, "camera", videoFeedMobile);
        flash("Camera feed is live on the projector.");
      } catch (err) {
        setError(
          `Camera started, but saving the feed failed: ${String(err)}`
        );
      }
    } catch (e) {
      setError(`Camera error: ${String(e)}`);
    }
  };

  const handleStopCamera = () => {
    cameraRef.current?.stop();
    setCameraActive(false);
    setCameraPreview(null);
    flash("PC camera relay stopped.");
  };

  // BUG-022: browser speech recognition streams live audio to the browser
  // vendor's cloud (Google in Chrome/Edge). Ask once per page load; the
  // default is the local server ASR path, never the cloud path.
  const speechConsentRef = useRef(false);

  const handleStartMic = async () => {
    const sid = room?.session_id || activeSession?.id;
    if (!sid) {
      setError("Cannot start mic without an active session. Create or start a room first.");
      return;
    }
    try {
      lastSentUtteranceRef.current = "";

      const useBrowserSpeech =
        LiveArabicRecognizer.isSupported() &&
        (speechConsentRef.current ||
          window.confirm(
            "Privacy notice: browser speech recognition (Web Speech API) sends " +
              "live mosque audio to the browser vendor's cloud servers (e.g. Google " +
              "in Chrome/Edge) for transcription. This differs from this app's " +
              "local-first posture. To keep audio on the mosque's own server, " +
              "choose Cancel and the server-side ASR (Whisper) path is used instead."
          ));
      if (useBrowserSpeech) {
        speechConsentRef.current = true;
        // Browser Speech API path — do NOT stream PCM to the server.
        // Using MicCapture.start() here would also open the ingest WebSocket,
        // causing the mock ASR to inject its hardcoded phrase into every
        // translation.  Instead, use level-only mode for the VU meter.
        await micRef.current?.startLevelOnly(16000, (rms: number) => {
          setAudioLevel(rms);
        });

        recognizerRef.current?.start(
          async (text: string, isFinal: boolean) => {
            setSpeechText(text);
            const trimmed = text.trim();
            if (isFinal && trimmed) {
              if (trimmed === lastSentUtteranceRef.current) {
                return; // Suppress duplicate emission
              }
              lastSentUtteranceRef.current = trimmed;
              try {
                await api.sendDemoLine(sid, trimmed);
              } catch (err) {
                // Ignore transient network errors
              }
            }
          },
          (err) => {
            console.warn("Speech recognition notice:", err);
          },
          "ar-SA"
        );

        flash("Live Speech Recognition active! Speak Arabic into your microphone.");
      } else {
        // Fallback: stream raw PCM to server for server-side ASR.
        await micRef.current?.start(
          sid,
          16000,
          getStoredToken() ?? undefined
        );
        micRef.current?.setLevelCallback((rms: number) => setAudioLevel(rms));
        flash("Microphone streaming active (server ASR).");
      }

      setMicActive(true);
    } catch (e) {
      setError(`Microphone error: ${String(e)}`);
    }
  };

  const handleStopMic = async () => {
    await micRef.current?.stop();
    recognizerRef.current?.stop();
    lastSentUtteranceRef.current = "";
    setMicActive(false);
    setAudioLevel(0);
    setSpeechText("");
    flash("Microphone capture stopped.");
  };


  const handleSendDemoLine = async (text?: string) => {
    const sid = room?.session_id || activeSession?.id;
    if (!sid) {
      setError("Please create or select an active session first.");
      return;
    }
    const toSend = text || demoText;
    if (!toSend.trim()) return;
    setBusy(true);
    try {
      const seg = await api.sendDemoLine(sid, toSend.trim());
      flash(`Sent test line #${seg.sequence} to live broadcast.`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  // --- SERMON STUDIO ---
  const loadSermons = useCallback(async () => {
    try {
      const s = await api.listSermons();
      setSermons(s);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const openSermon = async (sermonId: string) => {
    setSelectedSermon(sermonId);
    setPanels([]);
    setSelectedVersion(null);
    try {
      const vs = await api.versions(sermonId);
      setVersions(vs);
      if (vs.length > 0) {
        const last = vs[vs.length - 1];
        await openVersion(last.id, last.status);
      }
    } catch (e) {
      setError(String(e));
    }
  };

  const openVersion = async (vid: string, status?: string) => {
    setSelectedVersion(vid);
    setVersionStatus(status || null);
    try {
      const segs = await api.segments(vid);
      if (status === "PUBLISHED") {
        setPanels(
          segs.map((s) => ({
            segment: s,
            drafts: [],
            glossary_hits: [],
            warnings: [],
          }))
        );
        return;
      }
      const built: SegPanel[] = [];
      for (const seg of segs) {
        const panel = await api.reviewPanel(seg.id);
        built.push({
          segment: seg,
          drafts: panel.drafts,
          glossary_hits: panel.glossary_hits,
          warnings: panel.warnings,
        });
      }
      setPanels(built);
    } catch (e) {
      setError(String(e));
    }
  };

  const handleUploadSermon = async (file: File) => {
    setBusy(true);
    try {
      const v = await api.upload(file, file.name.replace(/\.[^.]+$/, ""));
      flash(`Uploaded sermon -> version ${v.version} (${v.status})`);
      await loadSermons();
      await openSermon(v.sermon_id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleGenerateDrafts = async () => {
    if (!selectedVersion) return;
    setBusy(true);
    try {
      const res = await api.generateDrafts(selectedVersion);
      flash(`Drafts ready: ${res.created} created, ${res.skipped} already existed.`);
      if (selectedVersion) await openVersion(selectedVersion, versionStatus || undefined);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleApproveAll = async () => {
    if (!selectedVersion) return;
    setBusy(true);
    try {
      const res = await api.approveAll(selectedVersion, reviewerName);
      flash(`Approved all ${res.approved} drafts.`);
      if (selectedVersion) await openVersion(selectedVersion, versionStatus || undefined);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleDeleteDraft = async (draftId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm("Delete this draft translation?")) return;
    setDeletingDraftId(draftId);
    setBusy(true);
    try {
      await api.deleteDraft(draftId);
      if (selectedVersion) await openVersion(selectedVersion, versionStatus || undefined);
    } catch (e) {
      setError(String(e));
    } finally {
      setDeletingDraftId(null);
      setBusy(false);
    }
  };

  const handlePublishVersion = async () => {
    if (!selectedVersion) return;
    setBusy(true);
    try {
      const updated = await api.transitionVersion(selectedVersion, "PUBLISHED", reviewerName);
      setVersionStatus(updated.status);
      flash(`Sermon version is now PUBLISHED and immutable (§41)! Ready for live Friday sermon.`);
      await loadSermons();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  // --- DIAGNOSTICS PLAYGROUND ---
  const handleTestTranslation = async () => {
    if (!playText.trim()) return;
    setPlayBusy(true);
    try {
      const res = await api.testTranslation(playText.trim(), effectivePlayLang);
      setPlayResult(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setPlayBusy(false);
    }
  };

  // --- SCRIPTURE & GLOSSARY ---
  const handleSearchScripture = useCallback(async () => {
    setScriptureLoading(true);
    try {
      const items = await api.listScripture({
        kind: scriptureKind,
        q: scriptureSearch.trim() || undefined,
        limit: 30,
      });
      setScriptureList(items);
    } catch (e) {
      setError(String(e));
    } finally {
      setScriptureLoading(false);
    }
  }, [scriptureKind, scriptureSearch]);

  const handleApproveVerse = async (id: string) => {
    try {
      await api.approveScripture(id);
      flash("Scripture reference approved! It is now reachable by live router.");
      await handleSearchScripture();
    } catch (e) {
      setError(String(e));
    }
  };

  const loadGlossary = useCallback(async () => {
    try {
      const terms = await api.glossary();
      setGlossaryTerms(terms);
    } catch (e) {
      // ignore
    }
  }, []);

  const handleAddGlossary = async () => {
    if (!newTermAr.trim()) return;
    try {
      const tr: Record<string, string> = {};
      if (newTermIt.trim()) tr.it = newTermIt.trim();
      if (newTermEn.trim()) tr.en = newTermEn.trim();
      await api.addGlossary(newTermAr.trim(), tr);
      flash(`Term "${newTermAr}" added to glossary.`);
      setNewTermAr("");
      setNewTermIt("");
      setNewTermEn("");
      await loadGlossary();
    } catch (e) {
      setError(String(e));
    }
  };

  const handleApproveTerm = async (id: string) => {
    try {
      await api.approveGlossary(id);
      flash("Glossary term approved (revision bumped).");
      await loadGlossary();
    } catch (e) {
      setError(String(e));
    }
  };

  // Load initial tab data
  useEffect(() => {
    if (tab === "sermon") void loadSermons();
    if (tab === "scripture") {
      void handleSearchScripture();
      void loadGlossary();
    }
  }, [tab, loadSermons, handleSearchScripture, loadGlossary]);

  const currentRoomCode = room?.code || (activeSession?.room_id ? "Active" : null);

  return (
    <div className={`dashboard-root ${darkMode ? "dark" : "light"}`}>
      {/* Top Navbar */}
      <header className="dash-nav">
        <div className="nav-brand">
          <span className="brand-badge">BAYAAN</span>
          <span className="brand-title">Mosque Operations Cockpit</span>
        </div>

        {/* System Health Indicators */}
        <div className="nav-indicators">
          <span className={`pill-badge ${summary?.mode === "full" ? "green" : "yellow"}`}>
            ● Mode: {summary?.mode?.toUpperCase() || "OK"}
          </span>
          <span className="pill-badge gray">
            MT: {summary?.config.translation_provider}
          </span>
          {summary?.config.tts_enabled && (
            <span className="pill-badge blue">TTS: Active</span>
          )}
        </div>

        {/* Quick Links & Theme Toggle */}
        <div className="nav-actions">
          {room?.code && (
            <>
              <a
                href={`/r/${room.code}`}
                target="_blank"
                rel="noreferrer"
                className="nav-btn"
                title="Open Worshipper View"
              >
                <Icon name="smartphone" size={14} /> Mobile View
              </a>
              <a
                href={`/projector/${room.code}`}
                target="_blank"
                rel="noreferrer"
                className="nav-btn"
                title="Open Projector Screen"
              >
                <Icon name="monitor" size={14} /> Projector Mode
              </a>
            </>
          )}
          {liveFeed.length > 0 && (
            <button
              className="nav-btn nav-btn-srt"
              onClick={handleExportSrt}
              title="Download Broadcast SRT Subtitles"
            >
              <Icon name="download" size={14} /> Export SRT
            </button>
          )}
          <button className="theme-toggle" onClick={toggleDarkMode} title="Toggle Dark/Light AV Mode">
            {darkMode ? (
              <>
                <Icon name="sun" size={14} /> Light
              </>
            ) : (
              <>
                <Icon name="moon" size={14} /> Dark AV
              </>
            )}
          </button>
          <button
            className="nav-btn nav-btn-danger"
            onClick={logoutOperator}
            title={`Signed in as ${getStoredUsername() || "operator"} - Sign out`}
          >
            <Icon name="logout" size={14} /> Sign Out
          </button>
        </div>
      </header>

      {/* Notifications */}
      {notice && <div className="dash-alert notice">{notice}</div>}
      {error && <div className="dash-alert error">{error}</div>}

      {/* Main Tabs */}
      <nav className="dash-tabs">
        <button
          className={`tab-btn ${tab === "live" ? "active" : ""}`}
          onClick={() => setTab("live")}
        >
          <Icon name="radio" size={16} /> Live Broadcast Cockpit
        </button>
        <button
          className={`tab-btn ${tab === "sermon" ? "active" : ""}`}
          onClick={() => setTab("sermon")}
        >
          <Icon name="book-open" size={16} /> Khutbah Studio
        </button>
        <button
          className={`tab-btn ${tab === "diagnostics" ? "active" : ""}`}
          onClick={() => setTab("diagnostics")}
        >
          <Icon name="zap" size={16} /> AI Providers & Health
        </button>
        <button
          className={`tab-btn ${tab === "scripture" ? "active" : ""}`}
          onClick={() => setTab("scripture")}
        >
          <Icon name="scroll" size={16} /> Scripture & Glossary
        </button>
      </nav>

      {/* TAB 1: LIVE BROADCAST COCKPIT */}
      {tab === "live" && (
        <div className="tab-pane live-cockpit">
          {/* Cockpit Actions Header */}
          <div className="cockpit-bar card">
            <div className="cockpit-status">
              <span className="label">Session Status:</span>
              <span className={`status-badge ${(room?.status || activeSession?.status || "IDLE").toLowerCase()}`}>
                {room?.status || activeSession?.status || "IDLE"}
              </span>
              {currentRoomCode && (
                <span className="room-code-tag">Room: {currentRoomCode}</span>
              )}
            </div>

            {!room && !activeSession && (
              <div className="play-lang-select room-lang-pick">
                <span className="label">Languages for this room:</span>
                {targetCodes.map((code) => {
                  const on = effectiveRoomLangs.includes(code);
                  return (
                    <button
                      key={code}
                      className={`btn-sm ${on ? "active" : ""}`}
                      aria-pressed={on}
                      title={`${languageLabel(code)} (${code})`}
                      onClick={() =>
                        setRoomLangs(
                          on
                            ? effectiveRoomLangs.filter((c) => c !== code)
                            : [...effectiveRoomLangs, code]
                        )
                      }
                    >
                      {languageLabel(code)} ({code})
                    </button>
                  );
                })}
                {notConfiguredCodes.length > 0 && (
                  <span className="small muted">
                    More languages available in the Control Panel → Languages:{" "}
                    {notConfiguredCodes.join(", ")}
                  </span>
                )}
              </div>
            )}

            <div className="cockpit-buttons">
              {!room && !activeSession ? (
                <button
                  className="btn btn-primary"
                  onClick={handleStartRoom}
                  disabled={busy}
                >
                  <Icon name="sparkles" size={16} /> Start New Live Room
                </button>
              ) : (room?.status === "READY" || activeSession?.status === "READY" || room?.status === "CREATED") ? (
                <button
                  className="btn btn-live"
                  onClick={handleGoLive}
                  disabled={busy}
                >
                  <Icon name="dot" size={12} /> Go LIVE
                </button>
              ) : null}

              {(room?.status === "LIVE" || activeSession?.status === "LIVE") && (
                <button
                  className="btn btn-danger"
                  onClick={() => handleEndSession()}
                  disabled={busy}
                >
                  <Icon name="stop" size={14} /> End Broadcast
                </button>
              )}
            </div>
          </div>

          <div className="cockpit-grid">
            {/* Left Column: Room & Audio Ingest */}
            <div className="cockpit-left">
              {/* Room Card with QR */}
              {room && (
                <div className="card room-card">
                  <h3>Room Access & QR Code</h3>
                  <div className="qr-container">
                    {/* §197/ADR-0013: fetch the QR same-origin (relative path) so
                        the operator's own browser always reaches the backend via
                        the Vite proxy. room.qr_url is the absolute LAN URL (used
                        when BAYAAN_PUBLIC_BASE_URL points at a LAN IP); using it
                        directly here breaks the image on the operator's machine
                        because the dev server binds to 127.0.0.1 only. The QR
                        content still encodes the public LAN URL for phones. */}
                    <img src={`/api/v1/rooms/${room.code}/qr.svg`} alt="Room QR Code" className="qr-img" />
                  </div>
                  <div className="join-url-box">
                    <input type="text" readOnly value={room.join_url} className="input-text" />
                    <button
                      className="btn-sm"
                      onClick={() => {
                        navigator.clipboard.writeText(room.join_url);
                        flash("Link copied to clipboard!");
                      }}
                    >
                      Copy
                    </button>
                  </div>
                  <p className="small muted">
                    Worshippers on the mosque Wi-Fi scan this QR code or open the link to read live captions.
                  </p>
                </div>
              )}

              {/* Audio Ingest & VU Meter */}
              <div className="card audio-card">
                <h3>Audio Mixer & Microphone</h3>
                <div className="audio-mode-info">
                  <span className="small muted">
                  Mode: Browser PC Mic (16kHz mono)
                  {LiveArabicRecognizer.isSupported() && (
                    <span
                      className="small muted audio-warn-line"
                    >
                      <Icon name="alert" size={13} /> Browser transcription may send audio to the
                      browser vendor's cloud (Web Speech API); you will be asked before it starts.
                    </span>
                  )}
                </span>
                </div>

                {/* Real-time VU-Meter */}
                <VUMeter level={audioLevel} />

                <div className="audio-controls">
                  {!micActive ? (
                    <button
                      className="btn btn-primary full-width"
                      onClick={handleStartMic}
                    >
                      <Icon name="mic" size={16} /> Start PC Microphone
                    </button>
                  ) : (
                    <button
                      className="btn btn-warning full-width"
                      onClick={handleStopMic}
                    >
                      <Icon name="stop" size={14} /> Stop Microphone
                    </button>
                  )}
                </div>

                {micActive && (
                  <div className="speech-box">
                    <div className="speech-label">
                      <span className="dot live" />
                      <span>Transcribing Arabic Voice:</span>
                    </div>
                    <div className="speech-transcript" dir="rtl">
                      {speechText || "Listening... تكلم باللغة العربية"}
                    </div>
                  </div>
                )}
              </div>

              {/* PC Camera Capture Card (ADR-0015). Hidden entirely when the
                  server has the relay off, so no dead button is offered. */}
              {summary?.config.camera_enabled !== false && (
              <div className="card camera-card">
                <h3>
                  <Icon name="camera" size={18} /> PC Camera (relay)
                </h3>
                <p className="small muted">
                  Stream this PC's webcam to the projector / phones. Frames are
                  relayed through the server and shown at up to 10 fps — no
                  camera app needed.
                </p>
                {cameraPreview ? (
                  <video
                    ref={cameraPreviewRef}
                    autoPlay
                    muted
                    playsInline
                    className="camera-preview"
                  />
                ) : null}
                {!cameraActive ? (
                  <button
                    className="btn btn-primary full-width"
                    onClick={handleStartCamera}
                    disabled={!sessionIsLive || (!room?.session_id && !activeSession?.id)}
                    title={
                      sessionIsLive
                        ? "Relay this PC's webcam to the projector / phones"
                        : "The camera can only relay during a live session"
                    }
                  >
                    <Icon name="camera" size={16} /> Start PC Camera
                  </button>
                ) : (
                  <button
                    className="btn btn-warning full-width"
                    onClick={handleStopCamera}
                  >
                    <Icon name="stop" size={14} /> Stop Camera
                  </button>
                )}
                {/* ADR-0015: the relay only accepts a LIVE session, so say so
                    instead of letting the webcam switch on and back off. */}
                {!cameraActive && !sessionIsLive ? (
                  <p className="small muted mt-sm">
                    The camera relays only during a live session — click{" "}
                    <strong>Go LIVE</strong> first.
                  </p>
                ) : null}
                <p className="small muted mt-sm">
                  Tip: after saving, choose "PC camera (relay)" as the feed kind
                  on the "Live Video Stream Feed" card so the projector and
                  phones render this stream.
                </p>
              </div>
              )}

              {/* Video Stream Configuration Card */}
              <div className="card video-config-card">
                <h3>
                  <Icon name="video" size={18} /> Live Video Stream Feed
                </h3>
                <p className="small muted">
                  Link a live video broadcast (HLS, MP4, WebM, MJPEG, PC camera,
                  or embed) to display alongside subtitles on the projector
                  screen.
                </p>
                <div className="feed-url-row">
                  <input
                    type="text"
                    className="input-text"
                    placeholder={
                      videoFeedKind === "camera"
                        ? "No URL — derived from the room code"
                        : "https://stream.mosque.org/live/stream.m3u8"
                    }
                    value={videoFeedKind === "camera" ? "" : videoFeedUrl}
                    disabled={videoFeedKind === "camera"}
                    onChange={(e) => setVideoFeedUrl(e.target.value)}
                  />
                  <select
                    className="input-select"
                    value={videoFeedKind}
                    onChange={(e: any) => setVideoFeedKind(e.target.value as VideoFeedKind)}
                  >
                    <option value="hls">HLS (.m3u8)</option>
                    <option value="mp4">MP4</option>
                    <option value="webm">WebM</option>
                    <option value="mjpeg">MJPEG stream</option>
                    <option value="camera">PC camera (relay)</option>
                    <option value="iframe">Embed</option>
                  </select>
                </div>
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={videoFeedMobile}
                    onChange={(e) => setVideoFeedMobile(e.target.checked)}
                  />
                  <span className="small">Also show on worshipper phones (room page)</span>
                </label>
                <button
                  className="btn btn-secondary full-width"
                  onClick={handleSaveVideoFeed}
                  disabled={!room?.session_id && !activeSession?.id}
                >
                  Save Stream Feed
                </button>
              </div>

              {/* Operator Quick Utterances */}
              <div className="card demo-card">
                <h3>Operator Quick Utterance</h3>
                <p className="small muted">Send an announcement or test line directly to live attendees:</p>
                <div className="input-group">
                  <input
                    type="text"
                    className="input-text"
                    value={demoText}
                    dir="rtl"
                    onChange={(e) => setDemoText(e.target.value)}
                    placeholder="Enter Arabic text..."
                  />
                  <button
                    className="btn btn-secondary"
                    onClick={() => handleSendDemoLine()}
                    disabled={busy}
                  >
                    Send Line
                  </button>
                </div>
                <div className="preset-chips">
                  <button
                    className="chip"
                    onClick={() => setDemoText("بسم الله الرحمن الرحيم")}
                  >
                    Basmala
                  </button>
                  <button
                    className="chip"
                    onClick={() => setDemoText("الحمد لله رب العالمين")}
                  >
                    Hamd
                  </button>
                  <button
                    className="chip"
                    onClick={() => setDemoText("أيها الإخوة الكرام، السلام عليكم ورحمة الله")}
                  >
                    Greeting
                  </button>
                </div>
              </div>
            </div>

            {/* Right Column: Live Stream Monitor */}
            <div className="cockpit-right">
              <div className="card live-monitor-card">
                <div className="monitor-header">
                  <h3>Real-time Broadcast Feed</h3>
                  <span className="counter-badge">{liveFeed.length} captions</span>
                </div>

                {liveFeed.length === 0 ? (
                  <div className="monitor-empty">
                    <p className="muted">No captions broadcast yet.</p>
                    <p className="small muted">Start microphone or send a test line to see live translation stream.</p>
                  </div>
                ) : (
                  <div className="live-feed-list">
                    {liveFeed.slice().reverse().map((item) => (
                      <div key={item.sequence} className="feed-item">
                        <div className="feed-meta">
                          <span className="feed-seq">#{item.sequence}</span>
                          <span className="feed-time">
                            {new Date(item.createdAt).toLocaleTimeString()}
                          </span>
                        </div>
                        <div className="feed-arabic" dir="rtl">{item.arabic}</div>
                        <div className="feed-translations">
                          {Object.entries(item.targets).map(([langKey, target]) => (
                            <div key={langKey} className="feed-target">
                              <span className="lang-tag">{langKey.toUpperCase()}</span>
                              <span className={`target-text ${target.state === "unavailable" ? "fallback" : ""}`}>
                                {target.text || "..."}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 2: KHUTBAH STUDIO (SERMON PREP) */}
      {tab === "sermon" && (
        <div className="tab-pane sermon-studio">
          <div className="studio-top card">
            <div className="upload-section">
              <label className="btn btn-primary file-btn">
                <Icon name="file-text" size={16} /> Upload Sermon (.docx, .pdf, .md, .txt)
                <input
                  type="file"
                  ref={fileInputRef}
                  hidden
                  accept=".docx,.pdf,.md,.txt"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) void handleUploadSermon(f);
                  }}
                />
              </label>
              <div className="reviewer-box">
                <label className="small muted">Reviewer:</label>
                <input
                  type="text"
                  className="input-text-sm"
                  value={reviewerName}
                  onChange={(e) => setReviewerName(e.target.value)}
                />
              </div>
            </div>

            {selectedVersion && versionStatus !== "PUBLISHED" && (
              <div className="version-actions">
                <button
                  className="btn btn-secondary"
                  onClick={handleGenerateDrafts}
                  disabled={busy}
                >
                  <Icon name="zap" size={16} /> Generate Drafts
                </button>
                <button
                  className="btn btn-secondary"
                  onClick={handleApproveAll}
                  disabled={busy}
                >
                  <Icon name="check-circle" size={16} /> Approve All
                </button>
                <button
                  className="btn btn-live"
                  onClick={handlePublishVersion}
                  disabled={busy}
                >
                  <Icon name="lock" size={16} /> Publish Sermon (Immutable §41)
                </button>
              </div>
            )}
          </div>

          <div className="studio-grid">
            {/* Sermons List */}
            <div className="card sermon-sidebar">
              <h3>Uploaded Khutbahs</h3>
              <div className="sermon-list">
                {sermons.map((s) => (
                  <button
                    key={s.id}
                    className={`sermon-item ${selectedSermon === s.id ? "active" : ""}`}
                    onClick={() => openSermon(s.id)}
                  >
                    <div className="sermon-name">{s.title || s.source_file_name}</div>
                    <span className="badge gray">{s.status}</span>
                  </button>
                ))}
              </div>
            </div>

            {/* Version & Review Segments */}
            <div className="card sermon-segments-view">
              <div className="segments-header">
                <h3>
                  {selectedVersion ? `Reviewing Version (${versionStatus})` : "Select a sermon to review"}
                </h3>
              </div>

              <div className="panels-container">
                {panels.map((p) => (
                  <div key={p.segment.id} className="segment-card">
                    <div className="seg-top">
                      <span className="seg-num">#{p.segment.segment_order + 1}</span>
                      <span className="badge blue">{p.segment.content_type}</span>
                      {p.warnings.length > 0 && (
                        <span className="badge red" title={p.warnings.join(", ")}>
                          <Icon name="alert" size={12} /> {p.warnings.length} warning(s)
                        </span>
                      )}
                    </div>
                    <div className="seg-arabic" dir="rtl">{p.segment.arabic}</div>
                    <div className="seg-drafts">
                      {p.drafts.map((d) => (
                        <div key={d.id} className="draft-row">
                          <span className="lang-tag">{d.language.toUpperCase()}</span>
                          <span className="draft-text">{d.text}</span>
                          <span className={`pill-badge ${d.approved ? "green" : "yellow"}`}>
                            {d.approved ? "Approved" : "Pending"}
                          </span>
                          <button
                            className="btn-sm"
                            onClick={(e) => handleDeleteDraft(d.id, e)}
                            disabled={busy || deletingDraftId === d.id}
                            title="Delete draft"
                          >
                            <Icon name="trash" size={14} />
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 3: AI PROVIDERS & HEALTH */}
      {tab === "diagnostics" && (
        <div className="tab-pane diagnostics-view">
          <div className="diagnostics-grid">
            {/* Interactive Playground */}
            <div className="card">
              <h3>
                <Icon name="flask" size={18} /> Interactive Translation Playground
              </h3>
              <p className="small muted">
                Test the live provider chain (Ollama Cloud / Ollama / Libre) and measure roundtrip latency:
              </p>
              <div className="play-input-box">
                <textarea
                  className="input-textarea"
                  value={playText}
                  dir="rtl"
                  rows={3}
                  onChange={(e) => setPlayText(e.target.value)}
                />
                <div className="play-row">
                  <div className="play-lang-select">
                    <label className="small muted">Target:</label>
                    {targetCodes.map((code) => (
                      <button
                        key={code}
                        className={`btn-sm ${effectivePlayLang === code ? "active" : ""}`}
                        onClick={() => setPlayLang(code)}
                      >
                        {languageLabel(code)} ({code})
                      </button>
                    ))}
                  </div>
                  <button
                    className="btn btn-primary"
                    onClick={handleTestTranslation}
                    disabled={playBusy}
                  >
                    {playBusy ? (
                      "Translating..."
                    ) : (
                      <>
                        <Icon name="zap" size={16} /> Run Test
                      </>
                    )}
                  </button>
                </div>
              </div>

              {playResult && (
                <div className="play-result-box">
                  <div className="result-header">
                    <span className="pill-badge green">{playResult.provider}</span>
                    <span className="pill-badge gray">{playResult.model}</span>
                    <span className="pill-badge blue">
                      <Icon name="timer" size={12} /> {playResult.latency_ms} ms
                    </span>
                  </div>
                  <div className="result-text">{playResult.text}</div>
                </div>
              )}
            </div>

            {/* ADR-0017: WHY lines were held back - operator-only (§98/§168). */}
            <div className="card">
              <h3>
                <Icon name="lock" size={18} /> Safety Holds — why a line was held back
              </h3>
              <p className="small muted">
                Worshippers only ever see the safe fallback text. These are the
                reasons BAYAAN recorded for holding a line back — visible to you,
                never to the room (§98/§168). Safety itself is unchanged.
              </p>
              {safetyBlocks.length === 0 ? (
                <p className="small muted">No held-back lines recorded yet.</p>
              ) : (
                <div className="circuits-table">
                  {safetyBlocks.map((block) => (
                    <div key={block.id} className="circuit-row">
                      <span className="c-name">
                        {block.action === "SAFETY_BLOCK" ? "Blocked" : "Audio held"}
                        {block.metadata.sequence !== undefined
                          ? ` · line #${block.metadata.sequence}`
                          : ""}
                        {block.metadata.lang
                          ? ` · ${languageLabel(block.metadata.lang)}`
                          : ""}
                      </span>
                      <span className="small muted">
                        {block.metadata.reason ?? block.metadata.raw ?? ""}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              <button className="btn-sm mt-sm" onClick={() => void loadSafetyBlocks()}>
                Refresh
              </button>
            </div>

            {/* Provider Circuit Breakers & Configuration */}
            <div className="card">
              <h3>
                <Icon name="zap" size={18} /> Provider Circuit Breakers & Tuning
              </h3>
              <div className="circuits-table">
                {summary &&
                  Object.entries(summary.providers).map(([cap, chain]) => (
                    <div key={cap} className="circuit-group">
                      <h4>{cap.toUpperCase()} Chain</h4>
                      {chain.map((c) => (
                        <div key={c.provider} className="circuit-row">
                          <span className="c-name">{c.provider}</span>
                          <span className={`pill-badge ${c.state === "closed" ? "green" : "red"}`}>
                            {c.state.toUpperCase()}
                          </span>
                        </div>
                      ))}
                    </div>
                  ))}
              </div>

              <div className="config-overview">
                <h4>Active Deployment Tuning</h4>
                <ul className="config-list">
                  <li>Translation: <strong>{summary?.config.translation_provider}</strong></li>
                  <li>Fallback: <strong>{summary?.config.translation_fallback || "None"}</strong></li>
                  <li>TTS Spoken Audio: <strong>{summary?.config.tts_enabled ? "Enabled" : "Disabled (Text-First)"}</strong></li>
                  <li>Audio Ingest: <strong>{summary?.config.audio_capture_mode}</strong></li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 4: SCRIPTURE & GLOSSARY */}
      {tab === "scripture" && (
        <div className="tab-pane scripture-view">
          <div className="scripture-grid">
            {/* Scripture Store Search */}
            <div className="card scripture-search-card">
              <h3>📖 Tanzil Quran & Hadith Repository</h3>
              <p className="small muted">
                Verified scripture references used for live sacred text matching (§78/§206):
              </p>
              <div className="search-bar">
                <select
                  value={scriptureKind}
                  onChange={(e) => setScriptureKind(e.target.value)}
                  className="select-sm"
                >
                  <option value="QURAN">Quran</option>
                  <option value="HADITH">Hadith</option>
                </select>
                <input
                  type="text"
                  className="input-text"
                  placeholder="Search Surah:Ayah (e.g. 2:255) or Arabic text..."
                  value={scriptureSearch}
                  onChange={(e) => setScriptureSearch(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleSearchScripture()}
                />
                <button
                  className="btn btn-secondary"
                  onClick={handleSearchScripture}
                  disabled={scriptureLoading}
                >
                  Search
                </button>
              </div>

              <div className="scripture-table-wrap">
                <table className="dash-table">
                  <thead>
                    <tr>
                      <th>Ref</th>
                      <th>Arabic</th>
                      <th>Italian (Piccardo)</th>
                      <th>Status</th>
                      <th>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {scriptureList.map((v) => (
                      <tr key={v.id}>
                        <td><strong>{v.key}</strong></td>
                        <td dir="rtl">{v.normalized_arabic || v.key}</td>
                        <td>{v.translations?.it || "—"}</td>
                        <td>
                          <span className={`pill-badge ${v.approved ? "green" : "yellow"}`}>
                            {v.approved ? "Approved" : "Pending"}
                          </span>
                        </td>
                        <td>
                          {!v.approved && (
                            <button
                              className="btn-sm"
                              onClick={() => handleApproveVerse(v.id)}
                            >
                              Approve
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Sacred Glossary Governance */}
            <div className="card glossary-card">
              <h3>
                <Icon name="bookmark" size={18} /> Sacred Islamic Glossary
              </h3>
              <p className="small muted">
                Strict glossary terms to prevent machine translation drift (§22/§180):
              </p>
              <div className="add-glossary-box">
                <input
                  type="text"
                  className="input-text-sm"
                  placeholder="Arabic (e.g. تقوى)"
                  value={newTermAr}
                  onChange={(e) => setNewTermAr(e.target.value)}
                />
                <input
                  type="text"
                  className="input-text-sm"
                  placeholder="Italian (e.g. Timore di Dio)"
                  value={newTermIt}
                  onChange={(e) => setNewTermIt(e.target.value)}
                />
                <input
                  type="text"
                  className="input-text-sm"
                  placeholder="English (e.g. Piety)"
                  value={newTermEn}
                  onChange={(e) => setNewTermEn(e.target.value)}
                />
                <button className="btn btn-primary btn-sm" onClick={handleAddGlossary}>
                  + Add Term
                </button>
              </div>

              <div className="glossary-list">
                {glossaryTerms.map((t) => (
                  <div key={t.id} className="glossary-item">
                    <div className="term-ar" dir="rtl">{t.term_ar}</div>
                    <div className="term-tr">
                      <span>IT: {t.translations.it || "—"}</span>
                      <span>EN: {t.translations.en || "—"}</span>
                    </div>
                    <div className="term-actions">
                      <span className={`pill-badge ${t.approved ? "green" : "yellow"}`}>
                        r{t.revision} {t.approved ? "Approved" : "Draft"}
                      </span>
                      {!t.approved && (
                        <button
                          className="btn-sm"
                          onClick={() => handleApproveTerm(t.id)}
                        >
                          Approve
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
