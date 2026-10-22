"""Sermon preparation domain models (BAYAAN_MASTER_SPEC.md §40, §41, §155).

Every approved output carries provenance (§69) and approval records are
never silently edited (§96 panel, §169 correction capture).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .enums import ContentType, SermonStatus


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Sermon(BaseModel):
    """§40 sermons table. Imam is an optional non-sensitive id only (§155)."""

    id: uuid.UUID = Field(default_factory=new_uuid)
    title: str
    source_file_name: str = ""
    status: SermonStatus = SermonStatus.DRAFT
    imam_ref: Optional[str] = None


class SermonVersion(BaseModel):
    """§40 sermon_versions. content_hash is sha256 over the canonical
    segment form; PUBLISHED rows are immutable (§41)."""

    id: uuid.UUID = Field(default_factory=new_uuid)
    sermon_id: uuid.UUID
    version: int = Field(ge=1)
    status: SermonStatus = SermonStatus.DRAFT
    content_hash: str = ""
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None


class SermonSegment(BaseModel):
    """§40 sermon_segments: ordered, classified, normalized Arabic."""

    id: uuid.UUID = Field(default_factory=new_uuid)
    sermon_version_id: uuid.UUID
    segment_order: int = Field(ge=0)
    arabic: str
    normalized_arabic: str
    content_type: ContentType = ContentType.UNKNOWN
    source_ref: Optional[str] = None


class TranslationDraft(BaseModel):
    """Per-segment per-language draft with §69 provenance and §169
    correction capture. approved_* are set only via the review flow."""

    id: uuid.UUID = Field(default_factory=new_uuid)
    segment_id: uuid.UUID
    language: str
    text: str
    provider: str
    model: str
    prompt_version: str
    glossary_version: str
    retrieval_version: str
    approved: bool = False
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    corrected_from: Optional[str] = None
    correction_reason: Optional[str] = None
    corrected_by: Optional[str] = None
    corrected_at: Optional[datetime] = None


class GlossaryTerm(BaseModel):
    """Approved terminology (§22 glossary checks, §180 governance).

    Only approved terms influence preparation/validation; the term key
    is normalized Arabic. translations maps target language -> term.
    """

    id: uuid.UUID = Field(default_factory=new_uuid)
    term_ar: str
    normalized_ar: str
    translations: dict[str, str] = Field(default_factory=dict)
    approved: bool = False
    revision: int = Field(default=0, ge=0)
