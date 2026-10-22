"""Sermon aligner tests (BAYAAN_MASTER_SPEC.md §77, §193).

Exact overlap of the live text with a prepared segment at the running index
should CONFIRM; unrelated text should FALLBACK; and semantic similarity is
absent (never faked) so it is excluded from the score.
"""

from __future__ import annotations

from bayaan_server.config import AlignmentSettings
from bayaan_server.domain.enums import AlignmentDecision
from bayaan_server.pipeline.aligner import (
    AlignmentCandidate,
    SermonAligner,
    fuzzy_similarity,
    lexical_overlap,
)


def _segments() -> list[AlignmentCandidate]:
    return [
        AlignmentCandidate(id=f"s{i}", normalized_arabic=t, order=i)
        for i, t in enumerate(
            [
                "الحمد لله رب العالمين",
                "اما بعد فان تقوى الله نور",
                "والصلاة والسلام على رسول الله",
                "اخوتي في الله اوصيكم بتقوى الله",
                "والله الموفق لكل خير",
            ]
        )
    ]


def _aligner() -> SermonAligner:
    return SermonAligner(AlignmentSettings())


def test_lexical_overlap_jaccard() -> None:
    assert lexical_overlap("الحمد لله رب", "الحمد لله رب العالمين") > 0.5
    assert lexical_overlap("", "شيء") == 0.0
    assert lexical_overlap("كلمة", "أخرى") == 0.0


def test_exact_text_at_index_confirms() -> None:
    result = _aligner().align(
        "الحمد لله رب العالمين", 0, _segments()
    )
    assert result.decision is AlignmentDecision.CONFIRMED
    assert result.segment_id == "s0"


def test_unrelated_text_falls_back() -> None:
    result = _aligner().align("زززز يييي ككك", 0, _segments())
    assert result.decision is AlignmentDecision.FALLBACK


def test_no_candidates_fall_back_not_crash() -> None:
    # §107: an empty window is a clean FALLBACK, never an exception.
    result = _aligner().align("نص", 3, [])
    assert result.decision is AlignmentDecision.FALLBACK


def test_semantic_signal_is_absent_not_faked() -> None:
    # §166/§0 rule 8: semantic embedding similarity is not available yet.
    result = _aligner().align("الحمد لله رب العالمين", 0, _segments())
    assert result.semantic_available is False


def test_window_bounds_candidates() -> None:
    # A window of 1 at current_index 2 only scores orders 1,2,3.
    aligner = SermonAligner(AlignmentSettings(window_size=1))
    result = aligner.align("الحمد لله رب العالمين", 2, _segments())
    assert result.candidates_scored == 3


def test_fuzzy_similarity_is_character_based() -> None:
    assert fuzzy_similarity("abc", "abc") == 1.0
    assert 0.0 <= fuzzy_similarity("الحمد لله", "الحمد لله رب") < 1.0
    assert fuzzy_similarity("", "x") == 0.0


def test_thresholds_must_be_ordered() -> None:
    import pydantic

    try:
        AlignmentSettings(candidate_threshold=0.9, confirm_threshold=0.8)
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("candidate must be below confirm threshold")
