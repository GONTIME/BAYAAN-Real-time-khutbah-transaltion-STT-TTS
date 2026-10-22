"""Scripture governance API (BAYAAN_MASTER_SPEC.md §42, §78, §206).

Quran/Hadith are never machine-translated live (§206): they are retrieved from
an approved store. This router lets the operator seed a reference and approve
it (§180-style governance). Only APPROVED verses are reachable by the live
router (§78); an unapproved verse is invisible live (§0 rule 8).

Operator auth is the same open-console situation as the rest of /admin
(ADR-0004 pilot blocker): reachable only on the trusted operator network until
§116 admin auth lands.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel

from bayaan_server.db.repository import AuditLogRepository, ScriptureRepository
from bayaan_server.domain.enums import AuditAction
from bayaan_server.pipeline.normalizer import ArabicNormalizerV1
from bayaan_server.sermon.scripture import strip_quran_decoration
from bayaan_server.auth.admin_auth import verify_admin

router = APIRouter(prefix="/api/v1/scripture", tags=["scripture"])


class ScriptureUpsertRequest(BaseModel):
    kind: str  # "QURAN" | "HADITH"
    key: str   # stable source id, e.g. "2:255"
    arabic: str
    translations: dict[str, str] = {}
    surah: str = ""
    ayah: str = ""
    edition: str = ""
    license: str = ""


class ScriptureResponse(BaseModel):
    id: str
    kind: str
    key: str
    surah: str
    ayah: str
    normalized_arabic: str
    translations: dict[str, str]
    edition: str
    license: str
    approved: bool


def _normalize(arabic: str) -> str:
    # §65 decoration is stripped on BOTH sides of §76 matching so a
    # bracket-marked line still verifies against a verbatim stored verse.
    return strip_quran_decoration(ArabicNormalizerV1().normalize(arabic))


def _to_response(reference) -> ScriptureResponse:
    return ScriptureResponse(
        id=reference.id,
        kind=reference.kind,
        key=reference.key,
        surah=reference.surah,
        ayah=reference.ayah,
        normalized_arabic=reference.normalized_arabic,
        translations=reference.translations or {},
        edition=reference.edition,
        license=reference.license,
        approved=reference.approved,
    )


def _validate_kind(kind: str) -> str:
    upper = kind.upper()
    if upper not in ("QURAN", "HADITH"):
        raise HTTPException(status_code=422, detail="kind must be QURAN or HADITH")
    return upper


@router.post("", response_model=ScriptureResponse, status_code=201)
async def upsert(payload: ScriptureUpsertRequest, request: Request, _=Depends(verify_admin)):
    """Seed or update a scripture reference (unapproved by default)."""
    state = request.app.state.service
    kind = _validate_kind(payload.kind)
    async with state.session_factory() as db:
        verse = await ScriptureRepository(db).upsert(
            kind=kind,
            key=payload.key,
            arabic=payload.arabic,
            normalized_arabic=_normalize(payload.arabic),
            translations=payload.translations,
            surah=payload.surah,
            ayah=payload.ayah,
            edition=payload.edition,
            license=payload.license,
            approved=False,
        )
        await AuditLogRepository(db).record(
            AuditAction.GLOSSARY_UPDATED,  # §98 nearest governance event
            metadata={"scripture": kind, "key": payload.key},
        )
        await db.commit()
    return _to_response(verse)


@router.get("", response_model=list[ScriptureResponse])
async def list_all(
    request: Request,
    kind: Optional[str] = None,
    q: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    _=Depends(verify_admin),
):
    state = request.app.state.service
    async with state.session_factory() as db:
        verses = await ScriptureRepository(db).list_all(
            kind=_validate_kind(kind) if kind else None,
            q=q,
            limit=limit,
            offset=offset,
        )
    return [_to_response(v) for v in verses]



@router.post("/{verse_id}/approve", response_model=ScriptureResponse)
async def approve(verse_id: uuid.UUID, request: Request, _=Depends(verify_admin)):
    """§180-style governance: approval makes the verse reachable live (§78)."""
    state = request.app.state.service
    async with state.session_factory() as db:
        try:
            verse = await ScriptureRepository(db).approve(verse_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await AuditLogRepository(db).record(
            AuditAction.SERMON_APPROVED,  # §98 governance approval trail
            target_id=verse_id,
            metadata={"scripture_key": verse.key},
        )
        await db.commit()
    return _to_response(verse)

