"""Safety engine tests (§166, §167, §95, §201).

The §167 example is a permanent regression test:
ASR 0.99 + translation 0.98 + breached safety signal => BLOCK.
"""

from __future__ import annotations

from bayaan_server.config import SafetySettings
from bayaan_server.domain.enums import SafetyDecision
from bayaan_server.domain.models import ConfidenceBreakdown
from bayaan_server.domain.safety import (
    PUBLIC_FALLBACK_MESSAGE,
    can_speak,
    evaluate_safety,
)


def breakdown(asr=0.99, alignment=0.99, translation=0.98, validation=0.99):
    return ConfidenceBreakdown(
        asr_confidence=asr,
        alignment_confidence=alignment,
        translation_confidence=translation,
        validation_score=validation,
    )


def test_spec_167_example_blocks_despite_high_overall() -> None:
    # High scores everywhere, but per-signal thresholds are hard blockers.
    safety = SafetySettings()
    verdict = evaluate_safety(
        breakdown(translation=0.98), safety
    )  # no breached signal in this setup
    assert verdict.decision is SafetyDecision.ALLOW

    # Now breach one signal: overall average must NOT rescue it.
    low_translation = breakdown(translation=0.10)
    verdict = evaluate_safety(low_translation, safety)
    assert verdict.decision is SafetyDecision.BLOCK
    assert any("translation_confidence" in r for r in verdict.reasons)


def test_low_overall_yields_fallback_not_guess() -> None:
    # No single signal breaches its minimum, but the composite is still weak:
    # the result is FALLBACK ("Translation unavailable"), never a guess (§92).
    safety = SafetySettings(
        asr_min_confidence=0.10,
        alignment_min_confidence=0.10,
        translation_min_confidence=0.10,
        overall_min_confidence=0.99,
    )
    verdict = evaluate_safety(breakdown(), safety)
    assert verdict.decision is SafetyDecision.FALLBACK


def test_tts_gate_stricter_than_text_gate() -> None:
    safety = SafetySettings(tts_min_confidence=0.95)
    bd = breakdown(translation=0.92)  # passes text path, fails TTS path
    assert evaluate_safety(bd, safety).decision is SafetyDecision.ALLOW
    assert can_speak(bd, safety) is False


def test_public_message_never_leaks_internal_scores() -> None:
    # §134: the public client sees "Translation unavailable", not risk=0.72.
    assert "risk" not in PUBLIC_FALLBACK_MESSAGE.lower()


def test_weights_must_sum_to_one() -> None:
    from bayaan_server.config import ConfidenceWeights

    import pydantic

    try:
        ConfidenceWeights(asr=0.5, alignment=0.5, translation=0.5, validation=0.5)
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("invalid weights must be rejected")


def test_absent_signals_are_excluded_not_faked() -> None:
    """§0 rule 8 + §166: a not-yet-evaluated signal (None) must never count
    as zero quality. With alignment/validation disabled (Phase 2 default),
    composite renormalizes over asr+translation only."""
    safety = SafetySettings()  # alignment_enabled=False, validation_enabled=False
    bd = ConfidenceBreakdown(
        asr_confidence=0.9,
        translation_confidence=0.95,
        # alignment / validation absent
    )
    verdict = evaluate_safety(bd, safety)
    assert verdict.decision is SafetyDecision.ALLOW
    # Renormalized: (0.25*0.9 + 0.35*0.95) / 0.6 = 0.9292 >= 0.75
    assert verdict.overall_confidence > 0.9


def test_disabled_alignment_signal_is_not_enforced() -> None:
    safety = SafetySettings(alignment_enabled=False)
    bd = ConfidenceBreakdown(asr_confidence=0.99, translation_confidence=0.99)
    # alignment engine absent -> its threshold must not fire
    assert evaluate_safety(bd, safety).decision is SafetyDecision.ALLOW


def test_enabled_alignment_signal_is_enforced() -> None:
    safety = SafetySettings(alignment_enabled=True)
    bd = ConfidenceBreakdown(
        asr_confidence=0.99,
        alignment_confidence=0.1,  # below 0.70 -> hard BLOCK (§167)
        translation_confidence=0.99,
    )
    verdict = evaluate_safety(bd, safety)
    assert verdict.decision is SafetyDecision.BLOCK
    assert any("alignment" in r for r in verdict.reasons)


def test_all_signals_absence_yields_zero_not_allow() -> None:
    safety = SafetySettings()
    empty = ConfidenceBreakdown()
    verdict = evaluate_safety(empty, safety)
    assert verdict.overall_confidence == 0.0
    assert verdict.decision is SafetyDecision.FALLBACK

def test_warning_level_validation_broadcasts() -> None:
    """BUG-003: a WARNING-level validation (risk 0.3, validation_score 0.7)
    must broadcast (ALLOW), matching the golden benchmark — not BLOCK."""
    safety = SafetySettings()
    bd = ConfidenceBreakdown(
        asr_confidence=0.99,
        alignment_confidence=0.99,
        translation_confidence=0.9,
        validation_score=0.7,
    )
    verdict = evaluate_safety(bd, safety)
    assert verdict.decision is SafetyDecision.ALLOW


def test_critical_validation_still_blocks() -> None:
    """BUG-003 guard: genuinely low validation (0.2) still BLOCKs."""
    safety = SafetySettings()
    bd = ConfidenceBreakdown(
        asr_confidence=0.99,
        alignment_confidence=0.99,
        translation_confidence=0.9,
        validation_score=0.2,
    )
    verdict = evaluate_safety(bd, safety)
    assert verdict.decision is SafetyDecision.BLOCK
