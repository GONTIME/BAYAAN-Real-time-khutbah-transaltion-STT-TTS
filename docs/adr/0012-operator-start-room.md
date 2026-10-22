# ADR-0012: Operator one-click "Start Room" (no khutbah text required)

**Status:** Accepted

**Context:** The reviewer console (`/admin`, §96) prepares and pre-approves a
khutbah so live captions can be aligned against it (§77). But that is the
*high-fidelity* path, not the only one: a live session has always been able to
run with `sermon_version_id=None`, in which case captions come straight from the
MT provider chain (§78/§86) instead of prepared drafts. Preparing a sermon is
minutes of work, yet a common real need is simpler: **the imam is ready now and
just wants a live room with captions** — a study circle, a jumu'ah run without a
pre-loaded text, a test, or a spontaneous gathering. Before this ADR there was
no operator surface for that: rooms and sessions could only be created through
the raw REST API or a tool, and there was no page to drive them.

**Decisions:**

1. **One new convenience endpoint, zero new business logic.** `POST
   /api/v1/rooms/start` creates a room and a `CREATED` session in a single
   transaction, with `sermon_version_id=None` (so captions use the existing MT
   chain — the same path a sermon-less session already takes). It reuses the
   existing `RoomRepository`, `LiveSessionRepository`, the §149 concurrency
   bound, the `admin` rate-limit bucket (§115) and the §98 audit trail
   (`SESSION_STARTED`). It adds no rule, bypasses no safety gate, and does not
   touch the `LiveSession` domain model (§64). This is §0 rule 4/§0 rule 5
   compliant: selection is configuration, nothing unsafe is auto-broadcast.

2. **`active_languages` defaults to config, and unknown codes are rejected.**
   Omitting the field uses `settings.languages.target_languages` (§63). Any
   explicitly supplied language must be a subset of the configured targets —
   an unknown code returns `422` rather than silently starting a room that
   broadcasts a language nobody can read (§185: no silent assumption).

3. **The response is operator-ready.** It returns `room_id`, `code`,
   `session_id`, `status`, `languages`, and fully-formed `qr_url` and
   `join_url`, so the caller needs no second request to show a scannable QR
   and a join link. The QR/join URLs point at the existing §197 QR route and
   the `/r/<code>` worshipper view — nothing new is mounted.

4. **The mosque-operations cockpit drives the flow.** The operator quick-start
   lives in `frontend/src/Dashboard.tsx`, served by the SPA fallback at
   `/`, `/admin`, and `/operator` (BUG-023: the separate `OperatorPage.tsx`/
   `AdminPage.tsx` pages were dead code and were removed in the BUG-023 fix).
   has a **Start Room** button → shows the code + QR + join link, a **Go live**
   button that walks the §110 lifecycle `CREATED → READY → LIVE`, and a live
   list of active sessions with an **End** action. It is purely additive: the
   reviewer console (`/admin`) and the worshipper room page are untouched. It
   inherits the operator-authenticated posture (ADR-0004 as updated by BUG-001/
   BUG-002: operator token or Basic auth required by the API and WebSocket
   ingest), so the cockpit must only be opened on the trusted operator
   PC — never shared publicly.

5. **A bounded active-sessions listing.** `GET /api/v1/sessions/active` returns
   the `READY`/`LIVE` sessions for the operator dashboard, capped by
   `max_active_sessions` (§149) so the list is never unbounded. It is registered
   before the `/{session_id}` route so the literal `active` path is not shadowed.

**Consequences / follow-ups:**
- Operators can start a captioned room in one click without preparing a khutbah
  first — the same MT chain, with no fidelity regression on the general path.
- The sacred-text guarantees are unchanged: Quran stays SQL-retrieval-only
  (§206) and is never machine-translated, so a sermon-less room is still safe.
- `/operator` shares `/admin`'s operator-authenticated posture; both need operator-only
  network access until auth lands (a known pilot blocker, ADR-0004).
- `tests/test_operator_start.py` pins the whole flow: room + `CREATED` session
  with no sermon version, discoverability + QR rendering, custom/unknown
  languages, the §149 concurrency bound, the `admin` rate limit, and the active
  listing.
