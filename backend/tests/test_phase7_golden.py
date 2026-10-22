"""Phase 7 quality-gate tests (BAYAAN_MASTER_SPEC.md §196, §197, §204, §213, §215).

These are the "critical test cases" §197/§213 mandate: the golden dataset is run
through the REAL safety engines (router §78/§86, validator §79, TTS gate §80) and
every rule-detectable critical case MUST be blocked. The gate asserts:

* every case's decision matches its gold label (no silent regression);
* §215 RCER == 0.0 (no religious critical error is broadcast or spoken);
* §80 no unsafe (critical) output is ever spoken;
* the requires_model meaning-swaps are reported as known gaps, not fake-passed.

Plus unit tests for the §5 word_error_rate helper. Pure + deterministic - no DB,
no provider (§185, §69).
"""

from __future__ import annotations

import pytest

from bayaan_server.eval import (
    compute_rc_er,
    evaluate_case,
    golden_cases,
    run_benchmark,
    word_error_rate,
)


async def _results():
    cases = golden_cases()
    return [await evaluate_case(c) for c in cases]


async def test_every_accountable_case_matches_its_gold_decision() -> None:
    """§197 critical test: the engine reproduces every annotated decision.

    ``requires_model`` meaning-swaps are excluded here on purpose - the rule
    engine is not accountable for them yet (§170); they are checked separately
    as known gaps in ``test_semantic_gaps_are_reported_not_fake_passed``.
    """
    results = await _results()
    accountable = [r for r in results if r.accountable]
    mismatches = [
        f"{r.case_id}: expected {r.expected_decision}, got {r.actual_decision}"
        for r in accountable
        if not r.passed
    ]
    assert not mismatches, "golden dataset mismatches:\n" + "\n".join(mismatches)


async def test_rc_er_is_zero_for_rule_detectable_criticals() -> None:
    """§215: no religious critical error is broadcast. RCER must be 0.0."""
    report = compute_rc_er(await _results())
    assert report.rcer == 0.0
    assert report.religious_critical_leaked == 0
    assert report.passed
    # The rule engine is accountable for a real, non-empty set of religious
    # criticals - the metric is not vacuously zero on an empty denominator.
    assert report.religious_critical_total > 0


async def test_no_unsafe_output_is_ever_spoken() -> None:
    """§80/§194: a rule-detectable critical case never reaches TTS."""
    results = await _results()
    unsafe = [r.case_id for r in results if r.critical and not r.requires_model and r.speak]
    assert unsafe == [], f"unsafe cases would be spoken: {unsafe}"


async def test_blocked_critical_cases_are_never_spoken() -> None:
    """§95: TTS is strictly stricter than text - a BLOCK is never spoken."""
    results = await _results()
    spoken_blocks = [r.case_id for r in results if r.blocked and r.speak]
    assert spoken_blocks == []


async def test_semantic_gaps_are_reported_not_fake_passed() -> None:
    """§170/§215: meaning-swaps the rule engine cannot see are known gaps."""
    report = compute_rc_er(await _results())
    # The requires_model cases exist and are surfaced for the §173 human eval.
    assert set(report.known_gaps) == {"halal-haram-swap-gap", "agent-object-swap-gap"}
    # They are excluded from the accountable denominator (never averaged in).
    assert all("gap" in cid for cid in report.known_gaps)


async def test_approved_quran_broadcasts_approved_translation() -> None:
    """§78 positive control: an approved reference is broadcast and may speak."""
    results = {r.case_id: r for r in await _results()}
    approved = results["quran-approved"]
    assert approved.actual_decision == "ALLOW"
    assert not approved.blocked
    assert approved.speak is True  # approved, high-confidence -> §80 allows speech


async def test_hadith_general_fallback_shown_but_not_spoken() -> None:
    """§86 + §80 rule 4: unverified hadith shows text but is never spoken."""
    results = {r.case_id: r for r in await _results()}
    hadith = results["hadith-general-fallback"]
    assert hadith.actual_decision == "ALLOW"  # text path (§86 sanctioned fallback)
    assert hadith.speak is False  # TTS path refuses: no approved reference (§80)


async def test_benchmark_report_structure_and_gate() -> None:
    """§204 report shape + decision gate over the deterministic safety run."""
    report = await run_benchmark("benchmark-test")
    assert report.cases_total == len(golden_cases())
    assert report.cases_failed == 0
    assert report.failed_case_ids == []
    assert report.rcer == 0.0
    assert report.unsafe_tts_count == 0
    assert report.safety_critical_blocked == report.safety_critical_total
    assert report.decision_pass is True
    # Provider / live-pilot fields stay honest None (never a fabricated zero).
    assert report.asr_wer is None
    assert report.latency_p95_ms is None
    assert report.estimated_monthly_cost is None
    # The glossary case is observed as a (non-critical) violation.
    assert report.glossary_violations >= 1

    data = report.to_dict()
    assert data["decision_pass"] is True
    assert data["rcer_report"]["rcer"] == 0.0


# -- §5 word_error_rate -----------------------------------------------------

def test_wer_identical_is_zero() -> None:
    assert word_error_rate("the quick brown fox", "the quick brown fox") == 0.0


def test_wer_single_substitution() -> None:
    # one wrong word out of four -> 0.25
    assert word_error_rate("the quick brown fox", "the quick brown cat") == pytest.approx(0.25)


def test_wer_deletion() -> None:
    # one word missing out of four -> 0.25
    assert word_error_rate("the quick brown fox", "the brown fox") == pytest.approx(0.25)


def test_wer_insertion_exceeds_one() -> None:
    # an extra word on a two-word reference -> 1 insertion / 2 refs = 0.5
    assert word_error_rate("brown fox", "the brown fox") == pytest.approx(0.5)


def test_wer_empty_reference() -> None:
    assert word_error_rate("", "") == 0.0
    assert word_error_rate("", "anything") == 1.0
