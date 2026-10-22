"""Live smoke check (§200): drive a scripted line through the REAL pipeline
against the configured MT provider (Ollama Cloud gemma4:31b) and print the
caption a phone would receive - proving "Send a test line" produces a real,
translated caption without a microphone.

Run from backend/ (uses backend/.env via get_settings, or set env vars):
    .\.venv\Scripts\python.exe tools\demo_line_live_check.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bayaan_server.api.app import create_app  # noqa: E402
from bayaan_server.config import get_settings  # noqa: E402

TEXT = "مرحبا بكم في مسجدنا، الحمد لله رب العالمين"


def main() -> int:
    base = get_settings()
    tmp = Path(tempfile.mkdtemp()) / "demo_check.db"
    settings = base.model_copy(update={"database": base.database.model_copy(
        update={"url": f"sqlite+aiosqlite:///{tmp}"}
    )})
    print(f"translation provider: {settings.providers.translation} "
          f"(fallback: {settings.providers.translation_fallback})")
    with TestClient(create_app(settings)) as c:
        start = c.post("/api/v1/rooms/start", json={}).json()
        sid = start["session_id"]
        for st in ("READY", "LIVE"):
            c.post(f"/api/v1/sessions/{sid}/status", json={"status": st})
        room = c.get(f"/api/v1/rooms/{start['code']}").json()
        token = room["visitor_token"]
        with c.websocket_connect(
            f"/ws/rooms/{start['code']}?lang=en&token={token}"
        ) as ws:
            hello = ws.receive_json()
            print(f"hello tts_enabled : {hello.get('tts_enabled')}")
            r = c.post(
                f"/api/v1/sessions/{sid}/demo-line", json={"arabic": TEXT}
            )
            print(f"demo-line status  : {r.status_code}")
            cap = ws.receive_json()
            print(f"arabic            : {cap['arabic']}")
            print(f"content_type      : {cap['content_type']}")
            for lang, t in cap["targets"].items():
                print(f"  {lang}: state={t['state']} route={t.get('route')} "
                      f"speak={t.get('speak')}")
                print(f"      text={t['text']}")
    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
