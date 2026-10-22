"""SQLAlchemy ORM tables (Phase 0 of §189, §98 audit log, §64 live models)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Uuid,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bayaan_server.domain.enums import AuditAction, ContentType, SermonStatus, SessionStatus


class Base(DeclarativeBase):
    pass


class RoomRow(Base):
    """Public room entry point (§197 MVP: Room + QR; §116 visitor flow)."""

    __tablename__ = "rooms"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LiveSessionRow(Base):
    __tablename__ = "live_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    room_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    sermon_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default=SessionStatus.CREATED.value, nullable=False
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    active_languages: Mapped[str] = mapped_column(String(64), default="")
    video_feed_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    video_feed_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # ADR-0015: also show the feed on the worshipper room page (phones).
    video_feed_mobile: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LiveSegmentRow(Base):
    __tablename__ = "live_segments"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_live_segments_session_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("live_sessions.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_arabic: Mapped[str] = mapped_column(String, nullable=False)
    normalized_arabic: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(32), default=ContentType.UNKNOWN.value, nullable=False
    )
    source_segment_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    asr_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    alignment_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditLogRow(Base):
    """§98: append-only audit log. Approval records are never silently edited."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    metadata_json: Mapped[str] = mapped_column(String, default="{}")
    immutable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SermonRow(Base):
    """§40 sermons. Title + source file name; status mirrors the data lifecycle (§119)."""

    __tablename__ = "sermons"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_file_name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=SermonStatus.DRAFT.value, nullable=False
    )
    imam_ref: Mapped[Optional[str]] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SermonVersionRow(Base):
    """§40 sermon_versions. content_hash = sha256 over the canonical segment
    form (§155); PUBLISHED rows are immutable (§41)."""

    __tablename__ = "sermon_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    sermon_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sermons.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=SermonStatus.DRAFT.value, nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[Optional[str]] = mapped_column(String(256))


class SermonSegmentRow(Base):
    """§40 sermon_segments: ordered, classified, normalized Arabic."""

    __tablename__ = "sermon_segments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    sermon_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sermon_versions.id"), nullable=False, index=True
    )
    segment_order: Mapped[int] = mapped_column(Integer, nullable=False)
    arabic: Mapped[str] = mapped_column(String, nullable=False)
    normalized_arabic: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(32), default=ContentType.UNKNOWN.value, nullable=False
    )
    source_ref: Mapped[Optional[str]] = mapped_column(String(512))


class TranslationDraftRow(Base):
    """§40 translation_versions + §169 correction capture + §69 provenance."""

    __tablename__ = "translation_drafts"
    # §75: one draft per (segment, language) - a re-run must not duplicate.
    __table_args__ = (
        UniqueConstraint("segment_id", "language", name="uq_draft_segment_language"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    segment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sermon_segments.id"), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    glossary_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    retrieval_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by: Mapped[Optional[str]] = mapped_column(String(256))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    corrected_from: Mapped[Optional[str]] = mapped_column(String)
    correction_reason: Mapped[Optional[str]] = mapped_column(String(256))
    corrected_by: Mapped[Optional[str]] = mapped_column(String(256))
    corrected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GlossaryTermRow(Base):
    """§22 glossary: only approved terms influence output (§180 governance)."""

    __tablename__ = "glossary_terms"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    term_ar: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_ar: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    translations_json: Mapped[str] = mapped_column(String, default="{}", nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScriptureVerseRow(Base):
    """§42.1/§42.2/§206 scripture_verses: an approved, versioned religious
    source with its approved per-language translations. Quran has its own
    repository/policy (§206); it is never machine-translated in the live path.

    ``kind`` is QURAN or HADITH. ``approved`` gates reachability live (§78):
    only approved references may surface (§0 rule 8).
    """

    __tablename__ = "scripture_verses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Stable source id, e.g. "2:255" (surah:ayah) or a hadith collection id.
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    surah: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    ayah: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    arabic: Mapped[str] = mapped_column(String, nullable=False)
    normalized_arabic: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # approved translations per language code, JSON {"it": "...", "en": "..."}.
    translations_json: Mapped[str] = mapped_column(String, default="{}", nullable=False)
    edition: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    license: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )