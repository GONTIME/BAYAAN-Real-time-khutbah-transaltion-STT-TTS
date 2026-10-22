"""Religious Critical Error Rate (§215) + per-case safety evaluation (§196).

RCER (Religious Critical Error Rate) is the independent safety metric §215 asks
for - the rate at which a *religious-critical* error is broadcast or spoken. A
religious critical error is one of the §215 examples (negation flip, number or
date change, halal<->haram, a changed ayah/hadith meaning) reaching a client.

Scope is honest about what the current rule engine owns:

* ``RCER`` counts only **rule-detectable** religious criticals
  (``critical AND religious AND NOT requires_model``). The engine is
  accountable for these *now* and MUST block every one, so RCER must be 0.0.
* **Known gaps** (``requires_model=True``) are semantic meaning-swaps the rule
  engine cannot see (§170: they need model work + §173 human eval). They are
  reported for the pilot and EXCLUDED from the automated gate - counting them as
  failures the engine cannot fix, or faking a BLOCK it cannot produce, would
  both violate §0 rule 8.

Each case is run through the REAL safety engines - the §78/§86 router, the §79
validator and the §80 TTS gate - so this exercises the same code the live
pipeline uses, not a reimplementation. Pure logic + injected settings only: no
DB, no provider, deterministic (§185, §69).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bayaan_server.config import SafetySettings, ScriptureSettings, TtsSettings, ValidationSettings
from bayaan_server.domain.enums import ContentType, SafetyDecision
from bayaan_server.domain.tts import TtsCandidate, allow_tts
from bayaan_server.domain.validation import ValidationVerdict
from bayaan_server.pipeline.normalizer import ArabicNormalizerV1
from bayaan_server.pipeline.translation_router import TranslationRouter
from bayaan_server.pipeline.validators import Validator
from bayaan_server.sermon.scripture import ScriptureMatcher

from .golden import APPROVED_SCRIPTURE, GoldenCase


@dataclass
class CaseResult:
    """Outcome of evaluating one :class:`GoldenCase` through the safety engines."""

    case_id: str
    category: str
    expected_decision: str
    actual_decision: str
    critical: bool
    religious: bool
    requires_model: bool
    blocked: bool
    speak: bool  # would the §80 gate synthesize this? must be False when blocked
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """§197 critical test: the engine's decision matches the gold label."""
        return self.actual_decision == self.expected_decision

    @property
    def accountable(self) -> bool:
        """Is the rule engine responsible for this case *today*?

        ``requires_model`` meaning-swaps (§170) are NOT accountable yet - they
        are tracked as known gaps for the §173 human eval, never as gate
        failures. Everything else the safety engine must get right now.
        """
        return not self.requires_model

    @property
    def leaked(self) -> bool:
        """A rule-detectable critical that was NOT blocked (a §215 error)."""
        return self.critical and not self.requires_model and not self.blocked


@dataclass
class RcerReport:
    """§215 RCER plus the honest gap accounting for the pilot."""

    religious_critical_total: int
    religious_critical_leaked: int
    rcer: float
    known_gaps: list[str]  # case ids the rule engine cannot yet catch (§170)

    @property
    def passed(self) -> bool:
        """The safety gate: no rule-detectable religious critical may leak."""
        return self.rcer == 0.0


def _decision_value(decision: SafetyDecision) -> str:
    return decision.value

