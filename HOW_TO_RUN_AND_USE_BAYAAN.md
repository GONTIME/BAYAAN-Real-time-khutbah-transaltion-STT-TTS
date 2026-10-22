# HOW TO RUN AND USE BAYAAN — Complete Beginner's Guide

> If you can open a folder, type on a keyboard, and use a web browser, you can run BAYAAN.
> This guide assumes **no coding experience**. Every click and command is explained.

## 0. What is BAYAAN in plain words?

**BAYAAN** turns a live Arabic mosque sermon (khutbah) into **live translated captions** on people's phones — and can also put a live video feed (an HLS broadcast, an MJPEG camera, or a webcam plugged into the operator's own PC) on the projector and phones, alongside the captions.

Think of it like subtitles (and optionally live video) for the sermon:

```
Imam speaks Arabic into mixer
  -> Computer listens
  -> Computer writes Arabic + translates to English / Italian
  -> Worshippers see the translation on their phone (live captions)
  -> (Optional) the projector shows a video feed beside the captions
```

There are **2 parts**:

1. **Backend** =  A Python program that runs on your PC. It does transcription, translation, safety checks, and — when you use one — the video/camera relaying.
2. **Frontend** = A website that people open on their phones to read captions, that you open on your PC to prepare sermons and control the broadcast, and that runs the projector screen.

You will run both on the **same PC** for testing. In a real mosque, the PC stays near the mixer and phones connect over Wi-Fi.

What you will be able to do after this guide:

- Start the backend and the frontend
- Log in to the operator console
- Upload a sermon file, review the translation, publish it
- Create a Room + QR code, join from your phone, see live captions
- Add a live video feed (HLS / MP4 / MJPEG / embed / **PC camera**) to the projector and, if you want, to phones

> **Security note (real mosques):** BAYAAN now has an **operator login** (default `admin` / `admin` — change it before a real event, see Section 2 Step 4). The worshipper room page (`/r/<CODE>`) needs no login — only the operator console does. Never share the operator console address or credentials with the public.

## 1. What you need BEFORE you start (checklist)

- [ ] A **Windows PC** (Windows 10 or 11). This guide is written for Windows.
- [ ] **Python 3.11 or newer** installed. Check like this.
- [ ] **Node.js 18 or newer** installed (only needed for the website part). Check like this.
- [ ] **Google Chrome or Microsoft Edge** browser.
- [ ] The BAYAAN folder on your PC. In this guide it is: `D:\BAYAAN-Real-time-khutbah-transaltion-STT-TTS`
- [ ] Internet for the one-time installations. After that, the demo works offline.

### 1a. Check if Python is installed (30 seconds)

1. Click the **Start** button (Windows logo, bottom-left).
2. Type `powershell` and press **Enter**. A blue window opens. This is called the **terminal** — a place where you type commands instead of clicking.
3. Copy-paste this line into the blue window and press **Enter**:

```powershell
python --version
```

4. What you want to see: `Python 3.11.x` or `Python 3.12.x` or `Python 3.13.x` (any number 11 or higher = OK).
   - ✅ Example good: `Python 3.13.5`
   - ❌ Not good: `Python 3.10...` (too old) or `python is not recognized...` (not installed).
5. If it is missing or too old: go to `https://www.python.org/downloads/`, download Python 3.12, run the installer, and **tick the box "Add python.exe to PATH"** before clicking Install. Then close and reopen PowerShell and check again.

### 1b. Check if Node.js is installed (30 seconds)

In the same blue window, type:

```powershell
node --version
npm --version
```

What you want: `v18.x.x` or higher (example `v24.13.0` is fine), and npm prints a number too.

If missing: go to `https://nodejs.org/`, download the LTS version, install it, close and reopen PowerShell.

### 1c. Understand the folder layout (look once, no typing)

Open **File Explorer** and go to your BAYAAN folder, e.g. `D:\BAYAAN-Real-time-khutbah-transaltion-STT-TTS`. You will see:

| Folder / File | What it is (simple) |
|---|---|
| `backend` | You start it first. |
| `backend\.venv` | Python's private toolbox for this project (created once). You do not open it. |
| `backend\.env` | Secret settings (keys, login, base URLs). Make one from `.env.example` if it is missing. |
| `frontend` | The website (the phone + admin pages + projector screen). |
| `BAYAAN_MASTER_SPEC.md` | The rule book for developers. Readers can ignore it. |
| `README.md` | Short technical summary. This guide is the beginner version. |
| `docs` | Extra notes for the pilot. Readers can ignore for now. |

## 2. First-time setup — do this ONCE (about 12 minutes)

You only do this section the very first time on a new PC. Afterwards, skip to Section 3.

> Vocabulary: a **command** is one line you type into the blue PowerShell window and press Enter.
> Copy-paste is safer than typing. Right-click in PowerShell = paste.

### Step 1 — Open PowerShell inside the BAYAAN folder (the easy way)

1. Open **File Explorer** to `D:\BAYAAN-Real-time-khutbah-transaltion-STT-TTS`.
2. Click the **address bar** at the top (where the folder path shows), type `powershell`, press **Enter**.
3. A blue window opens, already "standing" inside the BAYAAN folder. ✅ Leave it open — you will use it for everything.

