"""Translation confidence (BAYAAN_MASTER_SPEC.md §166, §167).

§166: ASR confidence, alignment confidence, translation confidence and safety
score are DISTINCT signals. §167 composites them, but safety blockers are
never averaged away.

The provider's own translation confidence is the base signal; the validator's
outcome and the alignment decision refine it deterministically:
  - a validation BLOCK drops translation confidence to 0.0 (the output will be
    blocked regardless, but keeping the signal honest avoids a misleading score);
  - a DISPLAY_ONLY/WARNING verdict and a non-CONFIRMED alignment lower the
    effective translation confidence, because we are less sure the text is a
    faithful rendering of what was spoken.

This is intentionally deterministic and side-effect free so the composite in
``domain.safety`` stays reproducible (§69). The provider confidence is never
invented: when the provider does not report one, we do not fake a number and
return None (absent != zero, §0 rule 8).
"""

from __future__ import annotations

from bayaan_server.domain.enums import AlignmentDecision, SafetyDecision
from bayaan_server.domain.validation import ValidationVerdict


def translation_confidence(
    provider_confidence: float | None,
    validation: ValidationVerdict | None = None,
    alignment_decision: AlignmentDecision | None = None,
) -> float | None:
    """Derive the translation-confidence signal for the §167 composite.

    Returns None only when the provider reported no confidence at all (§0 rule
    8: never fabricate). Otherwise starts from the provider score and applies
    deterministic penalties for a non-clean validation/alignment verdict.
    """
    if provider_confidence is None:
        return None
    score = provider_confidence

    if validation is not None:
        if validation.decision is SafetyDecision.BLOCK:
            return 0.0
        if validation.decision in (SafetyDecision.DISPLAY_ONLY, SafetyDecision.WARNING):
            # A reviewable issue reduces how sure we are of the translation.
            score *= 1.0 - validation.risk

    if alignment_decision is not None:
        if alignment_decision is AlignmentDecision.FALLBACK:
            # We could not tie the text to a prepared segment; less sure it is
            # a faithful rendering of what the imam said.
            score *= 0.9
        elif alignment_decision is AlignmentDecision.WAIT:
            score *= 0.95

    return max(0.0, min(1.0, score))
