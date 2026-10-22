"""Sermon preparation service (BAYAAN_MASTER_SPEC.md §75, §119, §155).

Flow: upload -> parse -> segment -> classify -> version (content hash)
-> draft translations per target language. Status: DRAFT -> REVIEW ->
APPROVED -> PUBLISHED (§119); publish snapshots the content hash (§41
immutable published translations).
"""

from __future__ import annotations

import dataclasses
import hashlib

from bayaan_server.config import Settings
from bayaan_server.db.repository import (
    GlossaryRepository,
    PublishedImmutableError,
    SermonRepository,
)
from bayaan_server.domain.enums import ContentType, SermonStatus
from bayaan_server.domain.sermon import SermonSegment, SermonVersion, TranslationDraft
from bayaan_server.pipeline.classifier import RuleContentDetector
from bayaan_server.pipeline.normalizer import ArabicNormalizerV1
from bayaan_server.providers.interfaces import TranslationProvider
from bayaan_server.sermon.glossary import glossary_version
from bayaan_server.sermon.parsers import parse_document
from bayaan_server.sermon.segmenter import segment_text


def content_hash(normalized_segments: list[str]) -> str:
    """§155: sha256 over the canonical normalized segment form."""
    digest = hashlib.sha256()
    for segment in normalized_segments:
        digest.update(b"\x00")
        digest.update(segment.encode("utf-8"))
    return digest.hexdigest()


@dataclasses.dataclass(frozen=True)
class DraftGenerationResult:
    """Outcome of one ``generate_drafts`` pass (§75).

    ``created`` counts new drafts written this pass; ``skipped`` counts
    segment/language pairs that already had a draft and were left untouched
    (§169/§41: a human correction or approval is never silently overwritten).
    ``total`` is the full matrix size for the version.
    """

    created: int
    skipped: int

    @property
    def total(self) -> int:
        return self.created + self.skipped


class PreparationService:
    def __init__(
        self,
        settings: Settings,
        sermons: SermonRepository,
        glossary: GlossaryRepository,
        translation: TranslationProvider,
    ) -> None:
        self._settings = settings
        self._sermons = sermons
        self._glossary = glossary
        self._translation = translation
        self._normalizer = ArabicNormalizerV1()
        self._detector = RuleContentDetector()

    async def ingest(
        self, title: str, filename: str, data: bytes,
        imam_ref: str | None = None,
    ) -> SermonVersion:
        """Upload a document and prepare version 1 (DRAFT) with segments."""
        limits = self._settings.sermon
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in limits.allowed_extensions:
            from bayaan_server.sermon.parsers import UnsupportedDocumentError

            raise UnsupportedDocumentError(
                f"{filename}: unsupported extension (allowed: "
                f"{limits.allowed_extensions})"
            )
        if len(data) > limits.max_upload_bytes:
            from bayaan_server.sermon.parsers import UnsupportedDocumentError

            raise UnsupportedDocumentError(
                f"{filename}: {len(data)} bytes exceeds "
                f"{limits.max_upload_bytes} byte limit"
            )
        text = parse_document(data, filename)
        texts = segment_text(text, limits)
        sermon = await self._sermons.create_sermon(
            title, source_file_name=filename, imam_ref=imam_ref
        )
        normalized = [self._normalizer.normalize(t) for t in texts]
        version = await self._sermons.create_version(
            sermon.id, content_hash(normalized), status=SermonStatus.DRAFT,
        )
        segments = [
            SermonSegment(
                sermon_version_id=version.id,
                segment_order=i,
                arabic=raw,
                normalized_arabic=norm,
                content_type=ContentType.GENERAL,  # refined below via detector
                source_ref=f"{filename}#para{i}",
            )
            for i, (raw, norm) in enumerate(zip(texts, normalized))
        ]
        for seg, raw, norm in zip(segments, texts, normalized):
            seg.content_type = self._detector.detect(raw)
            seg.normalized_arabic = norm
        await self._sermons.add_segments(version.id, segments)
        return version


    async def generate_drafts(
        self, version_id: object
    ) -> DraftGenerationResult:
        """Translate every segment to each target language (§75 drafts).

        Idempotent: a segment/language pair that already has a draft is left
        untouched (§169/§41 - a human correction or approval is never silently
        overwritten by a re-run), so re-clicking "Generate drafts" only fills
        the gaps and reports how many were created vs already present.

        Provenance (§69) travels on every draft: provider, model,
        prompt version, glossary g{N}, retrieval version.
        """
        import uuid as _uuid

        vid = (
            version_id
            if isinstance(version_id, _uuid.UUID)
            else _uuid.UUID(str(version_id))
        )
        version = await self._sermons.get_version(vid)
        if version is None:
            raise ValueError("sermon version not found")
        if version.status is SermonStatus.PUBLISHED:
            # §41: a published version is frozen - never regenerate its drafts,
            # even as a no-op. Surfaced as 409 by the route.
            raise PublishedImmutableError("published versions are immutable")
        revision = await self._glossary.current_revision()
        segments = await self._sermons.list_segments(vid)
        languages = self._settings.languages.target_languages
        created = 0
        skipped = 0
        for segment in segments:
            existing = {
                d.language for d in await self._sermons.list_drafts(segment.id)
            }
            for language in languages:
                if language in existing:
                    # §169/§41: never overwrite an existing draft on re-run.
                    skipped += 1
                    continue
                if segment.content_type == ContentType.UNKNOWN:
                    # Untranslated passthrough: provenance still explicit,
                    # prompt_version records that no MT was applied (§69).
                    draft = TranslationDraft(
                        segment_id=segment.id,
                        language=language,
                        text=segment.arabic,
                        provider=self._settings.providers.translation,
                        model="",
                        prompt_version="not-translated",
                        glossary_version=glossary_version(revision),
                        retrieval_version="none",
                    )
                else:
                    result = await self._translation.translate(
                        segment.arabic, "ar", language,
                    )
                    # §69: use the provider's own provenance, never a
                    # fabricated value. Glossary revision is pinned to the
                    # glossary that governed this preparation pass.
                    draft = TranslationDraft(
                        segment_id=segment.id,
                        language=language,
                        text=result.text,
                        provider=result.provider,
                        model=result.model,
                        prompt_version=result.prompt_version,
                        glossary_version=glossary_version(revision),
                        retrieval_version=result.retrieval_version,
                    )
                await self._sermons.save_draft(draft)
                created += 1
        return DraftGenerationResult(created=created, skipped=skipped)