### Step 2 — Set up the backend (one-time install)

Copy-paste these **one line at a time** (wait for each to finish before the next):

```powershell
cd "D:\BAYAAN-Real-time-khutbah-transaltion-STT-TTS\backend"
```

What this does: moves ("cd" = change directory) into the `backend` folder. Like double-clicking into it, but for commands.

```powershell
python -m venv .venv
```

What this does: creates a private toolbox (`.venv`) so BAYAAN's Python pieces don't mix with other programs. It takes ~1 minute, shows no success message — silence = OK. If the folder `backend\.venv` already exists, this step safely does nothing.

```powershell
.\venv\Scripts\Activate.ps1
```

What this does: "enters" the toolbox. Your prompt changes to start with `(.venv)`. Think of it as putting on BAYAAN's work gloves — you must see `(.venv)` before the next commands.

> If Windows blocks this with a red error about "execution policies": run this once, then try activating again:
>
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> .\.venv\Scripts\Activate.ps1
> ```

```powershell
pip install -e ".[dev]"
```

What this does: downloads and installs everything the backend needs (web server, database, PDF reader...). Takes 2–5 minutes. Lots of text scrolling = normal. Wait until you see `Successfully installed ...` at the end.

Check it worked:

```powershell
python -m pytest -q
```

What this does: runs the 317 self-tests (the backend checking itself). What you want at the end: **`317 passed`**. It takes about one minute. If you see that, the backend is correctly installed. 🎉

### Step 3 — Set up the website (one-time install)

Still in the same blue window (with `(.venv)` showing), run:

```powershell
cd "D:\BAYAAN-Real-time-khutbah-transaltion-STT-TTS\frontend"
npm install --legacy-peer-deps
```

What this does: downloads the website's building blocks into `frontend\node_modules`. Takes 1–3 minutes. Progress bars = normal.

> Why `--legacy-peer-deps`? BAYAAN's build tool (Vite 8) is newer than a companion package officially supports, which makes plain `npm install` complain about a harmless "peer dependency" conflict. `--legacy-peer-deps` tells npm to skip that warning — this is the supported way to install BAYAAN's frontend.

### Step 4 — Create / keep your operator login (one-time, important!)

For security (bug-fix pass BUG-001/BUG-002), ALL operator functions — starting rooms, uploading sermons, ingesting audio, viewing metrics, etc. — now require a login. Out of the box the account is:

| Setting | Default value |
|---|---|
| Username | `admin` |
| Password | `admin` |

That is fine for testing on your own PC. **Before a real mosque event**, change both. Edit/create `backend\.env` and set:

```
BAYAAN_AUTH__USERNAME=your-operator-name
BAYAAN_AUTH__PASSWORD=make-this-a-long-secret
BAYAAN_AUTH__OPERATOR_TOKEN_SECRET=a-long-random-string-of-at-least-32-characters
BAYAAN_SECURITY__VISITOR_TOKEN_SECRET=another-long-random-string
```

(No quotes needed. If `backend\.env` does not exist, copy `backend\.env.example` to `backend\.env` first and edit that copy.)

> The operator key stays valid for 24 hours by default, and the login screen blocks repeated wrong passwords for a short time (that is a security feature, not a bug).

> That's IT for setup. You never need to repeat Section 2 unless you move to a brand-new PC.

## 3. Everyday start — do this EVERY time you use BAYAAN (2 minutes)

You need **TWO blue PowerShell windows** open at the same time:
- Window A = the backend
- Window B = the frontend

Think of it like turning on two machines: the kitchen (backend) and the serving counter (frontend). Both must stay ON while you use BAYAAN. **Don't close them.**

### Window A — Start the backend

1. Open a PowerShell inside `D:\BAYAAN-Real-time-khutbah-transaltion-STT-TTS` (File Explorer address bar trick from Section 2).
2. Run these two lines:

```powershell
cd "D:\BAYAAN-Real-time-khutbah-transaltion-STT-TTS\backend"
.\venv\Scripts\Activate.ps1
```

3. You should see `(.venv)` at the start of the line:

```powershell
python -m uvicorn bayaan_server.api.app:create_app --factory --reload --host 127.0.0.1 --port 8000
```

4. What success looks like: lines saying `Uvicorn running on http://127.0.0.1:8000`, plus `startup complete`.
5. **Leave this window open.** Minimizing is fine. Closing = backend turns off.

> If the port is busy (`Address already in use`): an old process is still running. Close the other blue window, or restart your PC, then try again.

### Window B — Start the website

1. Open a **second, separate** PowerShell (Start → type `powershell` → Enter).
2. Run:

```powershell
cd "D:\BAYAAN-Real-time-khutbah-transaltion-STT-TTS\frontend"
npm run dev
```

3. What success looks like: `Local: http://localhost:5173/` plus `ready in ... ms`.
4. **Leave this window open too.**

### Quick "is it alive?" check

Open Chrome/Edge and visit these addresses (type into the address bar, press Enter):

