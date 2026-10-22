# BAYAAN
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688?logo=fastapi)](https://fastapi.tiangolo.com/)
[![React + Vite](https://img.shields.io/badge/frontend-React%20%2B%20Vite-61DAFB?logo=react)](https://vitejs.dev/)
[![SQLite](https://img.shields.io/badge/database-SQLITE-07405E?logo=sqlite)](https://www.sqlite.org/)
[![Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows)](https://www.microsoft.com/windows)
[![License: TBD](https://img.shields.io/badge/license-TBD-lightgrey)](LICENSE)

Real-time sermon translation platform for mosques. BAYAAN takes Arabic speech from the digital mixer, transcribes it, aligns it against approved prepared sermons, translates to
target languages (Italian, English), safety-validates every caption, and broadcasts live captions with optional gated text-to-speech to worshippers' phones, projectors, and
headsets over the local network.

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
| **Prepared sermons** | Upload txt/md/docx/pdf, semantic segmentation, translation drafts with full provenance, glossary governance, reviewer edit/approve, immutable publish. |
| **Rooms & QR** | One-click live room creation, room code + QR + join link, visitor tokens, rate limiting, security headers, caption history replay. |
| **Video (optional)** | HLS broadcast, MJPEG camera, embed, or PC webcam feed on the projector and (optionally) phones — operator-controlled. |
| **Resilience** | Per-capability failover chains, circuit breakers, failover budgets, watchdog heartbeats, degraded mode with a public fallback — never a crash, never fabricated output. |
| **Accessibility** | Arabic language toggle, right-to-left support, §134 public fallback display. |

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
## Tech Stack
**Backend** (`backend/`)
- Python 3.11+, FastAPI, Uvicorn, Pydantic v2, pydantic-settings
- SQLAlchemy 2.0 (async) + aiosqlite (SQLite); `asyncpg` optional for Postgres
- `pypdf` for sermon ingestion; `qrcode` for room QR codes; `sounddevice` optional for PC audio
- pytest + pytest-asyncio + httpx for testing (stdlib-only control panel tool)
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
cd Trans_Sys
```
2. Backend (the brain):
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
# edit backend/.env as needed (see .env.example)
python -m uvicorn bayaan_server.main:app --reload
```
3. Frontend (the face), in a second terminal:
```powershell
cd frontend
npm install
npm run dev
```
4. Open `http://localhost:5173/` in your browser, sign in to the operator console (default `admin` / `admin` — change before any real event), create a room, and share the QR code with worshippers on the same Wi-Fi.
The easiest Windows path is to double-click **`Start Bayaan.bat`** at the repository root.

---
## How to Use
Step-by-step, click-by-click instructions for non-technical users live in the separate guide **`HOW_TO_RUN_AND_USE_BAYAAN.md`** — it covers the pre-flight checklist, starting the brain and the website, logging in, uploading and publishing sermons, creating rooms with QR codes, adding optional video/PC-camera feeds, troubleshooting, and the Friday
operator checklist.
To keep this README focused, usage details are **not duplicated here**; refer to that
guide for the complete operator workflow.

---
## Configuration
Runtime settings live in `backend/.env` See `.env.example`.

---
## Testing
Run the backend test suite from `backend/` with the virtualenv activated:
```powershell
python -m pytest -q
```
---
## Security & Privacy
- **Operator login** protects the console (`/admin`); worshipper room pages (`/r/<CODE>`) need no login. Never share operator credentials publicly.
- **Text-first:** TTS is locked by default; only safety-approved, non-stale captions are ever synthesized or broadcast (§80 gate runs at enqueue and again at dequeue).
- **Audio events carry PCM bytes only** — no raw transcript, no identity.
- **Degraded mode:** provider outages are absorbed by failover; exhausted chains degrade gracefully with a public fallback — never a crash, never fabricated output.
- **Religious safety:** unverified Quran is blocked (never machine-translated); Hadith routes to approved-or-general non-attributed text.

---
## Code of Conduct
Please be respectful and inclusive in issues, pull requests, and discussions. This project serves houses of worship; keep contributions focused on reliability, safety, and accessibility for that context.

---
## Roadmap & Status
Implementation is organized by phases:
See [`docs/adr/`](docs/adr) (0001–0015) for the decisions behind each phase.

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