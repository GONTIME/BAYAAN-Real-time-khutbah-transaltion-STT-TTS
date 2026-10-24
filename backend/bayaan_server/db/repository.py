"""Repositories: the only sanctioned persistence path (§187 No Hidden
Persistence). Services must never write files/rows directly."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bayaan_server.domain.enums import AuditAction, ContentType, SermonStatus, SessionStatus
from bayaan_server.domain.models import LiveSegment, LiveSession
from bayaan_server.domain.sermon import (
    GlossaryTerm,
    Sermon,
    SermonSegment,
    SermonVersion,
    TranslationDraft,
)
from .orm import (
    AuditLogRow,
    GlossaryTermRow,
    LiveSegmentRow,
    LiveSessionRow,
    RoomRow,
    SermonRow,
    SermonSegmentRow,
    SermonVersionRow,
    ScriptureVerseRow,
    TranslationDraftRow,
)


def _to_domain_session(row: LiveSessionRow) -> LiveSession:
    return LiveSession(
        id=row.id,
        room_id=row.room_id,
        sermon_version_id=row.sermon_version_id,
        status=SessionStatus(row.status),
        started_at=row.started_at,
        ended_at=row.ended_at,
        active_languages=[lang for lang in row.active_languages.split(",") if lang],
        video_feed_url=row.video_feed_url,
        video_feed_kind=row.video_feed_kind,
        video_feed_mobile=row.video_feed_mobile,
    )


class RoomRepository:
    """Public rooms (§197 Room/QR). Rooms are referenced by short codes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, code_length: int) -> tuple[uuid.UUID, str]:
        # Collision retry with unique code constraint - bounded attempts.
        for _ in range(5):
            room_id = uuid.uuid4()
            code = secrets.token_hex(max(2, code_length // 2))[:code_length].upper()
            row = RoomRow(id=room_id, code=code)
            self._session.add(row)
            try:
                await self._session.flush()
                return room_id, code
            except IntegrityError:
                await self._session.rollback()
        raise RuntimeError("could not allocate a unique room code")

    async def get(self, room_id: uuid.UUID) -> RoomRow | None:
        return await self._session.get(RoomRow, room_id)

    async def get_by_code(self, code: str) -> RoomRow | None:
        stmt = select(RoomRow).where(RoomRow.code == code)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def link_session(self, room_id: uuid.UUID, session_id: uuid.UUID) -> None:
        row = await self._session.get(RoomRow, room_id)
        if row is None:
            raise ValueError("room not found")
        row.session_id = session_id
        await self._session.flush()


class LiveSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, domain: LiveSession) -> LiveSession:
        row = LiveSessionRow(
            id=domain.id,
            room_id=domain.room_id,
            sermon_version_id=domain.sermon_version_id,
            status=domain.status.value,
            started_at=domain.started_at,
            ended_at=domain.ended_at,
            active_languages=",".join(domain.active_languages),
        )
        self._session.add(row)
        await self._session.flush()
        return domain

    async def get(self, session_id: uuid.UUID) -> LiveSession | None:
        row = await self._session.get(LiveSessionRow, session_id)
        return _to_domain_session(row) if row else None

    async def list_active(self, limit: int) -> list[LiveSession]:
        stmt = (
            select(LiveSessionRow)
            .where(LiveSessionRow.status.in_([SessionStatus.READY.value, SessionStatus.LIVE.value]))
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain_session(r) for r in rows]

    async def count_active(self) -> int:
        # BUG-018: COUNT in the database — never materialize all rows.
        stmt = select(func.count()).select_from(LiveSessionRow).where(
            LiveSessionRow.status.in_([SessionStatus.READY.value, SessionStatus.LIVE.value])
        )
        value = await self._session.scalar(stmt)
        return int(value or 0)

    async def update_room(self, session_id: uuid.UUID, room_id: uuid.UUID) -> None:
        row = await self._session.get(LiveSessionRow, session_id)
        if row is None:
            raise ValueError("session not found")
        row.room_id = room_id
        await self._session.flush()

    async def update_status(
        self, session_id: uuid.UUID, status: SessionStatus
    ) -> LiveSession | None:
        row = await self._session.get(LiveSessionRow, session_id)
        if row is None:
            return None
        row.status = status.value
        now = datetime.now(timezone.utc)
        if status is SessionStatus.LIVE and row.started_at is None:
            row.started_at = now
        if status in (SessionStatus.ENDED, SessionStatus.FAILED):
            row.ended_at = now
        await self._session.flush()
        return _to_domain_session(row)

    async def update_video_feed(
        self, session_id: uuid.UUID, url: str | None, kind: str | None = "hls",
        mobile: bool = False,
    ) -> LiveSession | None:
        row = await self._session.get(LiveSessionRow, session_id)
        if row is None:
            return None
        row.video_feed_url = url
        row.video_feed_kind = kind
        row.video_feed_mobile = mobile
        await self._session.flush()
        return _to_domain_session(row)


class LiveSegmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, domain: LiveSegment) -> LiveSegment:
        row = LiveSegmentRow(
            id=domain.id,
            session_id=domain.session_id,
            sequence=domain.sequence,
            raw_arabic=domain.raw_arabic,
            normalized_arabic=domain.normalized_arabic,
            content_type=domain.content_type.value,
            source_segment_id=domain.source_segment_id,
            asr_confidence=domain.asr_confidence,
            alignment_confidence=domain.alignment_confidence,
        )
        self._session.add(row)
        await self._session.flush()
        return domain

    async def next_sequence(self, session_id: uuid.UUID) -> int:
        stmt = select(func.max(LiveSegmentRow.sequence)).where(
            LiveSegmentRow.session_id == session_id
        )
        value = await self._session.scalar(stmt)
        return (value if value is not None else -1) + 1

    async def list_for_session(self, session_id: uuid.UUID) -> list[LiveSegment]:
        stmt = (
            select(LiveSegmentRow)
            .where(LiveSegmentRow.session_id == session_id)
            .order_by(LiveSegmentRow.sequence)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            LiveSegment(
                id=r.id,
                session_id=r.session_id,
                sequence=r.sequence,
                raw_arabic=r.raw_arabic,
                normalized_arabic=r.normalized_arabic,
                content_type=ContentType(r.content_type),
                source_segment_id=r.source_segment_id,
                asr_confidence=r.asr_confidence,
                alignment_confidence=r.alignment_confidence,
            )
            for r in rows
        ]