| Address | What you should see | Meaning |
|---|---|---|
| `http://127.0.0.1:8000/health` | `{"status":"ok","database":"ok",...}` | ✅ backend is alive. (This public address shows only liveness — `status`, `version`, `environment`, `database`. It deliberately does NOT expose provider internals to the public.) |
| `http://localhost:5173/` | The **login page** ("Operator sign in") | ✅ Website is alive and asks you to log in the first time |
| After logging in: `http://localhost:5173/` again | The **Mosque Operations Cockpit** (dashboard) | ✅ You are signed in |

If those load, BAYAAN is running. 🎉

> Deep checks (providers, circuits, preflight, metrics) are **operator-only** now:
> - /api/v1/preflight               → must be opened while you're logged in (returns 401 without credentials)
> - /api/v1/diagnostics/system-summary → the dashboard's Diagnostics tab shows this for you
> - /metrics                        → also operator-only
> - /health                         → public, but only liveness (no secrets)

### How to STOP everything at the end of the day

1. Click into Window A, press **Ctrl + C** (this stops the backend), then type `exit`.
2. Click into Window B, press **Ctrl + C**, then type `exit`.
3. Done. Your data (sermons, rooms, sessions) is saved in `backend/bayaan_dev.db` and will be there tomorrow.

> **Easier: use the Bayaan Control Panel instead (Section 3b).** It does all of the above with two buttons — Start and Stop — and edits your settings in a friendly form, so you never need to type commands again.

### 3b. (Recommended) Start and manage Bayaan with the Control Panel

The **Bayaan Control Panel** is a small window that opens in your browser. From it you can **start and stop both servers with one click**, check that they are actually running, and **edit every setting** (the same ones that live in `backend/.env`) with a description next to each field — no typing commands needed.

**Step 1 — Open the panel**

1. Open **File Explorer** and go to your BAYAAN folder (e.g. `D:\BAYAAN-Real-time-khutbah-transaltion-STT-TTS`).
2. Double-click **`Start Bayaan.bat`**.
3. A black window appears for a moment, then your browser opens the **Control Panel** at `http://127.0.0.1:8765/`.
   - Keep that black window open while you use BAYAAN.

**Step 2 — Start everything**

- Click the big green **▶ Start Bayaan** button.
- The panel starts the backend first, waits until it is healthy, then starts the frontend. The status pills above turn green (**Backend**, **Frontend**).
- When it's ready, the **Open Bayaan** button lights up — click it to open the app and log in.
   - You can also use the small per-service Start/Stop buttons on the left if you ever want only one half running.

**Step 3 — Change settings (no more editing files)**

1. Click the **Settings** tab.
2. Use the search box to find any setting (try `tts`, `confidence`, or `base url`).
3. Change the value — booleans use switches, choices use dropdowns, everything else is a text box. Read the short description under each name.
4. Click **Save settings**.
   - Your `backend/.env` file is updated automatically; comments already in the file are preserved.
   - **Save only applies after you restart the servers** (press **■ Stop Bayaan**, then **▶ Start Bayaan**) — the panel reminds you of this.
   - **Secrets** (API keys, passwords, token secrets) are never shown back to you: a box that says "set — type to replace" means the current value is untouched unless you type over it.

**Step 4 — Watch the servers work**

- Click the **Logs** tab to see live output from the backend (uvicorn) and the frontend (Vite) — this is where clearer error messages appear if something fails to start.

**Step 5 — Stop everything at the end of the day**

- Just click **■ Stop Bayaan**. Both servers are closed cleanly (kids of the processes are killed too).
- You can close the black window (that only closes the panel).

> **Phones on the same Wi-Fi?** In the left column, tick **"Allow phones to connect"** only when you're ready to expose Bayaan on your network — it is OFF by default (everything stays on 127.0.0.1). After turning it on, also set **`BAYAAN_PUBLIC_BASE_URL`** to your PC's LAN address (e.g. `http://192.168.1.20:5173`) in the Settings tab so QR codes work from phones, then restart.

## 4. Your first 15 minutes with BAYAAN — the full tour (follow in order)

Do this with the backend (Window A) and website (Window B) **both running**.

### Tour stop 1 — Log in and open the pages (1 minute)

1. In Chrome/Edge, open `http://localhost:5173/`.
2. You see the **Operator sign in** page. Type `admin` / `admin` (or the login you set in Section 2 Step 4) and sign in.
3. You land on the **Mosque Operations Cockpit** — the dashboard with tabs: **Live**, **Sermons**, **Diagnostics**, **Scripture**. Everything you do as the operator happens here. It is the same page for `/`, `/dashboard`, `/admin`, `/operator` — one unified cockpit.
4. Three page types share the same BAYAAN install:
   - **Operator cockpit**: `http://localhost:5173/` — requires the login above.
   - **Worshipper mobile view**: `http://localhost:5173/r/<ROOM_CODE>` — what worshippers see on their smartphones (lightweight, live captions, language switcher, no login needed).
   - **Mosque projector display**: `http://localhost:5173/projector/<ROOM_CODE>` — full-screen high-contrast mode for projectors and TV walls (shows captions + the video feed when one is set).

> ⚠️ Safety note (real mosques): the operator cockpit requires its login, but the worshipper room page intentionally needs none. Keep the operator PC with the cockpit address + credentials in trusted hands only — never print that address on the room QR or share it publicly.

### Tour stop 2 — Upload a sermon file (2 minutes)

Imagine: the imam gives you Friday's sermon as a Word file. You upload it so the translation is **pre-approved** before the live sermon.

1. In the **Sermons** tab, find the upload area (file picker + title box).
2. Click **Browse / Choose file**, pick any `.txt`, `.md`, `.docx`, or `.pdf` file with some Arabic text.
   - No file handy? Open Notepad, type a few Arabic lines, save as `khutbah.txt` on your Desktop, upload that.
3. Type a title (e.g. `Friday Khutbah Test`) and click **Upload**.
4. Success looks like: `Uploaded -> version 1 (DRAFT)` + the sermon appears in the list on the left.
5. Click the sermon in the list. You see it split into numbered **segments** (small pieces, in order).

What just happened (simple): BAYAAN read your file, cleaned it, and cut it into small numbered pieces so each piece can be translated and checked separately.

File rules to remember:

- `.txt` / `.md` = always works.
- `.docx` (Word) = works, but tracked deletions, comments, and hidden text are **ignored on purpose** (safety: only final text is used).
- `.pdf` = works only if the PDF has real selectable text. A scanned photo-PDF is rejected with error `422` (you need to retype or use a text PDF — BAYAAN never guesses with OCR in the demo).

### Tour stop 3 — Make drafts, edit, approve (5 minutes)

Each segment needs an approved translation before Friday.

1. Click **Generate drafts**. BAYAAN writes a draft translation (English + Italian) for every segment. It is safe to click again — it only fills any missing draft and never duplicates or overwrites one you already reviewed. (Out of the box BAYAAN ships a *mock* translator that echoes the Arabic with a `[MOCK-TRANSLATION]` tag; for real English/Italian text switch the provider to LibreTranslate — see `docs/DATABASE.md` and `backend/.env.example`.)
2. Click a segment. You see: the **Arabic source**, the **draft translations**, **glossary hits** (special religious words), and yellow **⚠ warnings** (e.g. number changed, negation flipped — the safety engine explaining itself in plain words).
3. If a translation is wrong, click **Edit**, fix the text, optionally type a reason (e.g. `fixed number`), click **Save**. The old text is kept as `previously:` — nothing is silently overwritten.
4. When a draft is correct, click **Approve**. It gets a green **Approved** badge + your reviewer/operator name + date. To trust them all at once, click **Approve all drafts** (it approves every draft on this version as you). Editing any draft later resets just that one back to Unapproved.
5. Continue until every draft shows Approved.

Then move the whole version through its life stages (buttons in the version panel, in order):

```
DRAFT -> REVIEW -> APPROVED -> PUBLISHED
```

- Click **REVIEW** when drafts exist.
- Click **APPROVED** only when every draft is approved.
- Click **PUBLISHED** to lock it forever. 🔒

> Published = frozen. You cannot edit, re-publish, or delete its content anymore (trying gives error `409` — that is the safety lock working, not a bug). During the live sermon, BAYAAN reuses these approved translations instead of inventing new ones.

### Tour stop 4 — Start a room + QR code (2 minutes)

A **Room** is one live sermon event. The **QR code** is how worshippers join with their phones. No sermon file is needed for this:

1. Open the dashboard (`http://localhost:5173/`) — you're already logged in.
2. On the **Live** tab click **Start Room**. You instantly get a **room code**, a **QR code**, and a **join link** (`/r/<CODE>`).
3. Click **Go live** (this moves the room CREATED → READY → LIVE; the badge turns green).
4. Share the QR: worshippers scan it (or you type the link) and land on the room page for that code.

What the worshipper sees (the room page / phone):

- A **LIVE / IDLE** badge at the top.
- Language buttons: **Italiano / English**. Tap one.
- A checkbox **العربية** — tick it to also show the original Arabic under each line.
- The big current line + history of earlier lines scrolling below.
- A **🔊 / 🔇 audio control**. By default it reads **"🔇 Audio off (server)"** — that is honest: spoken audio is locked on the server (§201). See "Turning on spoken audio" below.
- If you enabled the video feed for phones, a live video box above the captions.

If you land on the bare home URL (`http://localhost:5173/`) and you are not logged in, you get the sign-in page — the room page itself always needs a code in the link (`/r/<CODE>`).

### Tour stop 5 — Go live and see captions (3 minutes)

**No microphone needed for a demo.** On the **Live** tab, once the room is LIVE, use the **"Send test line"** box:

1. The box already has an example Arabic sentence. Edit it if you like.
2. Click **Send test line**.
3. Open the worshipper view (`http://localhost:5173/r/YOUR_CODE`) — a translated caption appears within a second or two, newest at the bottom (last 30 kept).

That test line goes through the **exact same** pipeline as real audio: normalize → content routing → translation → safety validation → the safety gate → broadcast. It is not a shortcut and it bypasses no safety rule — if a line is unsafe or low-confidence you still get the safe **fallback message**, never a made-up line.

