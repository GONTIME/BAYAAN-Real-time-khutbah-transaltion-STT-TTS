"""End-to-end Ollama translation check against the REAL live pipeline.

Ops/diagnostic tool (§195): drives a scripted utterance through the actual
ASR -> translate (Ollama) -> validate -> safety-gate path and prints the
caption the public client would receive, so an operator can confirm the
local-LLM adapter is wired correctly without a microphone or a browser.

Usage (from backend/):
    ..\\.venv\\Scripts\\python.exe tools\\ollama_live_check.py [arabic text]
"""

from __future__ import annotations

import io
import struct
import sys
import tempfile
import wave
from pathlib import Path

from fastapi.testclient import TestClient

# The Windows console defaults to cp1252, which cannot encode Arabic (§58: the
# khutbah is Arabic). Force UTF-8 so this diagnostic can print real text.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bayaan_server.api.app import create_app  # noqa: E402
from bayaan_server.config import (  # noqa: E402
    DatabaseSettings,
    ProviderSettings,
    Settings,
)

DEFAULT_TEXT = "\u0645\u0631\u062d\u0628\u0627 \u0628\u0643\u0645 \u0641\u064a \u0645\u0633\u062c\u062f\u0646\u0627"


def make_wav(speech_seconds: float = 0.6) -> bytes:
    """Energy-VAD-shaped clip: silence, loud, silence (§84)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 8000)
        n = int(16000 * speech_seconds)
        w.writeframes(b"".join(struct.pack("<h", 3000) for _ in range(n)))
        w.writeframes(b"\x00\x00" * 16000)
    return buf.getvalue()


def main() -> int:
    arabic = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TEXT
    tmp = Path(tempfile.mkdtemp()) / "ollama_check.db"
    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp}"),
        providers=ProviderSettings(
            translation="ollama",
            mock_asr_text=arabic,
            mock_asr_confidence=0.99,
        ),
    )
    with TestClient(create_app(settings)) as client:
        room = client.post("/api/v1/rooms", json={}).json()
        session = client.post(
            "/api/v1/sessions",
            json={"room_id": room["id"], "active_languages": ["it", "en"]},
        ).json()
        for status in ("READY", "LIVE"):
            client.post(
                f"/api/v1/sessions/{session['id']}/status", json={"status": status}
            )

        token = client.get(f"/api/v1/rooms/{room['code']}").json()["visitor_token"]
        with client.websocket_connect(
            f"/ws/rooms/{room['code']}?lang=en&token={token}"
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            client.post(
                f"/api/v1/sessions/{session['id']}/ingest",
                files={"file": ("clip.wav", make_wav(), "audio/wav")},
            )
            caption = ws.receive_json()

    print(f"arabic      : {caption['arabic']}")
    print(f"content_type: {caption['content_type']}")
    for lang, target in caption["targets"].items():
        print(
            f"  {lang}: state={target['state']} route={target.get('route')} "
            f"speak={target.get('speak')}"
        )
        print(f"      text={target['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