class AuditLogRepository:
    """§98 audit log. Append-only by contract."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        action: AuditAction,
        *,
        actor_id: uuid.UUID | None = None,
        target_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> None:
        self._session.add(
            AuditLogRow(
                id=uuid.uuid4(),
                actor_id=actor_id,
                action=action.value,
                target_id=target_id,
                metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            )
        )

    async def list_recent(
        self, actions: list[AuditAction], limit: int = 20
    ) -> list[dict]:
        """Newest audit entries for the given actions (§98 read side).

        Used by the operator diagnostics to explain WHY a line was blocked: the
        public room only ever sees the §134 fallback message, while the operator
        gets the reason the gate recorded (§168). Read-only - the log itself
        stays append-only.
        """
        from sqlalchemy import select

        stmt = (
            select(AuditLogRow)
            .where(AuditLogRow.action.in_([action.value for action in actions]))
            .order_by(AuditLogRow.timestamp.desc())
            .limit(max(1, min(limit, 200)))
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        entries: list[dict] = []
        for row in rows:
            try:
                metadata = json.loads(row.metadata_json or "{}")
            except json.JSONDecodeError:
                # A corrupt metadata blob is never guessed at (§0 rule 8); the
                # entry is still listed, with the raw text kept for the operator.
                metadata = {"raw": row.metadata_json}
            entries.append(
                {
                    "id": str(row.id),
                    "action": row.action,
                    "target_id": str(row.target_id) if row.target_id else None,
                    "timestamp": (
                        row.timestamp.isoformat() if row.timestamp else None
                    ),
                    "metadata": metadata,
                }
            )
        return entries


class PublishedImmutableError(RuntimeError):
    """§41: a PUBLISHED version must never be modified in place."""


def _sermon_status(value: str) -> SermonStatus:
    return SermonStatus(value)


def _to_domain_sermon(row: SermonRow) -> Sermon:
    return Sermon(
        id=row.id,
        title=row.title,
        source_file_name=row.source_file_name,
        status=_sermon_status(row.status),
        imam_ref=row.imam_ref,
    )


def _to_domain_version(row: SermonVersionRow) -> SermonVersion:
    return SermonVersion(
        id=row.id,
        sermon_id=row.sermon_id,
        version=row.version,
        status=_sermon_status(row.status),
        content_hash=row.content_hash,
        approved_at=row.approved_at,
        approved_by=row.approved_by,
    )


def _to_domain_segment(row: SermonSegmentRow) -> SermonSegment:
    return SermonSegment(
        id=row.id,
        sermon_version_id=row.sermon_version_id,
        segment_order=row.segment_order,
        arabic=row.arabic,
        normalized_arabic=row.normalized_arabic,
        content_type=ContentType(row.content_type),
        source_ref=row.source_ref,
    )


def _to_domain_draft(row: TranslationDraftRow) -> TranslationDraft:
    return TranslationDraft(
        id=row.id,
        segment_id=row.segment_id,
        language=row.language,
        text=row.text,
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        glossary_version=row.glossary_version,
        retrieval_version=row.retrieval_version,
        approved=row.approved,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        corrected_from=row.corrected_from,
        correction_reason=row.correction_reason,
        corrected_by=row.corrected_by,
        corrected_at=row.corrected_at,
    )


def _to_domain_term(row: GlossaryTermRow) -> GlossaryTerm:
    return GlossaryTerm(
        id=row.id,
        term_ar=row.term_ar,
        normalized_ar=row.normalized_ar,
        translations=json.loads(row.translations_json or "{}"),
        approved=row.approved,
        revision=row.revision,
    )



class SermonRepository:
    """Sermon aggregates (§40). All lifecycle transitions go through here."""

    _ALLOWED: dict[SermonStatus, set[SermonStatus]] = {
        SermonStatus.DRAFT: {SermonStatus.REVIEW, SermonStatus.ARCHIVED},
        SermonStatus.REVIEW: {SermonStatus.APPROVED, SermonStatus.DRAFT},
        SermonStatus.APPROVED: {SermonStatus.PUBLISHED, SermonStatus.DRAFT},
        SermonStatus.PUBLISHED: {SermonStatus.ARCHIVED},
        SermonStatus.ARCHIVED: set(),
    }

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_sermon(
        self, title: str, source_file_name: str = "", imam_ref: str | None = None
    ) -> Sermon:
        domain = Sermon(
            title=title, source_file_name=source_file_name, imam_ref=imam_ref
        )
        self._session.add(
            SermonRow(
                id=domain.id,
                title=domain.title,
                source_file_name=domain.source_file_name,
                status=domain.status.value,
                imam_ref=domain.imam_ref,
            )
        )
        await self._session.flush()
        return domain

    async def get_sermon(self, sermon_id: uuid.UUID) -> Sermon | None:
        row = await self._session.get(SermonRow, sermon_id)
        return _to_domain_sermon(row) if row else None

    async def list_sermons(self) -> list[Sermon]:
        rows = (await self._session.execute(select(SermonRow))).scalars().all()
        return [_to_domain_sermon(r) for r in rows]

    async def transition_sermon(
        self, sermon_id: uuid.UUID, target: SermonStatus
    ) -> Sermon | None:
        row = await self._session.get(SermonRow, sermon_id)
        if row is None:
            return None
        current = _sermon_status(row.status)
        if target not in self._ALLOWED.get(current, set()):
            raise ValueError(f"transition {current.value} -> {target.value} not allowed")
        row.status = target.value
        await self._session.flush()
        return _to_domain_sermon(row)

    async def mark_sermon_published(
        self, sermon_id: uuid.UUID, *, approved_by: str | None = None
    ) -> Sermon:
        """Roll the sermon aggregate up to PUBLISHED.

        The sermon-level status is a roll-up of its versions (§119). The
        real guard is the version lifecycle (DRAFT -> REVIEW -> APPROVED ->
        PUBLISHED), which has already passed by the time this is called; the
        sermon itself is therefore set directly rather than re-walking every
        intermediate step. ARCHIVED remains reachable from PUBLISHED.
        """
        row = await self._session.get(SermonRow, sermon_id)
        if row is None:
            raise ValueError("sermon not found")
        row.status = SermonStatus.PUBLISHED.value
        await self._session.flush()
        _ = approved_by  # approval provenance lives on the version (§41)
        return _to_domain_sermon(row)


    async def create_version(
        self, sermon_id: uuid.UUID, content_hash: str, status: SermonStatus
    ) -> SermonVersion:
        stmt = select(func.max(SermonVersionRow.version)).where(
            SermonVersionRow.sermon_id == sermon_id
        )
        highest = await self._session.scalar(stmt)
        domain = SermonVersion(
            sermon_id=sermon_id,
            version=(highest or 0) + 1,
            status=status,
            content_hash=content_hash,
        )
        self._session.add(
            SermonVersionRow(
                id=domain.id,
                sermon_id=domain.sermon_id,
                version=domain.version,
                status=domain.status.value,
                content_hash=domain.content_hash,
            )
        )
        await self._session.flush()
        return domain

    async def get_version(self, version_id: uuid.UUID) -> SermonVersion | None:
        row = await self._session.get(SermonVersionRow, version_id)
        return _to_domain_version(row) if row else None

    async def list_versions(self, sermon_id: uuid.UUID) -> list[SermonVersion]:
        stmt = (
            select(SermonVersionRow)
            .where(SermonVersionRow.sermon_id == sermon_id)
            .order_by(SermonVersionRow.version)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain_version(r) for r in rows]

    async def _guard_mutable(self, version_id: uuid.UUID) -> SermonVersionRow:
        row = await self._session.get(SermonVersionRow, version_id)
        if row is None:
            raise ValueError("sermon version not found")
        if row.status == SermonStatus.PUBLISHED.value:
            # §41: never modify a published version in place.
            raise PublishedImmutableError("published versions are immutable")
        return row

    async def transition_version(
        self, version_id: uuid.UUID, target: SermonStatus, *,
        approved_by: str | None = None,
    ) -> SermonVersion:
        row = await self._session.get(SermonVersionRow, version_id)
        if row is None:
            raise ValueError("sermon version not found")
        # Lifecycle is governed by the state machine below; content writes
        # are guarded separately. PUBLISHED -> ARCHIVED stays allowed here.
        current = _sermon_status(row.status)
        if target not in self._ALLOWED.get(current, set()):
            raise ValueError(
                f"transition {current.value} -> {target.value} not allowed"
            )
        row.status = target.value
        if target is SermonStatus.APPROVED:
            # §96: the approval record is written at the APPROVE step.
            row.approved_at = datetime.now(timezone.utc)
            row.approved_by = approved_by
        elif target is SermonStatus.PUBLISHED and approved_by is not None:
            # §96/§41: never silently edit the approval record - preserve the
            # one captured at APPROVED unless a new one is explicitly supplied.
            row.approved_at = row.approved_at or datetime.now(timezone.utc)
            row.approved_by = approved_by
        await self._session.flush()
        return _to_domain_version(row)

    async def add_segments(
        self, version_id: uuid.UUID, segments: list[SermonSegment]
    ) -> list[SermonSegment]:
        await self._guard_mutable(version_id)
        for seg in segments:
            self._session.add(
                SermonSegmentRow(
                    id=seg.id,
                    sermon_version_id=version_id,
                    segment_order=seg.segment_order,
                    arabic=seg.arabic,
                    normalized_arabic=seg.normalized_arabic,
                    content_type=seg.content_type.value,
                    source_ref=seg.source_ref,
                )
            )
        await self._session.flush()
        return segments

    async def list_segments(self, version_id: uuid.UUID) -> list[SermonSegment]:
        stmt = (
            select(SermonSegmentRow)
            .where(SermonSegmentRow.sermon_version_id == version_id)
            .order_by(SermonSegmentRow.segment_order)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain_segment(r) for r in rows]

    async def get_segment(self, segment_id: uuid.UUID) -> SermonSegment | None:
        row = await self._session.get(SermonSegmentRow, segment_id)
        return _to_domain_segment(row) if row else None

    async def save_draft(self, draft: TranslationDraft) -> TranslationDraft:
        segment = await self._session.get(SermonSegmentRow, draft.segment_id)
        if segment is None:
            raise ValueError("segment not found")
        await self._guard_mutable(segment.sermon_version_id)
        self._session.add(
            TranslationDraftRow(
                id=draft.id,
                segment_id=draft.segment_id,
                language=draft.language,
                text=draft.text,
                provider=draft.provider,
                model=draft.model,
                prompt_version=draft.prompt_version,
                glossary_version=draft.glossary_version,
                retrieval_version=draft.retrieval_version,
                approved=draft.approved,
            )
        )
        await self._session.flush()
        return draft

    async def list_drafts(self, segment_id: uuid.UUID) -> list[TranslationDraft]:
        stmt = select(TranslationDraftRow).where(
            TranslationDraftRow.segment_id == segment_id
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain_draft(r) for r in rows]

    async def get_approved_draft(
        self, segment_id: uuid.UUID, language: str
    ) -> TranslationDraft | None:
        """§78: the prepared approved translation for live reuse."""
        stmt = (
            select(TranslationDraftRow)
            .where(
                TranslationDraftRow.segment_id == segment_id,
                TranslationDraftRow.language == language,
                TranslationDraftRow.approved.is_(True),
            )
            .order_by(TranslationDraftRow.created_at.desc())
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_domain_draft(row) if row else None

    async def update_draft_text(
        self, draft_id: uuid.UUID, text: str, *,
        corrected_by: str | None = None,
        correction_reason: str | None = None,
    ) -> TranslationDraft:
        """§169: reviewer edit keeps before/after/reason/reviewer/timestamp."""
        row = await self._session.get(TranslationDraftRow, draft_id)
        if row is None:
            raise ValueError("draft not found")
        segment = await self._session.get(SermonSegmentRow, row.segment_id)
        if segment is not None:
            await self._guard_mutable(segment.sermon_version_id)
        row.corrected_from = row.text
        row.corrected_by = corrected_by
        row.correction_reason = correction_reason
        row.corrected_at = datetime.now(timezone.utc)
        row.text = text
        row.approved = False  # edited text loses approval until re-approved
        await self._session.flush()
        return _to_domain_draft(row)

    async def approve_draft(
        self, draft_id: uuid.UUID, approved_by: str | None = None
    ) -> TranslationDraft:
        """§96 panel: Approve records approved_by/approved_at."""
        row = await self._session.get(TranslationDraftRow, draft_id)
        if row is None:
            raise ValueError("draft not found")
        segment = await self._session.get(SermonSegmentRow, row.segment_id)
        if segment is not None:
            await self._guard_mutable(segment.sermon_version_id)
        row.approved = True
        row.approved_by = approved_by
        row.approved_at = datetime.now(timezone.utc)
        await self._session.flush()
        return _to_domain_draft(row)

    async def approve_all_for_version(
        self, version_id: uuid.UUID, approved_by: str | None = None
    ) -> int:
        """§96: approve every draft of a version in one pass. Publishes nothing;
        it only records the reviewer's approval per draft (§119)."""
        # §41: a published version is immutable - refuse the batch.
        await self._guard_mutable(version_id)
        segments = await self.list_segments(version_id)
        approved = 0
        now = datetime.now(timezone.utc)
        for segment in segments:
            rows = (
                await self._session.execute(
                    select(TranslationDraftRow).where(
                        TranslationDraftRow.segment_id == segment.id
                    )
                )
            ).scalars().all()
            for row in rows:
                if not row.approved:
                    row.approved = True
                    row.approved_by = approved_by
                    row.approved_at = now
                    approved += 1
        await self._session.flush()
        return approved

    async def delete_sermon(self, sermon_id: uuid.UUID) -> None:
        """Delete an uploaded sermon and all its versions/segments/drafts.

        §41: a sermon with any PUBLISHED version is immutable and is never
        deleted - the caller must archive it instead. Deletion cascades
        drafts -> segments -> versions -> sermon (§98: the audit log records
        the deletion; the content rows themselves are gone).
        """
        sermon = await self._session.get(SermonRow, sermon_id)
        if sermon is None:
            raise ValueError("sermon not found")
        versions = await self.list_versions(sermon_id)
        if any(v.status is SermonStatus.PUBLISHED for v in versions):
            raise PublishedImmutableError(
                "published sermons are immutable (§41) and cannot be deleted"
            )
        version_ids = [v.id for v in versions]
        if version_ids:
            segment_ids = [
                row[0]
                for row in (
                    await self._session.execute(
                        select(SermonSegmentRow.id).where(
                            SermonSegmentRow.sermon_version_id.in_(version_ids)
                        )
                    )
                ).all()
            ]
            if segment_ids:
                await self._session.execute(
                    delete(TranslationDraftRow).where(
                        TranslationDraftRow.segment_id.in_(segment_ids)
                    )
                )
                await self._session.execute(
                    delete(SermonSegmentRow).where(
                        SermonSegmentRow.sermon_version_id.in_(version_ids)
                    )
                )
            await self._session.execute(
                delete(SermonVersionRow).where(
                    SermonVersionRow.sermon_id == sermon_id
                )
            )
        await self._session.delete(sermon)
        await self._session.flush()

    async def delete_draft(self, draft_id: uuid.UUID) -> None:
        """Delete a single translation draft (§96 review-edit lifecycle).

        §41: a draft belonging to a PUBLISHED version is immutable and is
        refused (409). Otherwise the draft row is removed.
        """
        draft = await self._session.get(TranslationDraftRow, draft_id)
        if draft is None:
            raise ValueError("draft not found")
        segment = await self._session.get(SermonSegmentRow, draft.segment_id)
        if segment is not None:
            await self._guard_mutable(segment.sermon_version_id)
        await self._session.delete(draft)
        await self._session.flush()


