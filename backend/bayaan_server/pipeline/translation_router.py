"""Translation router (BAYAAN_MASTER_SPEC.md §78, §86, §1.2).

§78/§86 process_translation(live_segment, target_language):

    content = classify_content(live_segment)
    IF content == QURAN:
        reference = quran_match(live_segment)
        IF reference.verified: result = approved_translation(reference, lang)
        ELSE: return BLOCKED                       # §206: never MT the Quran
    ELSE IF content == HADITH:
        reference = hadith_match(live_segment)
        IF reference.verified: result = approved_translation(reference, lang)
        ELSE: result = general_translation(...)     # §86 non-attributed fallback
    ELSE IF prepared_match exists:
        result = approved prepared translation      # §1.2/§78
    ELSE:
        result = provider.translate(...)            # general live MT

The router returns a RoutingDecision that carries the text, whether it is
blocked, the provenance and whether an approved reference backs it - so the
caller can build metadata for the validator (§79 scripture policy) and the
TTS gate (§95). Business logic never calls a provider directly for sacred
text: only approved references (§22.3, §206).

This module is pure: the caller injects the scripture matcher, the prepared
approved-translation lookup and the MT provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from bayaan_server.config import ScriptureSettings
from bayaan_server.domain.enums import ContentType
from bayaan_server.sermon.scripture import ScriptureMatcher, ScriptureMatch


class GeneralTranslator(Protocol):
    """The live MT provider port (§63), used only for non-sacred text."""

    async def translate(self, text: str, source: str, target: str) -> object: ...


@dataclass
class RoutingDecision:
    """§78/§86 outcome for one segment in one target language."""

    text: str
    blocked: bool = False
    content_type: ContentType = ContentType.GENERAL
    # §42.3 retrieval tier actually used, for metrics/provenance.
    route: str = "general_mt"
    approved_reference: str | None = None  # scripture key when approved-backed
    verification_level: str = ""
    prepared_segment_id: str | None = None
    metadata: dict = field(default_factory=dict)


class TranslationRouter:
    """§78/§86 router. Sacred text is NEVER machine-translated: QURAN without an
    approved reference is BLOCKED; HADITH falls back to a general translation
    when unverified (§86). Thresholds from ``ScriptureSettings`` (§185)."""

    def __init__(
        self,
        scripture: ScriptureMatcher,
        settings: ScriptureSettings,
    ) -> None:
        self._scripture = scripture
        self._settings = settings

    def _scripture_decision(
        self, match: ScriptureMatch, language: str
    ) -> RoutingDecision | None:
        """Map a §76 match onto an approved-translation decision, or None when
        no approved reference is available for this language."""
        if not match.verified or match.reference is None:
            return None
        approved = match.approved_translation(language)
        if approved is None:
            return None
        return RoutingDecision(
            text=approved,
            blocked=False,
            route=f"scripture_{match.level.value.lower()}",
            approved_reference=match.reference.key,
            verification_level=match.level.value,
        )

    async def route_quran(self, normalized_text: str, language: str) -> RoutingDecision:
        """§78/§206: Quran uses an approved reference only; else BLOCKED."""
        match = self._scripture.detect(normalized_text)
        decision = self._scripture_decision(match, language)
        if decision is not None:
            decision.content_type = ContentType.QURAN
            return decision
        if self._settings.quran_block_without_reference:
            return RoutingDecision(
                text="", blocked=True, content_type=ContentType.QURAN,
                route="quran_blocked", verification_level=match.level.value,
            )
        # Policy override (not default): allow a clearly-labelled fallback.
        return RoutingDecision(
            text="", blocked=True, content_type=ContentType.QURAN,
            route="quran_blocked", verification_level=match.level.value,
        )

    async def route_hadith(
        self, normalized_text: str, language: str, translator: GeneralTranslator
    ) -> RoutingDecision:
        """§86: approved reference wins; else a general (non-attributed) MT."""
        match = self._scripture.detect(normalized_text)
        decision = self._scripture_decision(match, language)
        if decision is not None:
            decision.content_type = ContentType.HADITH
            return decision
        if not self._settings.hadith_fallback_general:
            return RoutingDecision(
                text="", blocked=True, content_type=ContentType.HADITH,
                route="hadith_blocked", verification_level=match.level.value,
            )
        # §86 general_translation WITHOUT attribution (never claim a hadith
        # reference we could not verify).
        result = await translator.translate(normalized_text, "ar", language)
        return RoutingDecision(
            text=result.text,
            blocked=False,
            content_type=ContentType.HADITH,
            route="hadith_general_mt",
            verification_level=match.level.value,
        )