> **Real microphone path:** in production, audio is captured from the mixer line-in (`sounddevice` optional extra, §184) or from the operator's browser microphone (when capture is configured) and streamed through the same pipeline. When that is wired, you won't need the test-line button — but it stays handy for troubleshooting.

### Tour stop 6 — Add a live video feed (optional, 3 minutes)

In the **Live** tab there is a **"📹 Live Video Stream Feed"** card. It shows a feed on the projector (and, if you tick the box, on worshipper phones). Choose the **kind** that matches your source:

| Kind in the dropdown | For... | How it is shown |
|---|---|---|
| HLS (.m3u8) | Mosque CCTV / streaming servers (the common case) | Plays in every browser (Chrome/Edge/Firefox via hls.js, Safari natively) |
| MP4 / WebM | A plain recorded video file | Plain video player |
| MJPEG stream | Old-style CCTV IP cameras that output MJPEG | Refreshes like a live image |
| Embed | YouTube Live / other player with an embed link | Shows inside an embedded player |
| PC camera (relay) | A webcam plugged into the operator's own PC (see tour stop 7) | Your chosen camera feed, streamed through the server |

How to use it:

1. Pick the **kind**, then paste the stream URL (not needed for "PC camera (relay)" — it is automatic).
2. Tick **"Also show on worshipper phones (room page)"** only if you want phones to receive the video too (it costs Wi-Fi bandwidth per phone; default is projector-only).
3. Click **Save Stream Feed**. The projector screen (`/projector/<CODE>`) shows it right away.

> Cross-origin feeds "just work": BAYAAN routes HLS through a built-in same-origin media helper so even CCTV servers that send no CORS headers play fine. If a feed cannot be reached from the server's network at all, you get an honest **"Video feed unavailable + Retry"** message instead of a silent black box.

### Tour stop 7 — Use the PC camera (optional, 3 minutes)

1. On the **Live** tab, in the **"📷 PC Camera (relay)"** card, click **🎥 Start PC Camera**.
2. Your browser asks for camera permission — allow it. You see a small **preview** of the camera in the card.
3. Starting the camera **automatically makes it the live feed** on the projector — no second "Save" step needed. (If phones should get it too, tick the "show on worshipper phones" box and save once.)
4. Click **🛑 Stop Camera** when done.

What is happening (simple): the browser sends a small JPEG picture ~10 times a second to the server; the server keeps only the newest frame and re-serves it to the projector/phones. No camera app, no codec setup.

> The camera needs a **secure context** (https or `localhost`) and a **LIVE** session — that's why the button is disabled until the room is live. If the connection is rejected (e.g. session ended), the camera is released automatically and you get an honest error.

### Opening the room on phones on the same Wi-Fi

By default the QR points at `127.0.0.1`, which only the PC can reach. To open the room from other devices on your Wi-Fi:

1. **Bind both servers to your network** (not just localhost):
   - Backend (Window A): change the start command's `--host 127.0.0.1` to `--host 0.0.0.0`.
   - Frontend (Window B): run `npm run dev -- --host`.
2. **Find your PC's LAN address** in PowerShell:
   ```powershell
   (Get-NetIPAddress -AddressFamily IPv4 | Where-Object PrefixOrigin -ne 'WellKnown').IPAddress
   ```
   e.g. `192.168.1.20`.
3. **Tell BAYAAN that address** so the QR + join link use it. Add this line to `backend\.env`:
   ```
   BAYAAN_PUBLIC_BASE_URL=http://192.168.1.20:5173
   ```
   (Use your real address; use port `8000` instead of `5173` if you serve the built site from the backend.) Restart the backend so it re-reads `.env`.
4. Start a fresh room (so its QR encodes the new address) and scan it from a phone on the **same Wi-Fi**.

### Turning on spoken audio (optional)

BAYAAN is **text-first by design** (§201); spoken audio is off by default. To turn it on, set in `backend\.env`:

```
BAYAAN_SAFETY__TTS_ENABLED=true
BAYAAN_PROVIDERS__TTS=piper
```

and restart the backend. The room page will then show a working **🔊 Audio on / 🔇 Audio off** toggle, and only lines that pass the strict audio safety gate are spoken. Without a real engine installed, the **mock** provider emits silence (so captions work but audio is quiet) — install the local engine below to hear speech.

#### The shipped TTS engine: Piper / Sherpa-ONNX (offline, CPU-only)

BAYAAN ships a real local TTS adapter that runs open **Piper-format voices**
fully offline — no GPU, no vendor, no internet, which keeps it usable on
low-end mosque PCs (and it is what makes an open-source deployment portable).
Hardware footprint: CPU-only, well under 0.5 GB RAM, ~20–60 MB per voice,
real-time on a 2-core desktop (and Raspberry Pi 4 for low/medium voices).

