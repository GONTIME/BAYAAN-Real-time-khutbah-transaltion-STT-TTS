"""Translation confidence + §166/§167 composite tests.

The four confidence signals are DISTINCT (§166): a validation BLOCK drops the
translation signal to 0.0, a non-clean verdict lowers it, and an absent
provider confidence stays None (never faked, §0 rule 8).
"""

from __future__ import annotations

from bayaan_server.domain.enums import AlignmentDecision, SafetyDecision
from bayaan_server.domain.validation import ValidationVerdict, ValidationWarning
from bayaan_server.domain.enums import ValidationSeverity
from bayaan_server.pipeline.confidence import translation_confidence


def _verdict(decision, risk=0.0, codes=()):
    return ValidationVerdict(
        decision=decision,
        risk=risk,
        warnings=[
            ValidationWarning(code=c, message=c, severity=ValidationSeverity.MEDIUM)
            for c in codes
        ],
    )


def test_absent_provider_confidence_stays_none() -> None:
    # §0 rule 8: never fabricate a confidence the provider did not report.
    assert translation_confidence(None) is None


def test_clean_validation_keeps_provider_confidence() -> None:
    assert translation_confidence(0.9, _verdict(SafetyDecision.ALLOW)) == 0.9


def test_validation_block_drops_to_zero() -> None:
    assert translation_confidence(0.9, _verdict(SafetyDecision.BLOCK)) == 0.0


def test_warning_lowers_confidence_by_risk() -> None:
    # risk 0.5 -> 0.9 * 0.5 = 0.45
    assert translation_confidence(
        0.9, _verdict(SafetyDecision.WARNING, risk=0.5)
    ) == 0.45


def test_alignment_fallback_lowers_confidence() -> None:
    # 0.9 * 0.9 = 0.81
    assert (
        translation_confidence(0.9, None, AlignmentDecision.FALLBACK) == 0.81
    )


def test_alignment_confirmed_leaves_confidence_untouched() -> None:
    assert (
        translation_confidence(0.9, None, AlignmentDecision.CONFIRMED) == 0.9
    )


def test_confidence_clamped_to_unit_range() -> None:
    assert translation_confidence(1.0, None, AlignmentDecision.WAIT) <= 1.0
    assert translation_confidence(0.0, None, AlignmentDecision.FALLBACK) >= 0.0