class GlossaryRepository:
    """Glossary terms (§22, §180). Approval is explicit and versioned."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self, term_ar: str, normalized_ar: str, translations: dict[str, str]
    ) -> GlossaryTerm:
        stmt = select(GlossaryTermRow).where(
            GlossaryTermRow.normalized_ar == normalized_ar
        )
        existing = (await self._session.execute(stmt)).scalars().first()
        if existing is None:
            domain = GlossaryTerm(
                term_ar=term_ar, normalized_ar=normalized_ar,
                translations=translations,
            )
            self._session.add(
                GlossaryTermRow(
                    id=domain.id,
                    term_ar=domain.term_ar,
                    normalized_ar=domain.normalized_ar,
                    translations_json=json.dumps(
                        domain.translations, ensure_ascii=False
                    ),
                    approved=False,
                    revision=0,
                )
            )
            await self._session.flush()
            return domain
        existing.term_ar = term_ar
        existing.translations_json = json.dumps(translations, ensure_ascii=False)
        await self._session.flush()
        return _to_domain_term(existing)

    async def list_terms(self, approved_only: bool = False) -> list[GlossaryTerm]:
        stmt = select(GlossaryTermRow).order_by(GlossaryTermRow.term_ar)
        if approved_only:
            stmt = stmt.where(GlossaryTermRow.approved.is_(True))
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain_term(r) for r in rows]

    async def approve(self, term_id: uuid.UUID) -> GlossaryTerm:
        """§180: approval bumps the integer revision (g{N} provenance)."""
        row = await self._session.get(GlossaryTermRow, term_id)
        if row is None:
            raise ValueError("glossary term not found")
        row.approved = True
        row.revision += 1
        await self._session.flush()
        return _to_domain_term(row)

    async def current_revision(self) -> int:
        stmt = select(func.max(GlossaryTermRow.revision)).where(
            GlossaryTermRow.approved.is_(True)
        )
        value = await self._session.scalar(stmt)
        return value or 0


def _to_domain_scripture(row: ScriptureVerseRow):
    from bayaan_server.sermon.scripture import ScriptureReference

    return ScriptureReference(
        key=row.key,
        normalized_arabic=row.normalized_arabic,
        kind=row.kind,
        id=str(row.id),
        surah=row.surah,
        ayah=row.ayah,
        translations=json.loads(row.translations_json or "{}"),
        approved=row.approved,
        edition=row.edition,
        license=row.license,
    )


class ScriptureRepository:
    """§42.1/§42.2/§206 approved scripture references (§78 retrieval policy).

    The live router reads ONLY approved verses: Quran/Hadith are never machine
    translated, they are retrieved from this store (§206). Approving a verse is
    an explicit governance step (§180-style); edits are versioned by key.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        kind: str,
        key: str,
        arabic: str,
        normalized_arabic: str,
        translations: dict[str, str] | None = None,
        surah: str = "",
        ayah: str = "",
        edition: str = "",
        license: str = "",
        approved: bool = False,
    ):
        stmt = select(ScriptureVerseRow).where(
            ScriptureVerseRow.kind == kind, ScriptureVerseRow.key == key
        )
        existing = (await self._session.execute(stmt)).scalars().first()
        translations = translations or {}
        if existing is None:
            row = ScriptureVerseRow(
                id=uuid.uuid4(),
                kind=kind,
                key=key,
                surah=surah,
                ayah=ayah,
                arabic=arabic,
                normalized_arabic=normalized_arabic,
                translations_json=json.dumps(translations, ensure_ascii=False),
                edition=edition,
                license=license,
                approved=approved,
            )
            self._session.add(row)
            await self._session.flush()
            return _to_domain_scripture(row)
        existing.arabic = arabic
        existing.normalized_arabic = normalized_arabic
        existing.surah = surah
        existing.ayah = ayah
        existing.translations_json = json.dumps(translations, ensure_ascii=False)
        existing.edition = edition
        existing.license = license
        await self._session.flush()
        return _to_domain_scripture(existing)

    async def approve(self, verse_id: uuid.UUID):
        """§180-style governance: only approved verses are reachable live (§78)."""
        row = await self._session.get(ScriptureVerseRow, verse_id)
        if row is None:
            raise ValueError("scripture verse not found")
        row.approved = True
        await self._session.flush()
        return _to_domain_scripture(row)

    async def approve_all(self, kind: str) -> int:
        """§180 bulk governance: approve every verse of a kind. Returns the count.

        Deliberately separate from ``upsert``: an API re-upsert must never
        silently *un*-approve an already-governed verse, so approval is only
        ever an explicit act (the loader's ``--approve``, or ``approve``).
        """
        result = await self._session.execute(
            update(ScriptureVerseRow)
            .where(ScriptureVerseRow.kind == kind, ScriptureVerseRow.approved.is_(False))
            .values(approved=True)
        )
        await self._session.flush()
        return result.rowcount or 0

    async def list_approved(self, kind: str) -> list:
        stmt = select(ScriptureVerseRow).where(
            ScriptureVerseRow.kind == kind, ScriptureVerseRow.approved.is_(True)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain_scripture(r) for r in rows]

    async def list_all(
        self,
        kind: str | None = None,
        q: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list:
        stmt = select(ScriptureVerseRow)
        if kind is not None:
            stmt = stmt.where(ScriptureVerseRow.kind == kind)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                (ScriptureVerseRow.key.like(pattern))
                | (ScriptureVerseRow.arabic.like(pattern))
                | (ScriptureVerseRow.normalized_arabic.like(pattern))
                | (ScriptureVerseRow.translations_json.like(pattern))
            )
        stmt = stmt.order_by(ScriptureVerseRow.key)
        if offset > 0:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain_scripture(r) for r in rows]

    async def count(self, kind: str | None = None, q: str | None = None) -> int:
        stmt = select(func.count(ScriptureVerseRow.id))
        if kind is not None:
            stmt = stmt.where(ScriptureVerseRow.kind == kind)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                (ScriptureVerseRow.key.like(pattern))
                | (ScriptureVerseRow.arabic.like(pattern))
                | (ScriptureVerseRow.normalized_arabic.like(pattern))
                | (ScriptureVerseRow.translations_json.like(pattern))
            )
        res = await self._session.scalar(stmt)
        return res or 0