1. **Get the binary** — install a `piper` executable (or a sherpa-onnx CLI that
   accepts `--model <voice.onnx> --output_raw`), e.g.:
   - **Windows (recommended):** download the standalone self-contained build
     `piper_windows_amd64.zip` from the rhasspy/piper releases (tag
     `2023.11.14-2`) and unzip it (contains `piper.exe` + DLLs + espeak-ng data).
     No Python/venv dependency, survives any Python version.
   - `pip install piper-tts` also works on CPython ≤3.12, but its native
     extension **segfaults on CPython 3.13** (`0xC0000005` on `import piper`) —
     if your venv is 3.13, use the standalone binary instead.
2. **Download voices** into a folder (e.g. `backend/piper_voices/`), one per
   target language. Defaults (already configured) are:
   - Arabic: `ar_JO-kareem-medium` (or `-low` for weaker hardware)
   - Italian: `it_IT-paola-medium`
   - English: `en_US-lessac-medium`
   Each voice is two files: `<voice>.onnx` + `<voice>.onnx.json`.
3. **Point BAYAAN at them** in `backend\.env`:
   ```
   BAYAAN_PROVIDERS__TTS=piper
   BAYAAN_PROVIDERS__PIPER_EXEC=piper
   BAYAAN_PROVIDERS__PIPER_VOICE_DIR=C:\path\to\piper_voices
   BAYAAN_PROVIDERS__PIPER_VOICE_AR=ar_JO-kareem-medium
   BAYAAN_PROVIDERS__PIPER_VOICE_IT=it_IT-paola-medium
   BAYAAN_PROVIDERS__PIPER_VOICE_EN=en_US-lessac-medium
   ```
4. **Verify at preflight**: `/api/v1/preflight` gains a `tts_engine` component
   that checks the binary + voice files. If the engine is missing the app still
   starts and captions keep working (§201 text-first) — audio just stays silent.

> ⚠️ **One more knob so audio actually sounds.** Real translation providers
> (Ollama, Ollama Cloud, LibreTranslate) do **not** report a confidence score
> (§0 rule 8: BAYAAN never invents one), so the strict audio gate (§80) would
> refuse every line and nothing would be spoken. If you use one of those
> providers, opt into the trust-the-approved-chain path:
> ```
> BAYAAN_TTS__REQUIRE_TRANSLATION_CONFIDENCE=false
> ```
> All the other §80 rules still apply (safety ALLOW, validation ALLOW,
> sacred-text reference, freshness); only the *missing* confidence is waived.
> A provider that **does** report a numeric score below
> `BAYAAN_SAFETY__TTS_MIN_CONFIDENCE` is still never spoken.

> Hardware tiers: **Piper/Sherpa-ONNX** = runs on almost anything (CPU, offline).
> For higher-fidelity Arabic/Italian/English you can plug in a larger local
> engine (e.g. Coqui XTTS v2, ~1.5 GB VRAM GPU, note its non-commercial
> license) or a cloud API — the `TTSProvider` port (§186) accepts any adapter.

### The translation provider

`backend\.env` selects the machine-translation engine (see `docs/adr/0011`). The shipped default is **Ollama Cloud (gemma4:31b)** with a **local Ollama fallback** — the best Arabic→English/Italian quality we tested. Because cloud translation sends the sermon text to Ollama's cloud ("processed, not used for training"), it is an explicit opt-in (already set in the shipped `backend\.env`). To stay fully offline instead, switch to a local chain:

```
BAYAAN_PROVIDERS__TRANSLATION=ollama
BAYAAN_PROVIDERS__TRANSLATION_FALLBACK=mock
```

