"""TTS gate (BAYAAN_MASTER_SPEC.md §80 tts_gate.py, §95, §66, §201).

This is the single chokepoint that decides whether a translated caption may
be turned into speech. It is deliberately STRICTER than the text path (§95):
a caption can be displayed while being refused for TTS.

§80 allow_tts(result) - refuse unless ALL of these hold:
  1. status == PASS                 (safety engine ALLOWed the caption)
  2. validation_status == PASS      (§79 validator did not BLOCK)
  3. confidence >= TTS_MIN_CONFIDENCE
  4. QURAN/HADITH carry an approved reference (§206: never speak unverified
     sacred text - a number/negation/scripture breach already BLOCKs upstream)
  5. age_ms <= MAX_TTS_STALENESS    (stale audio is dropped, §88)

Pure and provider-free: it takes plain values + typed settings and returns an
immutable verdict with explainable reasons (§168). No DB, no network, no
globals - so it is unit-testable in isolation and the live pipeline only
orchestrates it. Reasons are operator-facing and never leak to the public
client (§134).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bayaan_server.config import SafetySettings, TtsSettings
from bayaan_server.domain.enums import ContentType, SafetyDecision


@dataclass(frozen=True)
class TtsCandidate:
    """The inputs §80 needs for one (segment, language) TTS decision.

    ``safety_decision``/``validation_decision`` are the already-computed
    upstream verdicts (ALLOW/PASS); we do not recompute safety here, we only
    enforce that it passed. ``translation_confidence`` is the §166 signal, and
    ``age_ms`` is how long ago the source utterance was produced.
    """

    text: str
    language: str
    content_type: ContentType
    safety_decision: SafetyDecision
    validation_decision: SafetyDecision
    translation_confidence: float | None
    age_ms: int
    approved_reference: str | None = None


@dataclass(frozen=True)
class TtsGateVerdict:
    """§80 outcome: whether to speak, plus explainable refusal reasons (§168)."""

    speak: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.speak


def allow_tts(
    candidate: TtsCandidate,
    safety: SafetySettings,
    tts: TtsSettings,
) -> TtsGateVerdict:
    """§80 gate. Returns a verdict; ``speak`` is True only if every rule holds.

    Order mirrors the spec pseudocode so a reader can diff them directly.
    A refusal reason is always recorded (never silently dropped) so the
    operator can see WHY a line was not spoken (§168) and so the acceptance
    criterion "unsafe outputs never reach TTS" is auditable.
    """
    reasons: list[str] = []

    # §80 (1): the caption must have passed the text safety gate first.
    if candidate.safety_decision is not SafetyDecision.ALLOW:
        reasons.append(f"safety_{candidate.safety_decision.value.lower()}")

    # §80 (2): the §79 validator must have PASSED (not BLOCK / DISPLAY_ONLY /
    # WARNING). TTS is stricter than text, so anything short of ALLOW refuses.
    if candidate.validation_decision is not SafetyDecision.ALLOW:
        reasons.append(f"validation_{candidate.validation_decision.value.lower()}")

    # §80 (3): confidence floor (stricter than the text translation minimum).
    # Real MT providers (ollama/ollama_cloud/libre) report NO confidence (§0
    # rule 8 - never invented), which is refused UNLESS the operator opted out
    # of the confidence requirement (tts.require_translation_confidence=False,
    # the explicit "trust the approved chain" path for providers that cannot
    # score). A NUMERIC confidence below the floor is always refused, whatever
    # the flag.
    if candidate.translation_confidence is None:
        if tts.require_translation_confidence:
            reasons.append("no_translation_confidence")
    elif candidate.translation_confidence < safety.tts_min_confidence:
        reasons.append(f"tts_confidence_below_{safety.tts_min_confidence}")

    # §80 (4): sacred text is never spoken without an approved reference.
    if candidate.content_type in (ContentType.QURAN, ContentType.HADITH) and (
        not candidate.approved_reference
    ):
        reasons.append("sacred_text_unapproved")

    # §80 (5): stale audio is dropped rather than spoken late.
    if candidate.age_ms > tts.max_staleness_ms:
        reasons.append(f"stale_age_ms>{tts.max_staleness_ms}")

    # §95/§0 rule 5: empty text is never spoken.
    if not candidate.text.strip():
        reasons.append("empty_text")

    return TtsGateVerdict(speak=not reasons, reasons=reasons)
