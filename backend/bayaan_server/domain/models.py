"""Domain models (BAYAAN_MASTER_SPEC.md §64, §166, §167).

These are the immutable contracts of Phase 0: AI integration is forbidden
until these are stable (§189).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .enums import ContentType, SafetyDecision, SessionStatus


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class LiveSession(BaseModel):
    """§64 LiveSession."""

    id: uuid.UUID = Field(default_factory=new_uuid)
    room_id: uuid.UUID
    sermon_version_id: Optional[uuid.UUID] = None
    status: SessionStatus = SessionStatus.CREATED
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    active_languages: list[str] = Field(default_factory=list)
    video_feed_url: Optional[str] = None
    video_feed_kind: Optional[str] = None
    # ADR-0015: when True the video feed is also shown on the worshipper room
    # page (phones), not just the projector screen. Default False: phones stay
    # captions-only - the operator opts in per session.
    video_feed_mobile: bool = False


class LiveSegment(BaseModel):
    """§64 LiveSegment.

    §166: ASR confidence, alignment confidence, translation confidence and
    safety score are stored separately - never collapsed into one number.
    """

    id: uuid.UUID = Field(default_factory=new_uuid)
    session_id: uuid.UUID
    sequence: int = Field(ge=0)
    raw_arabic: str
    normalized_arabic: str
    content_type: ContentType = ContentType.UNKNOWN
    source_segment_id: Optional[uuid.UUID] = None
    asr_confidence: float = Field(ge=0.0, le=1.0)
    alignment_confidence: float = Field(ge=0.0, le=1.0)


class ConfidenceBreakdown(BaseModel):
    """§166: the four confidence signals are distinct.

    A signal is `None` when its engine has not run yet (alignment Phase 4,
    validators Phase 4). None is different from 0.0: absent means "not
    evaluated", never "zero quality" (§0 rule 8).
    """

    asr_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    alignment_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    translation_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    validation_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class SafetyVerdict(BaseModel):
    """Result of the safety engine for one candidate output (§107, §167)."""

    decision: SafetyDecision
    overall_confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)