If "Generate drafts" ever fails, the console shows the exact reason (e.g. the cloud key is missing or Ollama isn't running) — fix that and click **Generate drafts** again; it is safe to retry.

> **Privacy, clearly stated:** the room page itself sends nothing anywhere; captions come from the mosque's own server. Two things involve a third party, and both are explicit operator choices: (1) cloud translation (above), and (2) the optional **browser speech** mode on the operator microphone, which clearly asks for your consent before it starts (it uses the browser vendor's speech service — decline = the server's own ASR path is used instead).

## 5. Glossary — the "holy words dictionary" (3 minutes, important!)

Some Arabic religious words must **always** translate the same way (e.g. الصلاة → prayer, never "worship" or "ritual"). The glossary enforces this.

1. In the **📖 Khutbah Studio** tab, find the **📿 Sacred Islamic Glossary** section.
2. Click **Add term**. Type the Arabic word (e.g. `الصلاة`), then its fixed translations: English `prayer`, Italian `preghiera`. Click Save.
3. The new term appears as **Pending**. Get it **Approved** (click Approve) — only approved glossary terms are enforced while the sermon is live.

When a prepared sermon segment contains an approved glossary word, the review panel highlights it ("glossary hit") and the translation is expected to use exactly the approved word. Approved terms are versioned — every published translation records which glossary version it was reviewed against, so nothing changes underneath you later.

## 6. Where to click — cheat-sheet for the cockpit

The one-page cockpit has four tabs. A quick map:

| You want to... | Go to |
|---|---|
| Start a room, go live, sell test lines, watch the live feed, add a video/camera feed | 🎙️ **Live Broadcast Cockpit** |
| Upload sermons, review segments, approve drafts, manage glossary | 📖 **Khutbah Studio** |
| See health, providers, latency, run a translation test | **Diagnostics** |
| Browse + approve Quran/Hadith scripture references | **Scripture** |
| Open the projector screen | 📽️ **Projector Mode** button (top right) |

**Khutbah Studio (sermon panel) cheat-sheet:**

| What you see | What it means |
|---|---|
| Sermon list (left) | All uploaded sermons. Click one to open it. The **✕** button deletes an unpublished sermon (asks first; a published one is frozen and cannot be deleted — §41). |
| Segments (#1, #2...) | The sermon cut into small numbered pieces. |
| `GENERAL / QURAN / HADITH` badge | What kind of text this piece is. Quran pieces are extra strict. |
| Draft (English / Italiano) | The proposed translation for that piece + language. |
| `Approved / Unapproved` badge | Green = a human said "this is correct". Red = not yet. |
| `provider / model · gv g1` small print | Who wrote the draft + which glossary version. Proof, not decoration. |
| Glossary hits | "This piece contains a holy word from the dictionary — check it." |
| ⚠ warnings | The safety engine saying *why* it worries (number changed, negation flipped, unverified verse...). Each warning has a code + sentence. |
| Edit / Save (§169) | Fix the text. Old text is kept as `previously:`. |
| Approve | "I, the reviewer, stand behind this translation." |
| Approve all drafts | Approve every draft on this version at once (§96). |
| Generate drafts | "Write first translations for all pieces." Safe to click again — it only fills gaps, never duplicates. |
| DRAFT → REVIEW → APPROVED → PUBLISHED | The life stages. Only PUBLISHED pieces are used live. PUBLISHED = frozen forever. |
| Refresh | Re-read from the backend. Click it if the screen looks stale. |

**Room page (`/r/YOUR_CODE`) — the worshipper's phone:**

| What you see | What it means |
|---|---|
| LIVE (green) / IDLE (gray) | Is the sermon happening right now? |
| `connecting / open / closed` | Is my phone connected? `open` = good. |
| Italiano / English buttons | Tap to switch language. Your choice is remembered. |
| العربية checkbox | Also show the original Arabic under each line. |
| Video box (when the operator enabled it) | The live feed, once the operator turns it on for the room. |
| Big current line | The newest caption. |
| History list | Older captions (last 30). Scroll up to re-read. |
| 🔊 Audio on / 🔇 Audio off | Spoken translation. Off by default; tap to allow. Only plays lines that passed the strict audio safety gate. |
| Room unavailable | Wrong code or room closed. Check the QR and try again. |
| Fallback message (gray text) | "The safe translation is not available right now." This is BAYAAN protecting you from a wrong line — not an error to fix by tapping. |

## 7. Safety rules you must know (read once — 2 minutes)

BAYAAN is strict on purpose. These are not bugs; they are the product:

1. **Quran verses are never machine-translated.** A Quran line is shown only if it matches an **approved** verse in the scripture library — word-for-word. Anything else is **BLOCKED** and the audience sees the safe fallback. No exceptions.
2. **Hadith is shown without attribution unless approved.** An approved hadith shows its approved translation. Anything else becomes a general teaching with **no** "the Prophet said" wording. BAYAAN never invents religious attribution.
3. **Numbers, negations, and names are checked every time.** "3 days" vs "7 days", "allowed" vs "forbidden", "Zayd" vs "Amr" — any change BLOCKS the line. The reviewer must fix and re-approve it.
4. **Low confidence = silence, not guessing.** If the computer is unsure, it shows the fallback message instead of a risky translation (spec rule: never auto-broadcast unsure output).
5. **Spoken audio is doubly locked.** Text captions are the main product. Audio (TTS) is OFF by default and only speaks lines that pass an extra-strict gate twice — plus it never plays old lines late (stale audio is dropped, not played).
6. **Published sermons are frozen.** PUBLISHED content cannot be edited — error `409` means the lock is working.
7. **The mosque speakers are never touched.** BAYAAN only adds captions/phones (and optionally the projector video). The original Arabic PA stays exactly as it is.
8. **The operator console is guarded.** Every operator action now needs the login (Section 2 Step 4). Wrong passwords are rate-limited, and minutes before an event you should test that your login works so you are never locked out.

## 8. Fix-it table — when something looks wrong

| Problem | What it usually means | What to do (in order) |
|---|---|---|
| `/health` won't open | Backend not running | Start Window A; visit `http://127.0.0.1:8000/health` |
| `/health` shows `"status":"degraded"` | A provider chain failed over (by design) | Open the **Diagnostics** tab (login first) and read the reason; the room still shows fallback, never fake text |
| Dashboard shows the login page even though "I logged in" | Login expired (tokens last 24h) | Sign in again — normal |
| `401` on `/api/v1/...` | Missed the login / token expired | Sign in on `http://localhost:5173/` first; the dashboard sends your token automatically |
| "Sermon upload rejected 422" | Image-only PDF or unreadable file | Use a text `.txt` / `.docx` / real-text PDF instead |
| Publish blocked / `409` | Trying to edit a PUBLISHED version | Correct — create a new version instead; published content is immutable |
| `Room unavailable` on phone | Wrong code / room closed / backend down | Check the code letter-by-letter; confirm Window A is running; re-scan QR |
| Phone can't open the room at all | QR points at `127.0.0.1` (PC-only) | Set `BAYAAN_PUBLIC_BASE_URL` to your LAN address, bind servers with `--host 0.0.0.0` / `npm run dev -- --host`, start a fresh room (see "same Wi-Fi" above) |
| No captions on the room page | No audio is being fed to the session | On the **Live Broadcast Cockpit** (room LIVE) click **Send test line** — a translated caption appears. For real audio, wire the mixer/mic path |
| Generate drafts fails | MT provider chain is down (cloud key missing, or Ollama not running) | The console shows the exact reason. Fix it (start Ollama / set the key) and click **Generate drafts** again — retry is safe |
| Video feed shows "🎬 unavailable + Retry" | The feed URL can't be reached from the server's network | Confirm the stream is reachable *from the PC* (not just your phone); check the address/kind; click Retry |
| Video feed plays on nothing / black box | Wrong kind chosen for that URL | Re-pick the kind on the Live tab (e.g. an `.m3u8` link must be HLS, not MP4) and Save |
| "Start PC Camera" does nothing | Not a secure context or no LIVE session | Use `http://localhost:5173` (or https); start the room and go live first |
| Camera shows feed on the PC but not the projector | Feed was never saved as the camera kind | Starting the camera now saves it automatically; if you stopped it, click **Start PC Camera** again (one click re-arms the feed) |
| No audio / "🔇 Audio off (server)" | Server TTS is locked (§201) | Set `BAYAAN_SAFETY__TTS_ENABLED=true` (+ `BAYAAN_PROVIDERS__TTS=piper`) in `backend\.env` and **restart the backend** |
| Audio on, but phones stay silent | Backend not restarted after installing the engine/voices; phone's audio toggle not tapped; or phone is on the source language `ar` (no Arabic TTS is emitted — only target languages) | Restart uvicorn so it picks up `PIPER_EXEC`/`PIPER_VOICE_DIR`; on each phone tap **🔊 Audio on** (autoplay policy needs the tap); select a target language (`English`/`Italiano`). Check `/api/v1/preflight` → `tts_engine` is `OK` |
| Captions stuck on `connecting` | Phone Wi-Fi / backend down | Same Wi-Fi as PC? Window A running? Reload the page |
| `(.venv)` missing in Window A | Forgot to activate | Run `.\venv\Scripts\Activate.ps1` before starting uvicorn |
| `npm install` errors about a "peer dependency" | Expected with Vite 8 | Use `npm install --legacy-peer-deps` (Section 2 Step 3) |

Still stuck? Copy the **last 20 lines** of the red text in the blue window and show them to whoever set up the PC — that text is the clue.

## 9. Friday checklist for the operator (print this)

**Thursday — prepare:**

- [ ] Sermon file received from the imam.
- [ ] Uploaded in **Khutbah Studio**, segments look correct.
- [ ] Drafts generated, every draft reviewed.
- [ ] Glossary terms added + approved (holy words fixed).
- [ ] Version moved DRAFT → REVIEW → APPROVED → PUBLISHED. Locked. 🔒
- [ ] Room created, QR printed / ready for projector.
- [ ] Test join from your own phone: language buttons work, العربية toggle works.
- [ ] (Optional) Video feed URL tested, and/or the PC camera tested in the Live tab.

**Friday — 30 minutes before:**

- [ ] Operator PC on, plugged into power (not battery).
- [ ] Window A (backend) started — `/health` says `ok` and `database: ok`.
- [ ] Window B (frontend) started — sign in at `http://localhost:5173/` and confirm the cockpit loads.
- [ ] **Diagnostics** tab looks normal (preflight-style checks green; login confirmed).
- [ ] Session status → READY, then → **LIVE** when the imam starts.
- [ ] QR visible. One volunteer confirms captions appear on their phone.
- [ ] (If using video) the feed/camera shows on the projector screen.

**During:**

- [ ] Watch the room page, not the code windows.
- [ ] If captions show fallback (gray) — normal protection, do nothing.
- [ ] If phones show `connecting` — check Wi-Fi, don't restart the backend mid-sermon unless told to.

**After:**

- [ ] Session → ENDED. Rooms deactivated. Stop the camera (if running) so the webcam light turns off.
- [ ] Stop Window A + B (Ctrl+C). Note any wrong lines for next week's glossary.

## 10. Where to go next (when you're comfortable)

- `README.md` — the short technical summary (for IT people).
- `BAYAAN_MASTER_SPEC.md` — the full rule book (§§0–217, for developers).
- `docs/adr/` — why each big decision was made (0001–0015).
- Want proof the safety works? In Window A (activated): `python -m pytest -q` → **`317 passed`**.

---

*Guide version: BAYAAN v0.1.1 (Phases 0–7 complete plus security hardening and video/camera support). Text-first product: live captions it/en + QR + prepared sermons + glossary. Optional extras clearly marked wherever they exist — spoken audio stays locked unless the operator enables it, and the optional PC camera / video feed is an operator choice, shown on phones only when explicitly enabled.*
