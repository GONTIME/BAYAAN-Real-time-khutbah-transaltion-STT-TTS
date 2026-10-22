"""Prepared-sermon retrieval for live use (BAYAAN_MASTER_SPEC.md §78).

Live Pipeline Router decision: IF exact match in a PUBLISHED sermon
version THEN use the approved translation and skip MT, ELSE translate
live. This MVP implements exact normalized matching only; semantic
retrieval lands in Phase 4 with the embedding index (§77 semantic
retrieval, §193 alignment).
"""

from __future__ import annotations

import uuid

from bayaan_server.db.repository import SermonRepository
from bayaan_server.domain.sermon import SermonSegment, TranslationDraft
from bayaan_server.pipeline.normalizer import ArabicNormalizerV1

_RETRIEVAL_VERSION = "exact-v1"


def retrieval_version() -> str:
    """Provenance stamp: exact matching only in this phase (§69)."""
    return _RETRIEVAL_VERSION


class PreparedRetrieval:
    def __init__(self, sermons: SermonRepository) -> None:
        self._sermons = sermons
        self._normalizer = ArabicNormalizerV1()

    async def match(
        self, live_arabic: str, language: str, published_version_id: uuid.UUID,
    ) -> tuple[SermonSegment, TranslationDraft] | None:
        """Exact normalized match + approved draft for one language.

        Convenience wrapper over find_segment; None means fall through
        to live MT (§78 ELSE branch).
        """
        segment = await self.find_segment(live_arabic, published_version_id)
        if segment is None:
            return None
        draft = await self._sermons.get_approved_draft(segment.id, language)
        if draft is None:
            return None
        return segment, draft

    async def find_segment(
        self, live_arabic: str, published_version_id: uuid.UUID,
    ) -> SermonSegment | None:
        """Exact normalized match against a PUBLISHED version.

        Non-published versions never match (§1.2 router: only published
        prepared sermons are reachable live).
        """
        version = await self._sermons.get_version(published_version_id)
        if version is None:
            return None
        from bayaan_server.domain.enums import SermonStatus

        if version.status != SermonStatus.PUBLISHED:
            return None
        normalized = self._normalizer.normalize(live_arabic)
        for segment in await self._sermons.list_segments(published_version_id):
            if segment.normalized_arabic == normalized:
                return segment
        return None
