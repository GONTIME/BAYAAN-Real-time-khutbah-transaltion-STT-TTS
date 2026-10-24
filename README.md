# BAYAAN
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688?logo=fastapi)](https://fastapi.tiangolo.com/)
[![React + Vite](https://img.shields.io/badge/frontend-React%20%2B%20Vite-61DAFB?logo=react)](https://vitejs.dev/)
[![SQLite](https://img.shields.io/badge/database-SQLITE-07405E?logo=sqlite)](https://www.sqlite.org/)
[![Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows)](https://www.microsoft.com/windows)
[![License: TBD](https://img.shields.io/badge/license-TBD-lightgrey)](LICENSE)

Real-time sermon translation platform for mosques. BAYAAN takes Arabic speech from the digital mixer, transcribes it, aligns it against approved prepared sermons, translates it into the
configured target languages (curated catalog, ADR-0017: `it en fr ur id bn fa ps tr sw de es`), safety-validates every caption, and broadcasts live captions with optional gated
text-to-speech to worshippers' phones, projectors, and headsets over the local network. Operators can additionally mirror every finalized caption line to a Telegram channel
(ADR-0016) and see exactly why a line was withheld (ADR-0017).

---
## About
BAYAAN is built for Friday khutbahs and mosque events where worshippers need live translated captions on their own phones. An operator PC near the sound mixer runs the backend and the web frontend; phones on the same Wi-Fi scan a QR code and read captions in their language, with no app install required. 
Core design principles:
- **Text-first:** captions are the product; audio/TTS stays locked unless the operator explicitly enables it.
- **Safety by default:** no unverified Quranic content is ever machine-translated or broadcast; low-confidence output is never auto-broadcast.
- **Offline-capable:** after the one-time dependency installation, the demo pipeline runs on the local network without internet.
- **No magic values:** every threshold is configured, validated, and versioned (`BAYAAN_*` environment variables).


---
## Key Features
| Area | Capability |
|------|------------|
| **Live captions** | Arabic ASR → normalization → segment detection → translation routing → safety validation → WebSocket broadcast to phones/projector/headsets. |
| **Language catalog** | Curated target list (12 languages) validated at startup; each room picks its own targets, and every rejection names the valid codes — never a bare `422` (ADR-0017). |
| **Prepared sermons** | Upload txt/md/docx/pdf, semantic segmentation, translation drafts with full provenance, glossary governance, reviewer edit/approve, immutable publish. |
| **Rooms & QR** | One-click live room creation, room code + QR + join link, visitor tokens, rate limiting, security headers, caption history replay. Closing a room returns the cockpit to IDLE immediately (ADR-0018). |
| **Video (optional)** | HLS broadcast, MJPEG camera, embed, or PC webcam feed on the projector and (optionally) phones — operator-controlled, LIVE-session gated, with a pre-flight that explains a refusal before the webcam opens (ADR-0015). |
| **Telegram mirror (optional)** | Mirror every finalized caption line (source + translated) to a bot chat or channel: off by default, bounded queue, retries/429 handling, token never logged, and a `test-telegram` endpoint to verify setup (ADR-0016). |
| **Operator diagnostics** | Provider latency, language preflight, Telegram test message, camera readiness, and admin-only safety-hold explanations (which signal withheld a line) — the safety gate itself is untouched (ADR-0017). |
| **Resilience** | Per-capability failover chains, circuit breakers, failover budgets, watchdog heartbeats, degraded mode with a public fallback — never a crash, never fabricated output. |
| **Accessibility** | Arabic language toggle, right-to-left support, §134 public fallback display, and a projector surface that matches the phone view (ADR-0018). |

---
## Architecture
```
Microphone → Mixer → Audio Capture → VAD/DSP → Arabic ASR
→ Normalization → Segment Detection → Prepared Alignment / Religious Retrieval
→ Translation Routing → Safety Validation → Captions → (optional) TTS
→ WebSocket → PWA / Projector / Headset clients
```
Layers (domain → db → providers + ops → api):
- **domain** — pure contracts
- **db** — repositories only
- **providers** — ports + adapters; all network I/O isolated
- **ops** — resilience: alerts, health, watchdog
- **api** — FastAPI HTTP + WebSocket surface
Every provider call crosses a failover layer first: per-capability chains (primary + fallback) guarded by circuit breakers, with time budgets and `SAFE_UNAVAILABLE` when a chain is exhausted. A watchdog polls capability heartbeats and soft-reboots stale chains; degraded mode is surfaced through `/health` and preflight checks.
For the full architecture narrative, see [`docs/adr/`](docs/adr).

---
## Interfaces
| Surface | URL | Notes |
|---------|-----|-------|
| Operator console | `/` (or `/dashboard`) | Login required (default `admin` / `admin`). Live cockpit, sermon studio, diagnostics, Control Panel. |
| Worshipper mobile view | `/r/<CODE>` | No login. Language switcher, Arabic toggle, optional audio, optional video, caption history. |
| Projector wall display | `/projector/<CODE>` | Large-font full screen for projectors and TV walls; same light surface as the phone view (ADR-0018). |
| Room discovery | `GET /api/v1/rooms/<CODE>` | Visitor token, room languages, video feed — used by both viewer surfaces. |
| Diagnostics | `/api/v1/diagnostics/*` | `system-summary`, `preflight`, `test-translation`, `safety-blocks`, `test-telegram` (admin auth). |
| Control Panel | `python tools/control_panel.py` → `http://127.0.0.1:8765` | GUI for every `BAYAAN_*` setting, plus start/stop of backend and frontend. |

---
## Tech Stack
**Backend** (`backend/`)
- Python 3.11+, FastAPI, Uvicorn, Pydantic v2, pydantic-settings
- SQLAlchemy 2.0 (async) + aiosqlite (SQLite); `asyncpg` optional for Postgres
- `pypdf` for sermon ingestion; `qrcode` for room QR codes; `sounddevice` optional for PC audio
- pytest + pytest-asyncio + httpx for testing (stdlib-only control panel tool)
- Optional Telegram caption mirror built on the standard library (`urllib`) — no extra dependency
**Frontend** (`frontend/`)
- React 18, TypeScript, Vite
- hls.js for video feeds; PWA with manifest + service worker
**Infrastructure**
- Windows 10/11 operator PC; phones connect over LAN Wi-Fi
- Docker support via `docker-compose.yml`


---
## Prerequisites
- **Windows PC** (Windows 10 or 11) — the guide is written for Windows
- **Python 3.11 or newer** (with `python.exe` on PATH)
- **Node.js 18 or newer** (only for the website/frontend part)
- **Google Chrome or Microsoft Edge**
- Internet for the one-time installations; after that the demo works offline on LAN
Verify with:
```powershell
python --version
node --version
npm --version
```
---
## Quick Start
1. Clone the repo:
```powershell
git clone <repo-url>
cd BAYAAN-Real-time-khutbah-transaltion-STT-TTS
```
2. Backend (the brain):
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
# edit backend/.env as needed (see .env.example)
python -m uvicorn bayaan_server.api.app:create_app --factory --reload
```
3. Frontend (the face), in a second terminal:
```powershell
cd frontend
npm install          # if npm complains about peer dependencies (Vite 8): npm install --legacy-peer-deps
npm run dev
```
4. Open `http://localhost:5173/` in your browser, sign in to the operator console (default `admin` / `admin` — change before any real event), create a room, and share the QR code with worshippers on the same Wi-Fi.
The easiest Windows path is to double-click **`Start Bayaan.bat`** at the repository root, or run the built-in Control Panel
(`python tools/control_panel.py` → `http://127.0.0.1:8765`), which starts/stops both servers and edits every setting with validation.

---
## How to Use
Step-by-step, click-by-click instructions for non-technical users live in the separate guide **`HOW_TO_RUN_AND_USE_BAYAAN.md`** — it covers the pre-flight checklist, starting the brain and the website, logging in, uploading and publishing sermons, creating rooms with QR codes, picking the room's languages, adding optional video/PC-camera feeds, the Telegram mirror, troubleshooting, and the Friday operator checklist.
To keep this README focused, usage details are **not duplicated here**; refer to that guide for the complete operator workflow.

---
## Configuration
Runtime settings live in `backend/.env` — copy `backend/.env.example` and edit it, or use the Control Panel, which lists every key with its
description, validates before saving and can restart the servers. Every key the code reads is listed in `.env.example` (audited against the
settings model, so the reference cannot silently drift).
Highlights:
- `BAYAAN_LANGUAGES__TARGET_LANGUAGES` — the targets a room may broadcast, picked from the curated catalog (`it en fr ur id bn fa ps tr sw de es`); unknown codes are rejected with the valid list (ADR-0017).
- `BAYAAN_TELEGRAM__*` — optional caption mirror. Needs BOTH `BOT_TOKEN` and `CHAT_ID`; the chat id must not be the bot's own `@username`.
- `BAYAAN_CAMERA__*` — PC camera relay (LIVE-session gated; the UI pre-flights before opening the webcam).
- `BAYAAN_SAFETY__*` / `BAYAAN_SCRIPTURE__*` — the safety gate. `BAYAAN_SAFETY__TTS_ENABLED` (default `false`) is the only switch that turns spoken audio on.
- `BAYAAN_PUBLIC_BASE_URL` — set it to your PC's LAN address so room QRs and join links work from phones on the same Wi-Fi.

---
## Testing
Run the backend test suite from `backend/` with the virtualenv activated:
```powershell
python -m pytest -q          # 405 tests
```
Frontend checks:
```powershell
cd frontend
npx tsc --noEmit             # type-check
npm run build                # production bundle
```
---
## Security & Privacy
- **Operator login** protects the console (`/admin`); worshipper room pages (`/r/<CODE>`) need no login. Never share operator credentials publicly.
- **Text-first:** TTS is locked by default; only safety-approved, non-stale captions are ever synthesized or broadcast (§80 gate runs at enqueue and again at dequeue).
- **Audio events carry PCM bytes only** — no raw transcript, no identity.
- **Degraded mode:** provider outages are absorbed by failover; exhausted chains degrade gracefully with a public fallback — never a crash, never fabricated output.
- **Religious safety:** unverified Quran is blocked (never machine-translated); Hadith routes to approved-or-general non-attributed text.
- **Safety holds are explainable, not loosened:** `/api/v1/diagnostics/safety-blocks` (admin only) reports which signal withheld a line; the gate itself is unchanged (ADR-0017).
- **Telegram mirror is opt-in and output-only:** it sends what the room already published (blocked lines appear as the public fallback text, never an internal reason); the bot token is never logged or returned by any endpoint (ADR-0016).
- **PC camera relay is LIVE-session gated:** the server answers a readiness pre-flight before the webcam is opened, viewers are visitor-token gated and capped per session, and the MJPEG route 404s when the relay is off (ADR-0015).

---
## Code of Conduct
Please be respectful and inclusive in issues, pull requests, and discussions. This project serves houses of worship; keep contributions focused on reliability, safety, and accessibility for that context.

---
## Roadmap & Status
Implementation is organized by phases:
See [`docs/adr/`](docs/adr) (0001–0018) for the decisions behind each phase.

Recent changes:
- **ADR-0016 — Telegram caption mirror (optional).** Every finalized caption line can be mirrored to a bot chat or channel: off by default, bounded queue with an asyncio worker, stdlib `urllib` (no new dependency), retries with 429 `retry_after`, 4096-char splitting, token scrubbing, and `POST /api/v1/diagnostics/test-telegram` to verify setup before a khutbah.
- **ADR-0017 — Curated language catalog + per-room targets.** Targets come from a validated catalog (`ar` source; `it en fr ur id bn fa ps tr sw de es`); the Control Panel renders a tick list, each room picks its own languages, and every rejection (rooms, WebSocket, diagnostics) names the valid codes. Room discovery, the projector, SRT export and the Telegram mirror all follow what the room actually broadcasts.
- **ADR-0017 — Safety-hold visibility (gate unchanged).** `GET /api/v1/diagnostics/safety-blocks` plus a Dashboard "Safety Holds" card show which signal withheld a line, so an operator can fix the input instead of guessing. The safety gate itself was not relaxed.
- **ADR-0015 follow-up — Camera refusal now explains itself.** `GET /api/v1/sessions/{id}/camera/ready` pre-flights the relay, the button stays disabled until the room is LIVE, and close codes are translated to operator words (no more "camera starts then closes").
- **ADR-0018 — Cockpit & projector UX.** Closing a room returns the cockpit to IDLE immediately (no page refresh before starting the next room), and the projector screen uses the same light surface as the phone view.

---
## Support
Open an issue in this repository for bugs, feature requests, or operational questions.

---
## Acknowledgments
Built for mosques and worshippers who need access to the khutbah in their own language.
Thanks to the open-source projects behind FastAPI, SQLAlchemy, React, Vite, hls.js, and SQLite that make this platform possible.

---
<p align="center">
<b>BAYAAN</b> — live translated captions for the mosque, text-first and safety-by-default.
</p>