async def evaluate_case(
    case: GoldenCase,
    *,
    validation: ValidationSettings | None = None,
    safety: SafetySettings | None = None,
    tts: TtsSettings | None = None,
    scripture: ScriptureSettings | None = None,
) -> CaseResult:
    """Run one golden case through the §78/§86 router, §79 validator and §80 gate.

    Mirrors the live pipeline's decision order so the gate tests the same code:
    sacred text is routed (never machine-translated), the surviving candidate is
    validated, and the §80 TTS gate is simulated on the result.
    """

    validation = validation or ValidationSettings()
    safety = safety or SafetySettings()
    tts = tts or TtsSettings()
    scripture = scripture or ScriptureSettings()

    normalizer = ArabicNormalizerV1()
    normalized = normalizer.normalize(case.arabic)
    content_type = ContentType(case.content_type)

    router = TranslationRouter(ScriptureMatcher(scripture, APPROVED_SCRIPTURE), scripture)
    validator = Validator(validation)

    approved_reference: str | None = None
    target = case.target

    # §78/§86/§206: sacred text takes the routing path first. Quran is never
    # machine-translated; hadith falls back to a general (non-attributed) MT.
    if content_type is ContentType.QURAN:
        routing = await router.route_quran(normalized, case.language)
        if routing.blocked:
            return CaseResult(
                case_id=case.id,
                category=case.category,
                expected_decision=case.expected_decision,
                actual_decision=_decision_value(SafetyDecision.BLOCK),
                critical=case.critical,
                religious=case.religious,
                requires_model=case.requires_model,
                blocked=True,
                speak=False,
                warnings=["quran_unverified"],
            )
        target = routing.text
        approved_reference = routing.approved_reference
    elif content_type is ContentType.HADITH:
        class _ScriptedTranslator:
            """Returns the case's candidate translation as the general MT result."""

            def __init__(self, text: str) -> None:
                self._text = text

            async def translate(self, text, source, target):
                class _R:
                    pass

                r = _R()
                r.text = self._text
                return r

        routing = await router.route_hadith(
            normalized, case.language, _ScriptedTranslator(case.target)
        )
        if routing.blocked:
            return CaseResult(
                case_id=case.id,
                category=case.category,
                expected_decision=case.expected_decision,
                actual_decision=_decision_value(SafetyDecision.BLOCK),
                critical=case.critical,
                religious=case.religious,
                requires_model=case.requires_model,
                blocked=True,
                speak=False,
                warnings=["hadith_unverified"],
            )
        target = routing.text
        approved_reference = routing.approved_reference

    # §79 validation over the surviving candidate.
    verdict: ValidationVerdict = validator.validate(
        normalized,
        target,
        case.language,
        content_type=case.content_type,
        metadata={"approved_reference": approved_reference} if approved_reference else {},
        glossary_terms=case.glossary_terms,
    )
    decision = verdict.decision
    blocked = decision is SafetyDecision.BLOCK

    # §80 TTS gate: strictly stricter than text (§95). Simulated with the case's
    # representative confidence and age 0 so only the safety rules decide speech.
    gate = allow_tts(
        TtsCandidate(
            text=target,
            language=case.language,
            content_type=content_type,
            safety_decision=decision,
            validation_decision=decision,
            translation_confidence=case.confidence,
            age_ms=0,
            approved_reference=approved_reference,
        ),
        safety,
        tts,
    )

    return CaseResult(
        case_id=case.id,
        category=case.category,
        expected_decision=case.expected_decision,
        actual_decision=_decision_value(decision),
        critical=case.critical,
        religious=case.religious,
        requires_model=case.requires_model,
        blocked=blocked,
        speak=gate.speak,
        warnings=[w.code for w in verdict.warnings],
    )


def compute_rc_er(results: list[CaseResult]) -> RcerReport:
    """§215: RCER = leaked religious criticals / accountable religious criticals.

    Accountable = rule-detectable (``not requires_model``). Requires-model
    meaning-swaps are collected as known gaps, never averaged into the rate.
    """

    accountable = [r for r in results if r.religious and not r.requires_model]
    leaked = [r for r in accountable if r.leaked]
    gaps = [r.case_id for r in results if r.requires_model]
    total = len(accountable)
    rate = (len(leaked) / total) if total else 0.0
    return RcerReport(
        religious_critical_total=total,
        religious_critical_leaked=len(leaked),
        rcer=rate,
        known_gaps=gaps,
    )

