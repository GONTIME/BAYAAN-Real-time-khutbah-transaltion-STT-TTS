"""Composite confidence + safety gating (BAYAAN_MASTER_SPEC.md §166-§168).

Hard rules:
- Safety blockers are NEVER overridden by an overall average (§167).
- Signals whose engines have not run yet are excluded (config flags), never
  faked with zero scores.
- Block reasons are explainable to the operator without leaking prompts (§168).
- The public client never sees internal diagnostics (§134).
"""

from __future__ import annotations

from typing import Optional

from bayaan_server.config import SafetySettings
from .models import ConfidenceBreakdown, SafetyVerdict
from .enums import SafetyDecision

# Public-facing message (§134 Screen Safety) - never internal scores.
PUBLIC_FALLBACK_MESSAGE = "Translation unavailable"


def _active_weights(breakdown: ConfidenceBreakdown, safety: SafetySettings) -> dict[str, float]:
    """Weights of signals that are both enabled and present, renormalized
    to sum to 1.0 over the active set (§167 weighted function)."""
    w = safety.weights
    candidates = {
        "asr": (breakdown.asr_confidence, w.asr),
        "alignment": (breakdown.alignment_confidence, w.alignment),
        "translation": (breakdown.translation_confidence, w.translation),
        "validation": (breakdown.validation_score, w.validation),
    }
    enabled = {
        "asr": True,
        "alignment": safety.alignment_enabled,
        "translation": True,
        "validation": safety.validation_enabled,
    }
    active = {
        name: weight
        for name, (value, weight) in candidates.items()
        if value is not None and enabled[name]
    }
    total = sum(active.values())
    if total <= 0:
        return {}
    return {name: weight / total for name, weight in active.items()}


def compute_overall_confidence(
    breakdown: ConfidenceBreakdown, safety: SafetySettings
) -> float:
    """§167 weighted composite over active signals only."""
    weights = _active_weights(breakdown, safety)
    if not weights:
        return 0.0
    values = {
        "asr": breakdown.asr_confidence,
        "alignment": breakdown.alignment_confidence,
        "translation": breakdown.translation_confidence,
        "validation": breakdown.validation_score,
    }
    return sum(weights[name] * values[name] for name in weights)  # type: ignore[arg-type]


def evaluate_safety(
    breakdown: ConfidenceBreakdown, safety: SafetySettings
) -> SafetyVerdict:
    """Decide ALLOW / FALLBACK / BLOCK for one candidate output.

    Per-signal thresholds are hard blockers enforced only for signals whose
    engines are enabled; the weighted average can never rescue a breached
    signal (§167 example: ASR 0.99 + translation 0.98 + negation mismatch
    => BLOCK).
    """
    reasons: list[str] = []

    if breakdown.asr_confidence is not None and (
        breakdown.asr_confidence < safety.asr_min_confidence
    ):
        reasons.append(f"asr_confidence below {safety.asr_min_confidence}")
    if safety.alignment_enabled and breakdown.alignment_confidence is not None and (
        breakdown.alignment_confidence < safety.alignment_min_confidence
    ):
        reasons.append(
            f"alignment_confidence below {safety.alignment_min_confidence}"
        )
    if breakdown.translation_confidence is not None and (
        breakdown.translation_confidence < safety.translation_min_confidence
    ):
        reasons.append(
            f"translation_confidence below {safety.translation_min_confidence}"
        )
    # BUG-003: the validation *signal* floor must not reuse the composite
    # confidence floor. Validation warnings already BLOCK via the validator
    # verdict path (critical -> BLOCK, high-risk -> DISPLAY_ONLY handled by
    # callers); here only an explicitly low validation *score* blocks, using
    # the caller's configured floor. A WARNING-level outcome (risk 0.3,
    # validation_score 0.7) must broadcast per the golden benchmark, so the
    # default validation floor is 0.5, not overall_min_confidence (0.75).
    _validation_floor = min(0.5, safety.overall_min_confidence)
    if safety.validation_enabled and breakdown.validation_score is not None and (
        breakdown.validation_score < _validation_floor
    ):
        reasons.append(f"validation_score below {_validation_floor}")

    overall = compute_overall_confidence(breakdown, safety)

    if reasons:
        return SafetyVerdict(
            decision=SafetyDecision.BLOCK,
            overall_confidence=overall,
            reasons=reasons,
        )

    if overall < safety.overall_min_confidence:
        return SafetyVerdict(
            decision=SafetyDecision.FALLBACK,
            overall_confidence=overall,
            reasons=[f"overall_confidence below {safety.overall_min_confidence}"],
        )

    return SafetyVerdict(decision=SafetyDecision.ALLOW, overall_confidence=overall)


def can_speak(breakdown: ConfidenceBreakdown, safety: SafetySettings) -> bool:
    """TTS gate (§66, §95, §201): TTS is a strictly stricter path than text."""
    if breakdown.translation_confidence is None:
        return False
    verdict = evaluate_safety(breakdown, safety)
    if verdict.decision is not SafetyDecision.ALLOW:
        return False
    return breakdown.translation_confidence >= safety.tts_min_confidence