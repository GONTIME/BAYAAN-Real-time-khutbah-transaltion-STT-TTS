"""Validator chain + number/negation/entity detectors (§66, §67, §68, §79, §101).

The §167 example is a permanent regression: a critical warning ALWAYS blocks
and is never rescued by a high average.
"""

from __future__ import annotations

from bayaan_server.config import ValidationSettings
from bayaan_server.domain.enums import SafetyDecision
from bayaan_server.pipeline import entities, negation, numbers
from bayaan_server.pipeline.validators import Validator
from bayaan_server.domain.validation import ValidationVerdict


# --- numbers (§66) ---------------------------------------------------------

def test_number_mismatch_is_detected_arabic_and_western() -> None:
    # §66: 30 (Western) vs 13 in the target is a critical mismatch.
    assert numbers.number_mismatches("عنده 30 كتابا", "ha 13 libri", "it")
    # Arabic-Indic digits fold to ASCII so ٣٠ == 30.
    assert numbers.number_mismatches("عنده ٣٠ كتابا", "ha trenta", "it") == []  # thirty word


def test_number_present_in_both_is_clean() -> None:
    assert numbers.number_mismatches("عنده 30 كتابا", "ha 30 libri", "it") == []


# --- negation (§67) --------------------------------------------------------

def test_negation_flip_is_critical() -> None:
    # Source negative -> target affirmative is a flip.
    assert negation.negation_mismatch("لا يحل هذا", "this is allowed", "en")
    # Target keeps a negation -> clean.
    assert negation.negation_mismatch("لا يحل هذا", "this is not allowed", "en") == []


def test_added_negation_is_not_a_critical_flip() -> None:
    # §67: source affirmative -> target negative is NOT reported (fail safe).
    assert negation.negation_mismatch("هذا يحل", "this is not allowed", "en") == []


# --- entities (§68) --------------------------------------------------------

def test_entity_number_omission_is_flagged() -> None:
    violations = entities.entity_preservation_violations(
        "عنده 30 كتابا", "he has some books", "en"
    )
    assert any("30" in v for v in violations)


# --- validator chain (§79/§101) -------------------------------------------

def _validator(**overrides) -> Validator:
    return Validator(ValidationSettings(**overrides))


def test_clean_translation_passes() -> None:
    verdict = _validator().validate(
        "عنده 30 كتابا", "he has 30 books", "en"
    )
    assert verdict.decision is SafetyDecision.ALLOW
    assert verdict.risk == 0.0
    assert not verdict.warnings


def test_number_mismatch_blocks_regardless_of_high_confidence() -> None:
    # §101 + §167: a changed number is a CRITICAL block, never averaged away.
    verdict = _validator().validate("عنده 30 كتابا", "he has 13 books", "en")
    assert verdict.decision is SafetyDecision.BLOCK
    assert any(w.code == "number_mismatch" and w.critical for w in verdict.warnings)


def test_negation_flip_blocks() -> None:
    verdict = _validator().validate("لا يحل هذا", "this is allowed", "en")
    assert verdict.decision is SafetyDecision.BLOCK
    assert any(w.code == "negation_flip" for w in verdict.warnings)


def test_quran_without_reference_blocks() -> None:
    # §78/§206: Quran text with no approved reference is a CRITICAL block.
    verdict = _validator().validate(
        "﴿ان مع العسر يسرا﴾", "with hardship comes ease", "en",
        content_type="QURAN",
    )
    assert verdict.decision is SafetyDecision.BLOCK
    assert any(w.code == "scripture_unverified" for w in verdict.warnings)


def test_quran_with_reference_passes_scripture_check() -> None:
    verdict = _validator().validate(
        "﴿ان مع العسر يسرا﴾", "with hardship comes ease", "en",
        content_type="QURAN",
        metadata={"approved_reference": "94:6"},
    )
    assert verdict.decision is SafetyDecision.ALLOW


def test_empty_target_blocks() -> None:
    verdict = _validator().validate("نص عربي", "   ", "en")
    assert verdict.decision is SafetyDecision.BLOCK
    assert any(w.code == "empty_target" for w in verdict.warnings)


def test_glossary_violation_is_a_warning_not_block() -> None:
    from bayaan_server.domain.sermon import GlossaryTerm

    terms = [GlossaryTerm(term_ar="صلاة", normalized_ar="صلاة",
                          translations={"en": "prayer"}, approved=True)]
    verdict = _validator().validate(
        "أقم الصلاة", "do the thing", "en", glossary_terms=terms
    )
    assert verdict.decision in (SafetyDecision.WARNING, SafetyDecision.DISPLAY_ONLY)
    assert any(w.code == "glossary_violation" for w in verdict.warnings)


def test_disabled_checks_do_not_fire() -> None:
    verdict = _validator(check_numbers=False, check_negation=False).validate(
        "لا 30", "yes 13", "en"
    )
    # Both criticals disabled -> no number/negation warnings.
    assert not any(w.code in ("number_mismatch", "negation_flip") for w in verdict.warnings)


def test_decimal_points_preserved_bug014() -> None:
    """BUG-014: '3.14' vs '314' must mismatch (decimal meaning preserved)."""
    from bayaan_server.pipeline.numbers import number_mismatches
    assert number_mismatches("value 3.14 here", "value 314 here", "en") != []
    # grouping commas still fold: 1,000 == 1000-magnitude match, no mismatch
    assert number_mismatches("count 1,000 things", "count 1000 things", "en") == []
