"""Scripture verification (BAYAAN_MASTER_SPEC.md §76, §42.3, §78, §86, §206).

§76 detect_scripture(text): exact -> VERIFIED, strict fuzzy -> VERIFIED_NEAR,
weaker candidate -> MANUAL_REVIEW_REQUIRED, none -> UNKNOWN.

§78/§86 routing policy:
  - QURAN: only an approved reference may be shown. If the live text is Quran
    but no verified reference exists -> BLOCKED. General MT of Quran is never
    permitted (§206: Quran has its own repository/policy).
  - HADITH: an approved reference wins; otherwise fall back to a general
    (non-attributed) translation (§86 general_translation).

§0 rule 8: we never claim a citation we cannot verify. Only VERIFIED and
VERIFIED_NEAR carry an approved reference; REVIEW/UNKNOWN never do.

The verse store is small and provided by the caller (empty by default: no
curated dataset is bundled, so by default every Quran hit is UNKNOWN and
blocks, which is the safe behaviour). Matching is over normalized Arabic and
uses stdlib difflib for the fuzzy tiers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from bayaan_server.config import ScriptureSettings
from bayaan_server.domain.enums import VerificationLevel

# §65 marks a Quran utterance with ornate parentheses (﴾ ﴿). That is a
# transcription convention, not part of the verse: the store keeps the source
# text verbatim (§42.2), so the decoration is stripped before matching.
# Without this, a bracket-marked live line could never exact-match a seeded
# verse and every real Quran recitation would fall through to BLOCKED.
_QURAN_DECORATION = re.compile("[\ufd3e\ufd3f]")


def strip_quran_decoration(text: str) -> str:
    """Remove §65 ornate-parenthesis decoration from already-normalized text.

    Applied symmetrically - when a verse is indexed (§42.2 store) and when a
    live line is matched (§76) - so a decorated transcript still verifies
    against a verbatim stored verse.
    """
    return _QURAN_DECORATION.sub("", text).strip()


@dataclass(frozen=True)
class ScriptureReference:
    """An approved scripture entry (§42.2): the source text plus its approved,
    per-language approved translations and an auditable edition/license."""

    key: str                      # stable id, e.g. "2:255" (surah:ayah)
    normalized_arabic: str = ""
    kind: str = "QURAN"           # QURAN | HADITH (§42)
    id: str = ""                  # row id (set by the repository); "" if standalone
    surah: str = ""
    ayah: str = ""
    # approved translations per language code -> approved text
    translations: dict[str, str] | None = None
    approved: bool = True
    edition: str = ""
    license: str = ""


@dataclass
class ScriptureMatch:
    """§76 outcome: the verification level plus the matched reference (only
    present for VERIFIED / VERIFIED_NEAR)."""

    level: VerificationLevel
    reference: ScriptureReference | None = None
    score: float = 0.0

    @property
    def verified(self) -> bool:
        """§78: only these tiers may surface an approved reference."""
        return self.level in (
            VerificationLevel.VERIFIED,
            VerificationLevel.VERIFIED_NEAR,
        )

    def approved_translation(self, language: str) -> str | None:
        if not self.verified or self.reference is None:
            return None
        return (self.reference.translations or {}).get(language)


def _best_fuzzy(text: str, references: list[ScriptureReference]) -> tuple[ScriptureReference, float]:
    best_ref = references[0]
    best_score = 0.0
    for ref in references:
        score = SequenceMatcher(None, text, ref.normalized_arabic).ratio()
        if score > best_score:
            best_score, best_ref = score, ref
    return best_ref, best_score


class ScriptureMatcher:
    """§76 scripture_matcher. Thresholds from ``ScriptureSettings`` (§185)."""

    def __init__(
        self, settings: ScriptureSettings, references: list[ScriptureReference] | None = None
    ) -> None:
        self._settings = settings
        self._references = references or []

    def detect(self, normalized_text: str) -> ScriptureMatch:
        """§76: exact -> VERIFIED; strict fuzzy -> VERIFIED_NEAR; weaker ->
        MANUAL_REVIEW_REQUIRED; none -> UNKNOWN. Approved references only."""
        s = self._settings
        approved = [r for r in self._references if r.approved]
        if not approved:
            return ScriptureMatch(level=VerificationLevel.UNKNOWN)

        # Compare the verse itself, not the §65 transcription decoration.
        normalized_text = strip_quran_decoration(normalized_text)

        # Exact normalized match -> VERIFIED.
        for ref in approved:
            if ref.normalized_arabic and ref.normalized_arabic == normalized_text:
                return ScriptureMatch(level=VerificationLevel.VERIFIED, reference=ref, score=1.0)

        ref, score = _best_fuzzy(normalized_text, approved)
        if score >= s.near_match_threshold:
            return ScriptureMatch(
                level=VerificationLevel.VERIFIED_NEAR, reference=ref, score=score
            )
        if score >= s.review_threshold:
            # A weak candidate is NOT verified: no reference may be claimed.
            return ScriptureMatch(level=VerificationLevel.MANUAL_REVIEW_REQUIRED, score=score)
        return ScriptureMatch(level=VerificationLevel.UNKNOWN, score=score)
