/* Typed client for the sermon preparation + review API (§117, §96, §192).
   Same-origin fetch only (CSP connect-src 'self', §114). */

export type Sermon = {
  id: string;
  title: string;
  source_file_name: string;
  status: string;
  imam_ref: string | null;
};

export type SermonVersion = {
  id: string;
  sermon_id: string;
  version: number;
  status: string;
  content_hash: string;
  approved_at: string | null;
  approved_by: string | null;
};

export type SermonSegment = {
  id: string;
  sermon_version_id: string;
  segment_order: number;
  arabic: string;
  normalized_arabic: string;
  content_type: string;
  source_ref: string | null;
};

export type Draft = {
  id: string;
  segment_id: string;
  language: string;
  text: string;
  provider: string;
  model: string;
  prompt_version: string;
  glossary_version: string;
  retrieval_version: string;
  approved: boolean;
  approved_by: string | null;
  approved_at: string | null;
  corrected_from: string | null;
  correction_reason: string | null;
};

export type ReviewPanel = {
  segment_order: number;
  content_type: string;
  arabic: string;
  drafts: Draft[];
  glossary_hits: string[];
  warnings: string[];
  glossary_version: string;
};

export type GlossaryTerm = {
  id: string;
  term_ar: string;
  normalized_ar: string;
  translations: Record<string, string>;
  approved: boolean;
  revision: number;
};

export type RoomStart = {
  room_id: string;
  code: string;
  session_id: string;
  status: string;
  languages: string[];
  qr_url: string;
  join_url: string;
  // ADR-0014 PC audio input: server tells the operator which input path is
  // configured, so the UI offers "use this PC's microphone" only when it works.
  capture_enabled?: boolean;
  capture_mode?: string;
  ingest_ws_enabled?: boolean;
};

export type SessionInfo = {
  id: string;
  room_id: string;
  sermon_version_id: string | null;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  active_languages: string[];
  video_feed_url?: string | null;
  video_feed_kind?: string | null;
  // ADR-0015: also shown on the worshipper room page (phones).
  video_feed_mobile?: boolean;
};

export type ScriptureVerse = {
  id: string;
  kind: string;
  key: string;
  surah: string;
  ayah: string;
  normalized_arabic: string;
  translations: Record<string, string>;
  edition: string;
  license: string;
  approved: boolean;
};

export type SystemSummary = {
  status: string;
  mode: string;
  degraded_reason: string | null;
  active_sessions_count: number;
  providers: Record<string, Array<{ provider: string; state: string }>>;
  // ADR-0017: every language picker in the operator UI is driven by this -
  // `targets` is what the server is configured for, `catalog` is the curated
  // list the admin can enable in the Control Panel.
  languages?: {
    source: string;
    targets: Array<{ code: string; label: string }>;
    catalog: Array<{ code: string; label: string; english: string }>;
  };
  config: {
    asr_provider: string;
    translation_provider: string;
    translation_fallback: string;
    tts_provider: string;
    tts_enabled: boolean;
    audio_capture_enabled: boolean;
    audio_capture_mode: string;
    audio_input_device: string;
    // ADR-0015: false = the PC camera relay is off on the server (hide the card).
    camera_enabled?: boolean;
  };
};

export type CameraReady = {
  ready: boolean;
  /** The camera WS close code this verdict corresponds to (4432/4404/4409). */
  code: number | null;
  reason: string;
};

export type SafetyBlockEntry = {
  id: string;
  action: string;
  target_id: string | null;
  timestamp: string | null;
  metadata: {
    sequence?: number;
    lang?: string;
    reason?: string;
    capability?: string;
    raw?: string;
  };
};

export type TestTranslationResult = {
  success: boolean;
  text: string;
  target_language: string;
  confidence: number | null;
  provider: string;
  model: string;
  latency_ms: number;
  error?: string;
};

export type LiveSegmentItem = {
  id: string;
  session_id: string;
  sequence: number;
  raw_arabic: string;
  normalized_arabic: string;
  content_type: string;
  source_segment_id: string | null;
  asr_confidence: number;
  alignment_confidence: number;
};


import { getStoredToken, logoutOperator } from "./auth";

