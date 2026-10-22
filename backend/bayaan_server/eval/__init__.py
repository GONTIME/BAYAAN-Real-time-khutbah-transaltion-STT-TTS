"""Phase 7 quality gate (BAYAAN_MASTER_SPEC.md §196, §197, §204, §213, §215).

Public API for the deterministic, provider-free safety benchmark:
the golden dataset, the §215 RCER metric and the §204 benchmark harness.
"""

from __future__ import annotations

from .benchmark import BenchmarkReport, run_benchmark
from .golden import APPROVED_SCRIPTURE, GoldenCase, golden_cases, word_error_rate
from .rc_er import CaseResult, RcerReport, compute_rc_er, evaluate_case

__all__ = [
    "APPROVED_SCRIPTURE",
    "BenchmarkReport",
    "CaseResult",
    "GoldenCase",
    "RcerReport",
    "compute_rc_er",
    "evaluate_case",
    "golden_cases",
    "run_benchmark",
    "word_error_rate",
]
