"""Validation domain contracts (BAYAAN_MASTER_SPEC.md §79, §101, §23).

§79 validator returns a verdict with warnings, a risk score and a decision
(PASS / DISPLAY_ONLY / BLOCK). §101 classifies each warning's severity so a
CRITICAL warning (number/negation/scripture) always BLOCKs (§167: a safety
blocker is never rescued by a high average).

These are the immutable contracts the validator chain returns; the pure
validation logic lives in ``pipeline/validators.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import RiskLevelName, SafetyDecision, ValidationSeverity


class ValidationWarning(BaseModel):
    """§101: one classified issue found while validating a translation.

    ``code`` is a stable, explainable identifier (§168) - never a leaked
    prompt or internal score. ``critical`` short-circuits the verdict to
    BLOCK regardless of the overall risk average.
    """

    code: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.MEDIUM

    @property
    def critical(self) -> bool:
        return self.severity is ValidationSeverity.CRITICAL


class ValidationVerdict(BaseModel):
    """§79 validate() outcome for one candidate translation.

    - ``decision``: PASS->ALLOW, DISPLAY_ONLY, or BLOCK.
    - ``risk``: §23 composite risk in [0,1].
    - ``risk_level``: bucketed risk for display/review (§95).
    - ``warnings``: every classified issue (explainable to the operator).
    """

    decision: SafetyDecision
    risk: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevelName = RiskLevelName.NONE
    warnings: list[ValidationWarning] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.decision is SafetyDecision.ALLOW
