"""Sermon preparation API (BAYAAN_MASTER_SPEC.md §117, §119, §192)."""

from __future__ import annotations

import uuid
import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bayaan_server.db.repository import (
    AuditLogRepository,
    GlossaryRepository,
    PublishedImmutableError,
    SermonRepository,
)
from bayaan_server.domain.enums import AuditAction, SermonStatus
from bayaan_server.providers.errors import ProviderUnavailableError
from bayaan_server.sermon.glossary import (
    glossary_hits,
    glossary_version,
    glossary_violations,
)
from bayaan_server.sermon.parsers import (
    NoExtractableTextError,
    UnsupportedDocumentError,
)
from bayaan_server.sermon.preparation import PreparationService
from bayaan_server.auth.admin_auth import verify_admin
from bayaan_server.services.export_service import ExportService

router = APIRouter(prefix="/api/v1/sermons", tags=["sermons"])


class SermonResponse(BaseModel):
    id: uuid.UUID
    title: str
    source_file_name: str
    status: SermonStatus
    imam_ref: Optional[str]


class VersionResponse(BaseModel):
    id: uuid.UUID
    sermon_id: uuid.UUID
    version: int
    status: SermonStatus
    content_hash: str
    approved_at: Optional[str]
    approved_by: Optional[str]


class SegmentResponse(BaseModel):
    id: uuid.UUID
    sermon_version_id: uuid.UUID
    segment_order: int
    arabic: str
    normalized_arabic: str
    content_type: str
    source_ref: Optional[str]


class DraftResponse(BaseModel):
    id: uuid.UUID
    segment_id: uuid.UUID
    language: str
    text: str
    provider: str
    model: str
    prompt_version: str
    glossary_version: str
    retrieval_version: str
    approved: bool
    approved_by: Optional[str]
    approved_at: Optional[str]
    corrected_from: Optional[str]
    correction_reason: Optional[str]
    corrected_by: Optional[str]
    corrected_at: Optional[str]


class StatusRequest(BaseModel):
    status: SermonStatus
    approved_by: Optional[str] = None


class DraftEditRequest(BaseModel):
    text: str = Field(min_length=0)
    corrected_by: Optional[str] = None
    correction_reason: Optional[str] = None


class DraftApproveRequest(BaseModel):
    approved_by: Optional[str] = None


class ApproveAllRequest(BaseModel):
    approved_by: Optional[str] = None


def _version_out(v) -> VersionResponse:
    return VersionResponse(
        id=v.id, sermon_id=v.sermon_id, version=v.version, status=v.status,
        content_hash=v.content_hash,
        approved_at=v.approved_at.isoformat() if v.approved_at else None,
        approved_by=v.approved_by,
    )


def _draft_out(d) -> DraftResponse:
    return DraftResponse(
        id=d.id, segment_id=d.segment_id, language=d.language, text=d.text,
        provider=d.provider, model=d.model, prompt_version=d.prompt_version,
        glossary_version=d.glossary_version, retrieval_version=d.retrieval_version,
        approved=d.approved, approved_by=d.approved_by,
        approved_at=d.approved_at.isoformat() if d.approved_at else None,
        corrected_from=d.corrected_from, correction_reason=d.correction_reason,
        corrected_by=d.corrected_by,
        corrected_at=d.corrected_at.isoformat() if d.corrected_at else None,
    )


def _immutable(exc: PublishedImmutableError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


def _prep_service(state, db) -> PreparationService:
    return PreparationService(
        state.settings, SermonRepository(db), GlossaryRepository(db),
        state.provider_set.translation,
    )


@router.post("/upload", response_model=VersionResponse, status_code=201)
async def upload_sermon(
    request: Request, file: UploadFile, title: str = "", _=Depends(verify_admin)
) -> VersionResponse:
    """§117 upload: parse + segment + classify -> DRAFT version 1."""
    state = request.app.state.service
    # BUG-019: bound memory — stream with an early cap instead of
    # buffering an unbounded upload.
    limit = state.settings.sermon.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        piece = await file.read(1024 * 1024)
        if not piece:
            break
        total += len(piece)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"upload too large (limit {limit} bytes)",
            )
        chunks.append(piece)
    data = b"".join(chunks)
    name = file.filename or "sermon.txt"
    async with state.session_factory() as db:
        try:
            version = await _prep_service(state, db).ingest(title or name, name, data)
        except UnsupportedDocumentError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except NoExtractableTextError as exc:
            # §157: image-only PDF is a loud 422, never a silent empty sermon.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            # BUG-011: any other parser ValueError is a client error, not 500.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await AuditLogRepository(db).record(
            AuditAction.SERMON_UPLOADED, target_id=version.sermon_id,
            metadata={"version_id": str(version.id), "filename": name},
        )
        await db.commit()
    return _version_out(version)


