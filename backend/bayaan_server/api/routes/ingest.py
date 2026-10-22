"""WAV ingest endpoint (BAYAAN_MASTER_SPEC.md §190).

Runs a recorded mixer input (or golden-dataset clip) through the live
pipeline: VAD -> ASR -> normalization -> rule detection -> persisted
segments. The WebSocket stream (Phase 2, §191) reuses the same service.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from bayaan_server.audio.format import PcmFormat
from bayaan_server.audio.sources import AudioFormatError, parse_wav
from bayaan_server.auth.admin_auth import verify_admin
from bayaan_server.auth.admin_auth import verify_admin
from bayaan_server.domain.enums import SessionStatus
from bayaan_server.db.repository import LiveSessionRepository
from bayaan_server.pipeline.live_pipeline import IngestResult

router = APIRouter(prefix="/api/v1/sessions", tags=["ingest"])

MAX_WAV_BYTES = 50 * 1024 * 1024  # bounded upload (§115 rate limiting spirit)


@router.post("/{session_id}/ingest", response_model=IngestResult)
async def ingest_wav(
    session_id: uuid.UUID, request: Request, file: UploadFile, _=Depends(verify_admin)
) -> IngestResult:
    state = request.app.state.service
    chunks: list[bytes] = []
    total = 0
    while True:
        piece = await file.read(1024 * 1024)
        if not piece:
            break
        total += len(piece)
        if total > MAX_WAV_BYTES:
            raise HTTPException(status_code=413, detail="wav file too large")
        chunks.append(piece)
    data = b"".join(chunks)

    async with state.session_factory() as db:
        session = await LiveSessionRepository(db).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.status is not SessionStatus.LIVE:
        raise HTTPException(
            status_code=409,
            detail=f"session must be LIVE to ingest (current: {session.status.value})",
        )

    fmt = PcmFormat(
        sample_rate=state.settings.audio.sample_rate,
        channels=state.settings.audio.channels,
    )
    try:
        pcm = parse_wav(data, fmt)
    except AudioFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    return await state.ingest_service.ingest_pcm(session_id, state.session_factory, pcm)