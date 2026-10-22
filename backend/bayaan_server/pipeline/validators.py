"""Translation validator chain (BAYAAN_MASTER_SPEC.md §79, §66-§68, §101, §23).

§79 validate(source, target, metadata): the check_* functions below are the
spec's checks. §23 turns the warnings into a risk score; a CRITICAL warning
(number/negation/scripture/empty) always forces BLOCK and is never averaged
away (§167). Each check is a pure function over text + metadata, so it is
unit-testable without DB or providers. §0 rule 8: a check never fabricates a
warning - it only fires on a pattern it can actually surface.
"""

from __future__ import annotations

from bayaan_server.config import ValidationSettings
from bayaan_server.domain.enums import ContentType, RiskLevelName, SafetyDecision, ValidationSeverity
from bayaan_server.domain.validation import ValidationVerdict, ValidationWarning
from bayaan_server.sermon.glossary import GlossaryTerm, glossary_violations
from bayaan_server.pipeline import entities, negation, numbers

# §101 severity per warning code. Only criticals force a BLOCK; everything
# else is a reviewable warning whose weight feeds the §23 risk score.
_SEVERITY: dict[str, ValidationSeverity] = {
    "number_mismatch": ValidationSeverity.CRITICAL,
    "negation_flip": ValidationSeverity.CRITICAL,
    "scripture_unverified": ValidationSeverity.CRITICAL,
    "empty_target": ValidationSeverity.CRITICAL,
    "entity_omitted": ValidationSeverity.MEDIUM,
    "glossary_violation": ValidationSeverity.MEDIUM,
    "target_language_mismatch": ValidationSeverity.MEDIUM,
    "unexpected_addition": ValidationSeverity.LOW,
}

# §23 risk contribution per warning code, in [0,1].
_RISK_WEIGHT: dict[str, float] = {
    "number_mismatch": 1.0,
    "negation_flip": 1.0,
    "scripture_unverified": 1.0,
    "empty_target": 1.0,
    "target_language_mismatch": 0.5,
    "entity_omitted": 0.4,
    "glossary_violation": 0.3,
    "unexpected_addition": 0.1,
}


def _warning(code: str, message: str) -> ValidationWarning:
    return ValidationWarning(code=code, message=message, severity=_SEVERITY[code])


def check_numbers(source: str, target: str, language: str) -> list[ValidationWarning]:
    """§66: a source number missing from the translation is CRITICAL."""
    return [
        _warning("number_mismatch", m)
        for m in numbers.number_mismatches(source, target, language)
    ]


def check_negation(source: str, target: str, language: str) -> list[ValidationWarning]:
    """§67: a source->target negation flip is CRITICAL."""
    return [
        _warning("negation_flip", m)
        for m in negation.negation_mismatch(source, target, language)
    ]


def check_entities(source: str, target: str, language: str) -> list[ValidationWarning]:
    """§68: numeric entities are strictly enforced (see entities module)."""
    return [
        _warning("entity_omitted", m)
        for m in entities.entity_preservation_violations(source, target, language)
    ]


def check_glossary(
    source: str, target: str, language: str, terms: list[GlossaryTerm]
) -> list[ValidationWarning]:
    """§22/§150: an approved source term whose approved translation is absent."""
    return [
        _warning("glossary_violation", m)
        for m in glossary_violations(source, target, language, terms)
    ]

def check_unexpected_additions(
    source: str, target: str, settings: ValidationSettings
) -> list[ValidationWarning]:
    """§22.3 forbidden behavior: the translation must not balloon far beyond
    the source length (a cheap proxy for added commentary/opinion). Only fires
    when the target is much longer than the source; never on normal variance."""
    src_len = max(1, len(source.split()))
    tgt_len = len(target.split())
    if tgt_len > src_len * settings.max_length_ratio:
        return [
            _warning(
                "unexpected_addition",
                f"translation is much longer than source "
                f"({tgt_len} vs {src_len} words); possible added content",
            )
        ]
    return []


