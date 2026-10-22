"""Golden dataset for the Phase 7 quality gate (BAYAAN_MASTER_SPEC.md §196,
§172, §197, §204, §213, §215).

A *golden* case is a hand-annotated example that pins one safety behaviour the
system must never violate. §172 fixes the annotation fields (exact transcript,
religious source refs, critical terms, numbers, negations, names, gold target
translation, severity); this module carries the subset the automated safety
gate can actually check today, plus the §215 critical-error taxonomy.

Two kinds of case live here:

* **Rule-detectable criticals** (``requires_model=False``): the safety engine is
  responsible for them *now* - a number change (§66), a negation flip (§67), an
  empty output, or an unverified Quran line (§78/§206). The benchmark MUST block
  every one of them; a leak is a Religious Critical Error (§215) and fails the
  gate. These drive the automated ``RCER`` and the "critical test cases" of
  §197/§213.

* **Semantic criticals** (``requires_model=True``): meaning swaps the current
  rule/lexical engine cannot see - halal<->haram, agent/object swap, a changed
  ayah/hadith meaning (§215). §170 is explicit that these need model work plus
  §173 human evaluation, so they are reported as *known gaps* for the pilot and
  are deliberately EXCLUDED from the automated pass/fail - faking a BLOCK the
  engine cannot produce would violate §0 rule 8 (fail safe, never guess).

Nothing here performs I/O or touches a provider: the dataset is pure data and
``word_error_rate`` is a pure function, so the whole quality gate is
deterministic and unit-testable (§185 no magic values, §69 reproducibility).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bayaan_server.domain.sermon import GlossaryTerm
from bayaan_server.sermon.scripture import ScriptureReference

# An approved Quran reference used to exercise the "approved -> broadcast the
# approved translation" path (§78) as the positive control for the sacred-text
# gate. In a real deployment this store is the curated scripture DB (§42); here
# it is a tiny in-memory stand-in so the benchmark stays provider-free.
APPROVED_SCRIPTURE: list[ScriptureReference] = [
    ScriptureReference(
        key="94:6",
        normalized_arabic="ان مع العسر يسرا",
        kind="QURAN",
        surah="94",
        ayah="6",
        translations={
            "en": "indeed, with hardship comes ease",
            "it": "in verita, con la difficolta viene la facilita",
        },
        approved=True,
        edition="pilot-fixture",
        license="fixture",
    ),
]


@dataclass
class GoldenCase:
    """One annotated safety case (§172).

    ``arabic`` is the source the imam speaks; ``target`` is the candidate
    translation under test. For a rule-detectable critical case ``target`` is a
    deliberately FLAWED translation that the safety engine must catch; for a
    clean case it is a correct translation that must be broadcast.

    ``expected_decision`` is the ``SafetyDecision`` value the engine must return
    ("ALLOW" / "WARNING" / "DISPLAY_ONLY" / "FALLBACK" / "BLOCK"). ``critical``
    marks a case whose leak would be a §215 religious-critical error;
    ``religious`` further marks the ones that feed ``RCER``. ``requires_model``
    marks a semantic gap the rule engine does not yet cover (§170).
    """

    id: str
    arabic: str
    language: str = "en"
    content_type: str = "GENERAL"
    target: str = ""
    expected_decision: str = "ALLOW"
    # §215 taxonomy label (negation_change, number_change, quran_unverified, ...).
    category: str = ""
    critical: bool = False
    religious: bool = False
    requires_model: bool = False
    # Representative translation confidence, used only to simulate the §80 TTS
    # gate (approved text is treated as fully confident).
    confidence: float = 0.95
    # Approved scripture key when this sacred line is verified (§78/§80 rule 4).
    scripture_reference: str | None = None
    # Approved glossary terms to enforce for this case (§22).
    glossary_terms: list[GlossaryTerm] = field(default_factory=list)
    notes: str = ""


def _term(term_ar: str, normalized_ar: str, translations: dict[str, str]) -> GlossaryTerm:
    return GlossaryTerm(
        term_ar=term_ar,
        normalized_ar=normalized_ar,
        translations=translations,
        approved=True,
        revision=1,
    )


def golden_cases() -> list[GoldenCase]:
    """The curated critical dataset (§197 critical test cases, §215 taxonomy).

    Every case is deterministic and self-contained. Sacred lines are matched by
    exact normalized Arabic against :data:`APPROVED_SCRIPTURE`.
    """

    salah_term = _term("الصلاة", "صلاة", {"en": "the prayer (salah)"})

    cases: list[GoldenCase] = [
        # -- Clean controls: correct output MUST broadcast (ALLOW) ----------
        GoldenCase(
            id="general-clean",
            arabic="الحمد لله رب العالمين",
            target="praise be to Allah, Lord of the worlds",
            category="clean_general",
            confidence=0.95,
        ),
        GoldenCase(
            id="general-clean-it",
            arabic="الحمد لله رب العالمين",
            language="it",
            target="lode ad Allah, Signore dei mondi",
            category="clean_general",
            confidence=0.95,
        ),
        # -- §66 number change: 30 -> 13 is CRITICAL -> BLOCK --------------
        GoldenCase(
            id="number-change-critical",
            arabic="صام 30 يوما",
            target="he fasted for 13 days",
            expected_decision="BLOCK",
            category="number_change",
            critical=True,
            religious=True,  # a changed fast length changes a religious ruling
        ),
        GoldenCase(
            id="number-clean",
            arabic="صام 30 يوما",
            target="he fasted for 30 days",
            category="clean_number",
            confidence=0.95,
        ),
        # -- §67 negation flip: source negated, target affirmative ----------
        GoldenCase(
            id="negation-flip-critical",
            arabic="لا تشرب الخمر",
            target="drink the alcohol",
            expected_decision="BLOCK",
            category="negation_change",
            critical=True,
            religious=True,  # flips a prohibition into a command
        ),
        GoldenCase(
            id="negation-clean",
            arabic="لا تشرب الخمر",
            target="do not drink alcohol",
            category="clean_negation",
            confidence=0.95,
        ),
        # -- §78/§206 unverified Quran: never machine-translated -> BLOCK ---
        GoldenCase(
            id="quran-unverified-critical",
            arabic="قل هو الله احد",
            content_type="QURAN",
            target="",  # a Quran line is never MT'd; the router blocks it
            expected_decision="BLOCK",
            category="quran_unverified",
            critical=True,
            religious=True,
            confidence=1.0,
        ),
        # -- §78 approved Quran: broadcast the approved translation ---------
        GoldenCase(
            id="quran-approved",
            arabic="ان مع العسر يسرا",
            content_type="QURAN",
            target="indeed, with hardship comes ease",
            expected_decision="ALLOW",
            category="clean_quran",
            scripture_reference="94:6",
            confidence=1.0,
        ),
        # -- §86 unverified Hadith: general, non-attributed MT -------------
        # Text is ALLOWed (sanctioned fallback) but the §80 gate must refuse
        # speech because no approved reference backs it (TTS stricter than text).
        GoldenCase(
            id="hadith-general-fallback",
            arabic="قال رسول الله انما الاعمال بالنيات",
            content_type="HADITH",
            target="actions are judged by intentions",
            expected_decision="ALLOW",
            category="hadith_general_mt",
            confidence=0.95,
        ),
        # -- empty output is CRITICAL -> BLOCK -----------------------------
        GoldenCase(
            id="empty-critical",
            arabic="الحمد لله",
            target="   ",
            expected_decision="BLOCK",
            category="empty_output",
            critical=True,
            religious=False,  # an emptiness failure, not a meaning swap
        ),
        # -- §22 glossary: approved term present, translation missing -------
        # Non-critical: a reviewable WARNING (§79 grading), still broadcast.
        GoldenCase(
            id="glossary-warning",
            arabic="اقام الصلاة",
            target="he established worship",
            expected_decision="WARNING",
            category="glossary_violation",
            glossary_terms=[salah_term],
            confidence=0.95,
        ),
        # -- §215 semantic criticals the rule engine CANNOT yet detect ------
        # Reported as known gaps for the §174 pilot + §173 human eval, never
        # auto-passed (§170 model work required). Excluded from the RCER gate.
        GoldenCase(
            id="halal-haram-swap-gap",
            arabic="هذا حلال",
            target="it is forbidden",
            expected_decision="BLOCK",
            category="halal_haram_swap",
            critical=True,
            religious=True,
            requires_model=True,
        ),
        GoldenCase(
            id="agent-object-swap-gap",
            arabic="ضرب زيد عمرا",
            target="Amr hit Zayd",
            expected_decision="BLOCK",
            category="agent_object_swap",
            critical=True,
            religious=False,  # a grammar meaning-change, not a sacred-text swap
            requires_model=True,
        ),
    ]
    return cases


def word_error_rate(reference: str, hypothesis: str) -> float:
    """§5 ASR quality: word error rate = edit distance / reference length.

    Standard Levenshtein distance over word tokens (substitution + insertion +
    deletion), normalised by the number of reference words. Pure and
    dependency-free so it is unit-testable and reproducible. An empty reference
    scores 0.0 when the hypothesis is also empty, else 1.0 (nothing to align
    against -> any output is fully wrong). Values exceed 1.0 when the hypothesis
    is longer than the reference (insertions), matching the usual WER convention.
    """

    ref = reference.split()
    hyp = hypothesis.split()
    if not ref:
        return 0.0 if not hyp else 1.0

    # Dynamic-programming table of edit distances (rows: reference prefix).
    previous = list(range(len(hyp) + 1))
    for i, r_word in enumerate(ref, start=1):
        current = [i] + [0] * len(hyp)
        for j, h_word in enumerate(hyp, start=1):
            cost = 0 if r_word == h_word else 1
            current[j] = min(
                previous[j] + 1,         # deletion
                current[j - 1] + 1,      # insertion
                previous[j - 1] + cost,  # substitution / match
            )
        previous = current
    return previous[-1] / len(ref)