@router.get("", response_model=list[SermonResponse])
async def list_sermons(request: Request, _=Depends(verify_admin)) -> list[SermonResponse]:
    state = request.app.state.service
    async with state.session_factory() as db:
        sermons = await SermonRepository(db).list_sermons()
    return [
        SermonResponse(
            id=s.id, title=s.title, source_file_name=s.source_file_name,
            status=s.status, imam_ref=s.imam_ref,
        )
        for s in sermons
    ]


@router.get("/{sermon_id}/versions", response_model=list[VersionResponse])
async def list_versions(
    sermon_id: uuid.UUID, request: Request, _=Depends(verify_admin)
) -> list[VersionResponse]:
    state = request.app.state.service
    async with state.session_factory() as db:
        repo = SermonRepository(db)
        if await repo.get_sermon(sermon_id) is None:
            raise HTTPException(status_code=404, detail="sermon not found")
        versions = await repo.list_versions(sermon_id)
    return [_version_out(v) for v in versions]


@router.get("/versions/{version_id}/segments", response_model=list[SegmentResponse])
async def list_segments(
    version_id: uuid.UUID, request: Request, _=Depends(verify_admin)
) -> list[SegmentResponse]:
    state = request.app.state.service
    async with state.session_factory() as db:
        repo = SermonRepository(db)
        if await repo.get_version(version_id) is None:
            raise HTTPException(status_code=404, detail="sermon version not found")
        segments = await repo.list_segments(version_id)
    return [
        SegmentResponse(
            id=s.id, sermon_version_id=s.sermon_version_id,
            segment_order=s.segment_order, arabic=s.arabic,
            normalized_arabic=s.normalized_arabic,
            content_type=s.content_type.value, source_ref=s.source_ref,
        )
        for s in segments
    ]