def check_scripture_policy(
    content_type_value: str, metadata: dict, settings: ValidationSettings
) -> list[ValidationWarning]:
    """§78/§86: QURAN output MUST carry an approved reference (CRITICAL block);
    HADITH without a reference is a warning unless the strict policy is on."""
    try:
        ctype = ContentType(content_type_value)
    except ValueError:
        return []
    if ctype is ContentType.QURAN and not metadata.get("approved_reference"):
        return [
            _warning(
                "scripture_unverified",
                "Quran text requires an approved reference; "
                "general translation is not permitted",
            )
        ]
    if (
        ctype is ContentType.HADITH
        and settings.hadith_requires_reference
        and not metadata.get("approved_reference")
    ):
        return [
            _warning(
                "scripture_unverified",
                "Hadith translation lacks an approved reference",
            )
        ]
    return []


def check_target_language(
    target: str, language: str, settings: ValidationSettings
) -> list[ValidationWarning]:
    """Sanity: an empty target can never be shown as a real translation."""
    if not target.strip():
        return [_warning("empty_target", "translation is empty")]
    return []


def calculate_risk(
    warnings: list[ValidationWarning], settings: ValidationSettings
) -> float:
    """§23 risk score: max of the per-warning contributions (worst case
    dominates), clamped to [0,1]. Criticals therefore always reach 1.0."""
    if not warnings:
        return 0.0
    risk = max(_RISK_WEIGHT.get(w.code, 0.2) for w in warnings)
    return min(1.0, max(0.0, risk))


def _risk_level(risk: float, settings: ValidationSettings) -> RiskLevelName:
    if risk >= settings.high_risk_threshold:
        return RiskLevelName.HIGH
    if risk >= settings.medium_risk_threshold:
        return RiskLevelName.MEDIUM
    if risk > 0.0:
        return RiskLevelName.LOW
    return RiskLevelName.NONE


class Validator:
    """§79 validator. Pure logic; thresholds come from ``ValidationSettings``
    (§185 no magic values)."""

    def __init__(self, settings: ValidationSettings) -> None:
        self._settings = settings

    def validate(
        self,
        source: str,
        target: str,
        language: str,
        *,
        content_type: str = "GENERAL",
        metadata: dict | None = None,
        glossary_terms: list[GlossaryTerm] | None = None,
    ) -> ValidationVerdict:
        metadata = metadata or {}
        glossary_terms = glossary_terms or []
        s = self._settings

        warnings: list[ValidationWarning] = []
        if s.check_numbers:
            warnings += check_numbers(source, target, language)
        if s.check_negation:
            warnings += check_negation(source, target, language)
        if s.check_entities:
            warnings += check_entities(source, target, language)
        if s.check_glossary:
            warnings += check_glossary(source, target, language, glossary_terms)
        if s.check_additions:
            warnings += check_unexpected_additions(source, target, s)
        if s.check_scripture:
            warnings += check_scripture_policy(content_type, metadata, s)
        warnings += check_target_language(target, language, s)

        risk = calculate_risk(warnings, s)

        # §167: a critical warning always BLOCKs; the risk average never
        # rescues it.
        if any(w.critical for w in warnings):
            return ValidationVerdict(
                decision=SafetyDecision.BLOCK,
                risk=risk,
                risk_level=_risk_level(risk, s),
                warnings=warnings,
            )
        if risk >= s.high_risk_threshold:
            return ValidationVerdict(
                decision=SafetyDecision.DISPLAY_ONLY,
                risk=risk,
                risk_level=_risk_level(risk, s),
                warnings=warnings,
            )
        if warnings:
            return ValidationVerdict(
                decision=SafetyDecision.WARNING,
                risk=risk,
                risk_level=_risk_level(risk, s),
                warnings=warnings,
            )
        return ValidationVerdict(
            decision=SafetyDecision.ALLOW, risk=0.0, risk_level=RiskLevelName.NONE
        )

