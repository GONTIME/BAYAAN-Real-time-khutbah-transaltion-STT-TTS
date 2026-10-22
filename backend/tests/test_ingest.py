"""Phase 1 end-to-end ingest tests (§190): WAV -> VAD -> ASR -> segments."""

from __future__ import annotations

import io
import struct
import uuid
import wave

from fastapi.testclient import TestClient

SR = 16000


def make_wav(segments: list[tuple[str, float]], sample_rate: int = SR) -> bytes:
    """Build a mono 16-bit WAV from (kind, seconds) parts."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for kind, seconds in segments:
            n = int(sample_rate * seconds)
            if kind == "silence":
                w.writeframes(b"\x00\x00" * n)
            else:  # speech: loud constant amplitude (RMS far above threshold)
                w.writeframes(b"".join(struct.pack("<h", 3000) for _ in range(n)))
    return buf.getvalue()


def _goto_live(client: TestClient) -> dict:
    created = client.post(
        "/api/v1/sessions",
        json={"room_id": str(uuid.uuid4()), "active_languages": ["it", "en"]},
    ).json()
    client.post(f"/api/v1/sessions/{created['id']}/status", json={"status": "READY"})
    client.post(f"/api/v1/sessions/{created['id']}/status", json={"status": "LIVE"})
    return created


def test_ingest_produces_persisted_segments(client: TestClient) -> None:
    session = _goto_live(client)
    wav = make_wav([("silence", 0.5), ("speech", 0.6), ("silence", 1.0)])
    resp = client.post(
        f"/api/v1/sessions/{session['id']}/ingest",
        files={"file": ("clip.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["utterances"] == 1
    assert body["segments"][0]["sequence"] == 0
    assert body["segments"][0]["raw_arabic"].startswith("[MOCK")
    assert body["segments"][0]["content_type"] == "GENERAL"

    listed = client.get(f"/api/v1/sessions/{session['id']}/segments").json()
    assert len(listed) == 1
    assert listed[0]["alignment_confidence"] == 0.0  # engine arrives Phase 4


def test_ingest_multiple_utterances_sequence_increments(client: TestClient) -> None:
    session = _goto_live(client)
    wav = make_wav(
        [
            ("silence", 0.3), ("speech", 0.5), ("silence", 0.8),
            ("speech", 0.5), ("silence", 1.0),
        ]
    )
    resp = client.post(
        f"/api/v1/sessions/{session['id']}/ingest",
        files={"file": ("clip.wav", wav, "audio/wav")},
    )
    body = resp.json()
    assert body["utterances"] == 2
    assert [s["sequence"] for s in body["segments"]] == [0, 1]

    # Second ingest continues numbering - mixer stream continuity (§190)
    wav2 = make_wav([("silence", 0.3), ("speech", 0.5), ("silence", 1.0)])
    body2 = client.post(
        f"/api/v1/sessions/{session['id']}/ingest",
        files={"file": ("clip.wav", wav2, "audio/wav")},
    ).json()
    assert body2["segments"][0]["sequence"] == 2


def test_ingest_rejects_wrong_format(client: TestClient) -> None:
    session = _goto_live(client)
    wav = make_wav([("silence", 0.2), ("speech", 0.5)], sample_rate=8000)
    resp = client.post(
        f"/api/v1/sessions/{session['id']}/ingest",
        files={"file": ("clip.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 415  # §184: loud error, no silent resampling


def test_ingest_requires_live_session(client: TestClient) -> None:
    created = client.post(
        "/api/v1/sessions",
        json={"room_id": str(uuid.uuid4()), "active_languages": ["it"]},
    ).json()
    wav = make_wav([("speech", 0.5)])
    resp = client.post(
        f"/api/v1/sessions/{created['id']}/ingest",
        files={"file": ("clip.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 409


def test_ingest_unknown_session_404(client: TestClient) -> None:
    wav = make_wav([("speech", 0.5)])
    resp = client.post(
        f"/api/v1/sessions/{uuid.uuid4()}/ingest",
        files={"file": ("clip.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 404