@router.post("/versions/{version_id}/drafts", response_model=dict)
async def generate_drafts(version_id: uuid.UUID, request: Request, _=Depends(verify_admin)) -> dict:
    """§75: translate every segment to each target language."""
    state = request.app.state.service
    async with state.session_factory() as db:
        try:
            result = await _prep_service(state, db).generate_drafts(version_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublishedImmutableError as exc:
            raise _immutable(exc) from exc
        except ProviderUnavailableError as exc:
            # §87 SAFE_UNAVAILABLE: every MT provider in the chain failed
            # (e.g. ollama down and no fallback). The live pipeline degrades;
            # this batch endpoint must not 500 or fabricate drafts - return a
            # typed 503 so the console shows why. The failed pass rolls back
            # and a re-click is idempotent (§169), so nothing is half-written.
            raise HTTPException(
                status_code=503,
                detail=(
                    "translation unavailable: every provider in the chain "
                    f"failed ({exc.capability}). Start Ollama/LibreTranslate "
                    "or set BAYAAN_PROVIDERS__TRANSLATION=mock, then retry. "
                    f"Last error: {exc.last_error}"
                ),
            ) from exc
        await db.commit()
    return {
        "version_id": str(version_id),
        "drafts": result.created,
        "created": result.created,
        "skipped": result.skipped,
        "total": result.total,
    }


@router.post("/versions/{version_id}/status", response_model=VersionResponse)
async def transition_version(
    version_id: uuid.UUID, payload: StatusRequest, request: Request, _=Depends(verify_admin)
) -> VersionResponse:
    """Lifecycle (§119): publish requires every draft approved first."""
    state = request.app.state.service
    async with state.session_factory() as db:
        repo = SermonRepository(db)
        version = await repo.get_version(version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="sermon version not found")
        if payload.status is SermonStatus.PUBLISHED:
            unapproved = 0
            for segment in await repo.list_segments(version_id):
                for draft in await repo.list_drafts(segment.id):
                    if not draft.approved:
                        unapproved += 1
            if unapproved:
                raise HTTPException(
                    status_code=409,
                    detail=f"{unapproved} drafts not approved; "
                    "approve every draft before publishing",
                )
        try:
            updated = await repo.transition_version(
                version_id, payload.status, approved_by=payload.approved_by,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PublishedImmutableError as exc:
            raise _immutable(exc) from exc
        if payload.status is SermonStatus.PUBLISHED:
            await repo.mark_sermon_published(
                version.sermon_id, approved_by=payload.approved_by
            )
        await AuditLogRepository(db).record(
            AuditAction.SERMON_PUBLISHED
            if payload.status is SermonStatus.PUBLISHED
            else AuditAction.SERMON_APPROVED,
            target_id=version_id,
            metadata={"status": payload.status.value, "by": payload.approved_by},
        )
        await db.commit()
    return _version_out(updated)


@router.get("/segments/{segment_id}/drafts", response_model=list[DraftResponse])
async def list_segment_drafts(
    segment_id: uuid.UUID, request: Request, _=Depends(verify_admin)
) -> list[DraftResponse]:
    state = request.app.state.service
    async with state.session_factory() as db:
        drafts = await SermonRepository(db).list_drafts(segment_id)
    return [_draft_out(d) for d in drafts]


@router.patch("/drafts/{draft_id}", response_model=DraftResponse)
async def edit_draft(
    draft_id: uuid.UUID, payload: DraftEditRequest, request: Request, _=Depends(verify_admin)
) -> DraftResponse:
    """§96 [Edit] + §169: before/after/reason/reviewer/timestamp kept."""
    state = request.app.state.service
    async with state.session_factory() as db:
        try:
            updated = await SermonRepository(db).update_draft_text(
                draft_id, payload.text, corrected_by=payload.corrected_by,
                correction_reason=payload.correction_reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublishedImmutableError as exc:
            raise _immutable(exc) from exc
        await db.commit()
    return _draft_out(updated)


@router.delete("/drafts/{draft_id}", status_code=204)
async def delete_draft(
    draft_id: uuid.UUID, request: Request, _=Depends(verify_admin)
) -> Response:
    """Delete a single translation draft (§96 review-edit lifecycle).

    §41: a draft belonging to a PUBLISHED version is immutable and is refused (409).
    """
    state = request.app.state.service
    async with state.session_factory() as db:
        try:
            await SermonRepository(db).delete_draft(draft_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublishedImmutableError as exc:
            raise _immutable(exc) from exc
        await AuditLogRepository(db).record(
            AuditAction.SERMON_DELETED, target_id=draft_id,
            metadata={"type": "draft"},
        )
        await db.commit()
    return Response(status_code=204)


@router.post("/drafts/{draft_id}/approve", response_model=DraftResponse)
async def approve_draft(
    draft_id: uuid.UUID, payload: DraftApproveRequest, request: Request, _=Depends(verify_admin)
) -> DraftResponse:
    """§96 [Approve]: records approved_by/approved_at; never silent."""
    state = request.app.state.service
    async with state.session_factory() as db:
        try:
            updated = await SermonRepository(db).approve_draft(
                draft_id, approved_by=payload.approved_by
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublishedImmutableError as exc:
            raise _immutable(exc) from exc
        await AuditLogRepository(db).record(
            AuditAction.SERMON_APPROVED, target_id=draft_id,
            metadata={"by": payload.approved_by},
        )
        await db.commit()
    return _draft_out(updated)


@router.post("/versions/{version_id}/approve-all", response_model=dict)
async def approve_all_drafts(
    version_id: uuid.UUID, payload: ApproveAllRequest, request: Request, _=Depends(verify_admin)
) -> dict:
    """§96: approve every draft of a version in one pass (never silent)."""
    state = request.app.state.service
    async with state.session_factory() as db:
        repo = SermonRepository(db)
        if await repo.get_version(version_id) is None:
            raise HTTPException(status_code=404, detail="sermon version not found")
        try:
            approved = await repo.approve_all_for_version(
                version_id, approved_by=payload.approved_by
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PublishedImmutableError as exc:
            raise _immutable(exc) from exc
        await AuditLogRepository(db).record(
            AuditAction.SERMON_APPROVED, target_id=version_id,
            metadata={"by": payload.approved_by, "count": approved, "scope": "all-drafts"},
        )
        await db.commit()
    return {"version_id": str(version_id), "approved": approved}


@router.get("/versions/{version_id}/export/{fmt_type}/{lang}", response_class=Response)
async def export_version(
    version_id: uuid.UUID, fmt_type: str, lang: str, request: Request, _=Depends(verify_admin)
) -> Response:
    """Export a PUBLISHED version to SRT/VTT (§191).

    BUG-007: format validated (422); unapproved/missing drafts skipped, 409
    when none exportable; timings are honest document-order estimates.
    """
    fmt = fmt_type.lower()
    if fmt not in ("srt", "vtt"):
        raise HTTPException(
            status_code=422, detail=f"unsupported export format: {fmt_type} (srt/vtt only)"
        )
    state = request.app.state.service
    async with state.session_factory() as db:
        repo = SermonRepository(db)
        version = await repo.get_version(version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="version not found")
        if version.status is not SermonStatus.PUBLISHED:
            raise HTTPException(
                status_code=403, detail="only PUBLISHED versions can be exported"
            )
        
        segments = await repo.list_segments(version_id)
        ordered = sorted(segments, key=lambda s: s.segment_order)
        export_data = []
        for idx, seg in enumerate(ordered):
            drafts = await repo.list_drafts(seg.id)
            approved_draft = next((d for d in drafts if d.language == lang and d.approved), None)
            if approved_draft is None or not approved_draft.text.strip():
                continue
            export_data.append({
                "start": idx * 5,
                "end": idx * 5 + 5,
                "text": approved_draft.text,
            })
        if ordered and not export_data:
            raise HTTPException(
                status_code=409,
                detail=f"no approved '{lang}' translations available for export",
            )
        
    service = ExportService()
    
    class SimpleSegment:
        def __init__(self, start, end, drafts):
            self.start = start
            self.end = end
            self.drafts = drafts
            
    class MockDraft:
        def __init__(self, lang, text):
            self.language = lang
            self.text = text
            self.approved = True
            
    formatted_segments = [
        SimpleSegment(
            datetime.timedelta(seconds=item["start"]),
            datetime.timedelta(seconds=item["end"]),
            [MockDraft(lang, item["text"])]
        )
        for item in export_data
    ]
    
    content = service.export_sermon(formatted_segments, lang, fmt, header_note="NOTE: timings are estimates in document order, not recorded timestamps.")
    filename = f"sermon_{version_id}.{fmt}"
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@router.delete("/{sermon_id}", status_code=204)
async def delete_sermon(sermon_id: uuid.UUID, request: Request, _=Depends(verify_admin)) -> Response:
    """Delete an uploaded sermon (§119 DELETED under retention). §41: a sermon
    with a PUBLISHED version is immutable and is refused (409)."""
    state = request.app.state.service
    async with state.session_factory() as db:
        repo = SermonRepository(db)
        try:
            await repo.delete_sermon(sermon_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublishedImmutableError as exc:
            raise _immutable(exc) from exc
        await AuditLogRepository(db).record(
            AuditAction.SERMON_DELETED, target_id=sermon_id, metadata={},
        )
        await db.commit()
    return Response(status_code=204)


@router.get("/segments/{segment_id}/review", response_model=dict)
async def review_panel(segment_id: uuid.UUID, request: Request, _=Depends(verify_admin)) -> dict:
    """§96 panel: Arabic source, per-language drafts, glossary hits, warnings."""
    state = request.app.state.service
    async with state.session_factory() as db:
        repo = SermonRepository(db)
        glossary = GlossaryRepository(db)
        segment = await repo.get_segment(segment_id)
        if segment is None:
            raise HTTPException(status_code=404, detail="segment not found")
        drafts = [_draft_out(d) for d in await repo.list_drafts(segment_id)]
        terms = await glossary.list_terms(approved_only=True)
        revision = await glossary.current_revision()
    # Glossary hits run against the Arabic SOURCE (terms are Arabic); a
    # violation is an approved source term whose approved target translation
    # is missing from the draft (§22/§150).
    hits = [t.term_ar for t in glossary_hits(segment.normalized_arabic, terms)]
    warnings: list[str] = []
    for draft in drafts:
        warnings.extend(
            glossary_violations(
                segment.normalized_arabic, draft.text, draft.language, terms
            )
        )
    # §79 Phase 4: run the full validator chain per draft so the review panel
    # (§96) surfaces number/negation/entity/scripture warnings too, not just
    # glossary. Each warning is explainable to the reviewer (§168).
    from bayaan_server.pipeline.validators import Validator

    validator = Validator(state.settings.validation)
    validation: list[dict] = []
    for draft in drafts:
        verdict = validator.validate(
            segment.arabic, draft.text, draft.language,
            content_type=segment.content_type.value,
            glossary_terms=terms,
        )
        validation.append({
            "language": draft.language,
            "decision": verdict.decision.value,
            "risk": verdict.risk,
            "risk_level": verdict.risk_level.value,
            "warnings": [
                {"code": w.code, "message": w.message, "severity": w.severity.value}
                for w in verdict.warnings
            ],
        })
        warnings.extend(w.message for w in verdict.warnings)
    return {
        "segment_order": segment.segment_order,
        "content_type": segment.content_type.value,
        "arabic": segment.arabic,
        "drafts": [d.model_dump(mode="json") for d in drafts],
        "glossary_hits": hits,
        "warnings": warnings,
        "validation": validation,
        "glossary_version": glossary_version(revision),
    }