async function authFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const token = getStoredToken();
  const headers = new Headers(init?.headers || {});
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(input, { ...init, headers });
  if (response.status === 401) {
    // Only redirect if not already on the login page
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      logoutOperator();
    }
  }
  return response;
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const api = {
  listSermons: () => authFetch("/api/v1/sermons").then((r) => j<Sermon[]>(r)),
  versions: (sermonId: string) =>
    authFetch(`/api/v1/sermons/${sermonId}/versions`).then((r) =>
      j<SermonVersion[]>(r)
    ),
  segments: (versionId: string) =>
    authFetch(`/api/v1/sermons/versions/${versionId}/segments`).then((r) =>
      j<SermonSegment[]>(r)
    ),
  generateDrafts: (versionId: string) =>
    authFetch(`/api/v1/sermons/versions/${versionId}/drafts`, { method: "POST" }).then(
      (r) =>
        j<{
          version_id: string;
          drafts: number;
          created: number;
          skipped: number;
          total: number;
        }>(r)
    ),
  reviewPanel: (segmentId: string) =>
    authFetch(`/api/v1/sermons/segments/${segmentId}/review`).then((r) =>
      j<ReviewPanel>(r)
    ),
  editDraft: (
    draftId: string,
    body: { text: string; corrected_by?: string; correction_reason?: string }
  ) =>
    authFetch(`/api/v1/sermons/drafts/${draftId}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<Draft>(r)),
  deleteDraft: (draftId: string) =>
    authFetch(`/api/v1/sermons/drafts/${draftId}`, { method: "DELETE" }).then(
      (r) => {
        if (r.status === 204) return null;
        return j<null>(r);
      }
    ),
  approveDraft: (draftId: string, approved_by: string) =>
    authFetch(`/api/v1/sermons/drafts/${draftId}/approve`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ approved_by }),
    }).then((r) => j<Draft>(r)),
  approveAll: (versionId: string, approved_by: string) =>
    authFetch(`/api/v1/sermons/versions/${versionId}/approve-all`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ approved_by }),
    }).then((r) => j<{ version_id: string; approved: number }>(r)),
  deleteSermon: (id: string) =>
    authFetch(`/api/v1/sermons/${id}`, { method: "DELETE" }).then((r) => {
      if (r.status === 204) return null;
      return j<null>(r);
    }),
  transitionVersion: (versionId: string, status: string, approved_by: string) =>
    authFetch(`/api/v1/sermons/versions/${versionId}/status`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ status, approved_by }),
    }).then((r) => j<SermonVersion>(r)),
  upload: (file: File, title: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("title", title);
    return authFetch(`/api/v1/sermons/upload?title=${encodeURIComponent(title)}`, {
      method: "POST",
      body: form,
    }).then((r) => j<SermonVersion>(r));
  },
  glossary: () => authFetch("/api/v1/glossary/terms").then((r) => j<GlossaryTerm[]>(r)),
  addGlossary: (term_ar: string, translations: Record<string, string>) =>
    authFetch("/api/v1/glossary/terms", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ term_ar, translations }),
    }).then((r) => j<GlossaryTerm>(r)),
  approveGlossary: (id: string) =>
    authFetch(`/api/v1/glossary/terms/${id}/approve`, { method: "POST" }).then((r) =>
      j<GlossaryTerm>(r)
    ),
  // §200 operator quick-start: one call -> room + CREATED session, no khutbah.
  startRoom: (active_languages?: string[]) =>
    authFetch("/api/v1/rooms/start", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(active_languages ? { active_languages } : {}),
    }).then((r) => j<RoomStart>(r)),
  activeSessions: () =>
    authFetch("/api/v1/sessions/active").then((r) => j<SessionInfo[]>(r)),
  setSessionStatus: (sessionId: string, status: string) =>
    authFetch(`/api/v1/sessions/${sessionId}/status`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ status }),
    }).then((r) => j<SessionInfo>(r)),
  setVideoFeed: (
      sessionId: string,
      url: string | null,
      kind: string = "hls",
      mobile: boolean = false
    ) =>
      authFetch(`/api/v1/sessions/${sessionId}/video-feed`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url, kind, mobile }),
      }).then((r) => j<SessionInfo>(r)),
  // §200: push a scripted utterance through the real pipeline (no mic/ASR) so
  // captions + live translation appear on the room page for a demo / when
  // troubleshooting. Requires the session to be LIVE; runs the full safety path.
  sendDemoLine: (sessionId: string, arabic: string) =>
    authFetch(`/api/v1/sessions/${sessionId}/demo-line`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ arabic }),
    }).then((r) =>
      j<{
        id: string;
        session_id: string;
        sequence: number;
        raw_arabic: string;
        normalized_arabic: string;
        content_type: string;
        asr_confidence: number;
        alignment_confidence: number;
      }>(r)
    ),
  sessionSegments: (sessionId: string) =>
    authFetch(`/api/v1/sessions/${sessionId}/segments`).then((r) =>
      j<LiveSegmentItem[]>(r)
    ),
  // ADR-0015: pre-flight for the PC camera relay - answers "will the camera WS
  // accept right now, and if not why" BEFORE getUserMedia switches the webcam
  // on (a rejected relay used to look like "camera starts then closes").
  cameraReady: (sessionId: string) =>
    authFetch(`/api/v1/sessions/${sessionId}/camera/ready`).then((r) =>
      j<CameraReady>(r)
    ),
  systemSummary: () =>
    authFetch("/api/v1/diagnostics/system-summary").then((r) =>
      j<SystemSummary>(r)
    ),
  // ADR-0017: operator-only explanation of WHY a line was held back (§98/§168).
  // The room itself only ever sees the §134 fallback message.
  safetyBlocks: (limit = 20) =>
    authFetch(`/api/v1/diagnostics/safety-blocks?limit=${limit}`).then((r) =>
      j<{ blocks: SafetyBlockEntry[]; count: number }>(r)
    ),
  testTranslation: (text: string, target_language: string) =>
    authFetch("/api/v1/diagnostics/test-translation", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, target_language }),
    }).then((r) => j<TestTranslationResult>(r)),
  listScripture: (params?: {
    kind?: string;
    q?: string;
    limit?: number;
    offset?: number;
  }) => {
    const sp = new URLSearchParams();
    if (params?.kind) sp.set("kind", params.kind);
    if (params?.q) sp.set("q", params.q);
    if (params?.limit !== undefined) sp.set("limit", String(params.limit));
    if (params?.offset !== undefined) sp.set("offset", String(params.offset));
    const qs = sp.toString();
    return authFetch(`/api/v1/scripture${qs ? `?${qs}` : ""}`).then((r) =>
      j<ScriptureVerse[]>(r)
    );
  },
  approveScripture: (id: string) =>
    authFetch(`/api/v1/scripture/${id}/approve`, { method: "POST" }).then((r) =>
      j<ScriptureVerse>(r)
    ),
  preflight: () => authFetch("/api/v1/preflight").then((r) => j<any>(r)),
  health: () => fetch("/health").then((r) => j<any>(r)),
};

