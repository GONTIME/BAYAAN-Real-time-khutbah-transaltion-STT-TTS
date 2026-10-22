"""Provider benchmark harness (BAYAAN_MASTER_SPEC.md §196, §203, §204, §213).

§204 fixes the benchmark report shape; §203 lists the research questions that
must be *measured, not guessed*. This module runs the golden dataset through the
safety engines and fills the report with the numbers the deterministic,
provider-free quality gate can actually produce today:

* ``safety``  - critical cases total/blocked, unsafe-TTS count (§204 safety).
* ``rcer``    - §215 Religious Critical Error Rate over rule-detectable cases.
* ``translation`` - glossary violations observed on the golden set (§204).

Fields that require a REAL provider or a live mosque (ASR WER, latency p50/p95,
monthly cost, uptime) are reported as ``None`` rather than a fabricated zero -
§0 rule 8 (fail safe, never guess) and §203 ("measure, do not guess"). When real
providers land, the same report is filled with measured values; the structure is
the contract.

``run_benchmark`` is async (the router's hadith path awaits the MT port) but
performs no I/O against any provider: it is fully deterministic and unit-testable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from bayaan_server.config import SafetySettings, ScriptureSettings, TtsSettings, ValidationSettings

from .golden import golden_cases
from .rc_er import CaseResult, RcerReport, compute_rc_er, evaluate_case


@dataclass
class BenchmarkReport:
    """§204 Benchmark Report Template, filled by the deterministic safety gate.

    ``None`` means "not measurable without a real provider / live pilot yet" -
    never a fabricated number. ``decision_pass`` is the quality-gate verdict:
    every rule-detectable critical case matches its gold label AND RCER == 0.
    """

    run_id: str
    dataset: str
    cases_total: int = 0
    cases_passed: int = 0
    cases_failed: int = 0
    safety_critical_total: int = 0
    safety_critical_blocked: int = 0
    unsafe_tts_count: int = 0
    rcer: float = 0.0
    rcer_report: RcerReport | None = None
    glossary_violations: int = 0
    # Provider / live-pilot fields - honest None until a real provider runs.
    asr_wer: float | None = None
    translation_human_score: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    estimated_monthly_cost: float | None = None
    known_gaps: list[str] = field(default_factory=list)
    failed_case_ids: list[str] = field(default_factory=list)

    @property
    def decision_pass(self) -> bool:
        """§204 decision.pass: no failed critical case and RCER == 0."""
        return self.cases_failed == 0 and self.rcer == 0.0 and self.unsafe_tts_count == 0

    def to_dict(self) -> dict:
        """A JSON-friendly view (§204 report as data, e.g. for /metrics or a file)."""
        data = asdict(self)
        data["rcer_report"] = asdict(self.rcer_report) if self.rcer_report else None
        data["decision_pass"] = self.decision_pass
        return data


async def run_benchmark(
    run_id: str = "benchmark-local",
    *,
    validation: ValidationSettings | None = None,
    safety: SafetySettings | None = None,
    tts: TtsSettings | None = None,
    scripture: ScriptureSettings | None = None,
) -> BenchmarkReport:
    """Run the golden dataset through the safety engines and build a §204 report."""

    cases = golden_cases()
    results: list[CaseResult] = [
        await evaluate_case(
            case,
            validation=validation,
            safety=safety,
            tts=tts,
            scripture=scripture,
        )
        for case in cases
    ]

    rcer = compute_rc_er(results)
    # Only accountable (rule-detectable) cases count toward the pass/fail gate;
    # requires_model meaning-swaps are known gaps, not failures (§170/§0 rule 8).
    failed = [r for r in results if r.accountable and not r.passed]
    critical = [r for r in results if r.critical and not r.requires_model]
    critical_blocked = [r for r in critical if r.blocked]
    # §80: an unsafe (rule-detectable critical) case must never be spoken.
    unsafe_tts = [r for r in critical if r.speak]
    glossary_violations = sum(1 for r in results if "glossary_violation" in r.warnings)

    return BenchmarkReport(
        run_id=run_id,
        dataset=f"golden:{len(cases)}cases",
        cases_total=len(results),
        cases_passed=len(results) - len(failed),
        cases_failed=len(failed),
        safety_critical_total=len(critical),
        safety_critical_blocked=len(critical_blocked),
        unsafe_tts_count=len(unsafe_tts),
        rcer=rcer.rcer,
        rcer_report=rcer,
        glossary_violations=glossary_violations,
        known_gaps=list(rcer.known_gaps),
        failed_case_ids=[r.case_id for r in failed],
    )
