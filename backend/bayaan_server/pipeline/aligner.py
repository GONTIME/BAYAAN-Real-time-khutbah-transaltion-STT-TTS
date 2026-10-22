"""Sermon aligner (BAYAAN_MASTER_SPEC.md §77, §193).

§77 align(asr_text, current_index, segments): score the live ASR text against
a local window of prepared segments and return CONFIRMED / WAIT / FALLBACK.

Composite score mixes four signals (§77):
    lexical     token overlap (Jaccard over normalized Arabic words)
    fuzzy       character-level similarity (difflib ratio, stdlib only)
    temporal    position consistency (closer to the running index scores higher)
    continuity  adjacency to the previously confirmed segment

Semantic (embedding) similarity is NOT computed here: no embedding model is
wired yet, and §166/§0 rule 8 forbid faking an absent signal with zero. The
aligner renormalizes over the three present signals and records
``semantic_available=False`` so the caller keeps alignment honest (§166:
absent != zero).

All thresholds live in ``AlignmentSettings`` (§185). Pure logic - no DB, no
network - so it is unit-testable with plain prepared-segment stand-ins.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from bayaan_server.config import AlignmentSettings
from bayaan_server.domain.enums import AlignmentDecision


@dataclass
class AlignmentCandidate:
    """One prepared segment the aligner can score against (§77 local window)."""

    id: str
    normalized_arabic: str
    order: int = 0


@dataclass
class AlignmentResult:
    """§77 outcome for one ASR utterance."""

    decision: AlignmentDecision
    score: float = 0.0
    segment_id: str | None = None
    lexical: float = 0.0
    fuzzy: float = 0.0
    temporal: float = 0.0
    continuity: float = 0.0
    semantic_available: bool = False
    candidates_scored: int = 0


def _tokens(text: str) -> set[str]:
    return set(text.split())


def lexical_overlap(asr_text: str, segment_text: str) -> float:
    """§77 token_overlap: Jaccard similarity of normalized Arabic word sets."""
    a, b = _tokens(asr_text), _tokens(segment_text)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def fuzzy_similarity(asr_text: str, segment_text: str) -> float:
    """§77 fuzzy_similarity: character-level ratio (stdlib difflib).

    Arabic morphology makes exact token overlap brittle (a clitic prefix on one
    side breaks the Jaccard), so a character ratio is a cheap, dependency-free
    complement. A real fuzzy library (rapidfuzz) is a later optimization.
    """
    if not asr_text or not segment_text:
        return 0.0
    return SequenceMatcher(None, asr_text, segment_text).ratio()


def position_consistency(segment_order: int, current_index: int, window: int) -> float:
    """§77 temporal: 1.0 at the running index, decaying linearly to 0 at the
    window edge. The imam reads roughly in order (§77 continuity)."""
    distance = abs(segment_order - current_index)
    if window <= 0:
        return 1.0 if distance == 0 else 0.0
    return max(0.0, 1.0 - distance / window)


def previous_segment_consistency(segment_order: int, previous_order: int | None) -> float:
    """§77 continuity: 1.0 if this segment immediately follows the last
    confirmed one, decaying with distance. 0.5 neutral when there is no
    previous confirmed segment yet (start of session)."""
    if previous_order is None:
        return 0.5
    distance = abs(segment_order - previous_order)
    if distance <= 1:
        return 1.0
    return max(0.0, 1.0 - (distance - 1) / 3.0)


class SermonAligner:
    """§77 sermon_aligner. Thresholds from ``AlignmentSettings`` (§185)."""

    def __init__(self, settings: AlignmentSettings) -> None:
        self._settings = settings

    def _local_window(
        self, segments: list[AlignmentCandidate], current_index: int
    ) -> list[AlignmentCandidate]:
        window = self._settings.window_size
        if not segments:
            return []
        in_window = [s for s in segments if abs(s.order - current_index) <= window]
        # If nothing is inside the window (big jump), still score the nearest
        # candidate so the caller gets a low FALLBACK, never a crash (§107).
        if not in_window:
            in_window = sorted(
                segments, key=lambda s: abs(s.order - current_index)
            )[:1]
        return in_window

    def align(
        self,
        asr_text: str,
        current_index: int,
        segments: list[AlignmentCandidate],
        previous_order: int | None = None,
    ) -> AlignmentResult:
        s = self._settings
        candidates = self._local_window(segments, current_index)
        if not candidates or not asr_text.strip():
            return AlignmentResult(
                decision=AlignmentDecision.FALLBACK, semantic_available=False
            )

        best: AlignmentResult | None = None
        total = (
            s.lexical_weight + s.fuzzy_weight + s.temporal_weight + s.continuity_weight
        )
        for segment in candidates:
            lex = lexical_overlap(asr_text, segment.normalized_arabic)
            fuz = fuzzy_similarity(asr_text, segment.normalized_arabic)
            temp = position_consistency(segment.order, current_index, s.window_size)
            cont = previous_segment_consistency(segment.order, previous_order)

            score = (
                s.lexical_weight * lex
                + s.fuzzy_weight * fuz
                + s.temporal_weight * temp
                + s.continuity_weight * cont
            ) / total

            result = AlignmentResult(
                decision=AlignmentDecision.FALLBACK,
                score=score,
                segment_id=segment.id,
                lexical=lex,
                fuzzy=fuz,
                temporal=temp,
                continuity=cont,
                semantic_available=False,
                candidates_scored=len(candidates),
            )
            if best is None or score > best.score:
                best = result

        assert best is not None
        if best.score >= s.confirm_threshold:
            best.decision = AlignmentDecision.CONFIRMED
        elif best.score >= s.candidate_threshold:
            best.decision = AlignmentDecision.WAIT
        else:
            best.decision = AlignmentDecision.FALLBACK
        return best
