"""Glossary API (BAYAAN_MASTER_SPEC.md S22, S180 governance)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field

from bayaan_server.db.repository import AuditLogRepository, GlossaryRepository
from bayaan_server.domain.enums import AuditAction
from bayaan_server.pipeline.normalizer import ArabicNormalizerV1
from bayaan_server.auth.admin_auth import verify_admin

router = APIRouter(prefix="/api/v1/glossary", tags=["glossary"])


class GlossaryUpsertRequest(BaseModel):
    term_ar: str = Field(min_length=1)
    translations: dict[str, str] = Field(default_factory=dict)


class GlossaryTermResponse(BaseModel):
    id: uuid.UUID
    term_ar: str
    normalized_ar: str
    translations: dict[str, str]
    approved: bool
    revision: int


def _tout(t) -> GlossaryTermResponse:
    return GlossaryTermResponse(
        id=t.id, term_ar=t.term_ar, normalized_ar=t.normalized_ar,
        translations=t.translations, approved=t.approved, revision=t.revision,
    )


@router.post("/terms", response_model=GlossaryTermResponse, status_code=201)
async def upsert_term(payload: GlossaryUpsertRequest, request: Request, _=Depends(verify_admin)):
    state = request.app.state.service
    normalizer = ArabicNormalizerV1()
    async with state.session_factory() as db:
        term = await GlossaryRepository(db).upsert(
            payload.term_ar, normalizer.normalize(payload.term_ar),
            payload.translations,
        )
        await db.commit()
    return _tout(term)


@router.get("/terms", response_model=list[GlossaryTermResponse])
async def list_terms(request: Request, approved_only: bool = False, _=Depends(verify_admin)):
    state = request.app.state.service
    async with state.session_factory() as db:
        terms = await GlossaryRepository(db).list_terms(approved_only=approved_only)
    return [_tout(t) for t in terms]


@router.post("/terms/{tid}/approve", response_model=GlossaryTermResponse)
async def approve_term(tid: uuid.UUID, request: Request, _=Depends(verify_admin)):
    state = request.app.state.service
    async with state.session_factory() as db:
        try:
            term = await GlossaryRepository(db).approve(tid)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # §98: every glossary approval is audited.
        await AuditLogRepository(db).record(
            AuditAction.GLOSSARY_APPROVED, target_id=term.id,
            metadata={"term_ar": term.term_ar, "revision": term.revision},
        )
        await db.commit()
    return _tout(term)
