# ADR-0008: Phase 7 pilot-readiness quality gate (golden dataset, RCER, benchmark)

**Status:** Accepted

**Context:** §196 Phase 7 is the last implementation phase before the real
mosque pilot. Its spec line reads: *golden dataset, provider benchmark, real
mosque pilot, security review, privacy/legal review, release candidate.* Three of
those are real-world activities, not code: the on-site pilot (§174/§175), the
security review, and the privacy/legal review (§138, §180) all require humans,
a venue and counsel. They are tracked as pilot blockers, not shipped as software.

What Phase 7 *can* deliver deterministically is the **quality gate** that the
pilot depends on and that §197/§213 list under QUALITY: a golden dataset, the
critical test cases, and a provider-benchmark harness. Before a khutbah is ever
translated live, the acceptance the earlier phases kept deferring - "a critical
religious error is never broadcast" (§0 rule 5) - must be *measured*, not
assumed. §215 defines the metric for exactly this: **RCER = Religious Critical
Error Rate**, and §203 is explicit that the release-blocking questions must be
measured, not guessed.

**Decisions:**

1. **The golden dataset is annotated data, not a new engine (§172).** `eval/
   golden.py` carries curated `GoldenCase`s using the §172 annotation fields
   (transcript, religious source ref, critical terms/numbers/negations, gold
   target, severity) and the §215 critical-error taxonomy. It exercises the
   *existing* engines - the §78/§86 router, the §79 validator, the §76 scripture
   matcher and the §80 TTS gate - so the gate tests the same code the live
   pipeline runs, not a reimplementation. A tiny in-memory `APPROVED_SCRIPTURE`
   store stands in for the curated scripture DB (§42) so the gate stays
   provider-free and deterministic (§185, §69).

2. **RCER counts only what the rule engine owns; semantic swaps are known gaps
   (§170, §215).** Each case is tagged `requires_model`. Rule-detectable
   religious criticals (a number change §66, a negation flip §67, an unverified
   Quran line §78/§206, an empty output) are *accountable now* and must block, so
   RCER must be 0.0. Meaning-swaps the rule engine structurally cannot see -
   halal<->haram, agent/object swap (§215) - are marked `requires_model` and
   reported as `known_gaps` for the §173 human evaluation. They are EXCLUDED from
   the automated pass/fail and from the RCER denominator: faking a BLOCK the
   engine cannot produce, or failing the gate on a defect it cannot fix, would
   both violate §0 rule 8 (fail safe, never guess). This is the honest boundary
   between "verified safe today" and "needs model work before V1.1".

3. **The §80 TTS gate is simulated, never invoked (§194, §201).** `evaluate_case`
   feeds each validated candidate through `domain.tts.allow_tts` with a
   representative confidence and `age_ms=0`, so the benchmark proves the safety
   invariant *an unsafe output is never spoken* without a TTS backend. A blocked
   critical, and an unverified hadith (text-allowed but reference-less), must both
   come back `speak=False` - TTS stays strictly stricter than text (§95).

4. **The benchmark report is the §204 template, filled honestly.** `eval/
   benchmark.py::run_benchmark` returns the §204 shape. Deterministic fields
   (safety critical total/blocked, unsafe-TTS count, RCER, glossary violations)
   are measured now; provider/live-pilot fields (ASR WER, latency p50/p95,
   monthly cost, uptime) are `None`, never a fabricated zero - they are filled
   when a real provider runs (§203). `decision_pass` is the gate: no accountable
   failure AND RCER == 0 AND no unsafe TTS.

5. **WER is a pure, unit-tested helper (§5).** `golden.word_error_rate` is the
   standard Levenshtein word error rate (substitution+insertion+deletion /
   reference length), dependency-free so ASR quality is reproducible and testable
   before any provider is wired.

**Consequences:**
- §196/§197/§213 QUALITY: golden dataset + critical test cases + provider
  benchmark harness are delivered and green (13 new tests; full suite 188).
- §215 RCER is now a first-class, measured metric: 0.0 on the current rule
  engine, with the accountable set non-empty so the zero is meaningful, not
  vacuous.
- The two semantic gaps (halal/haram, agent/object) are surfaced explicitly as
  known gaps for the §174 pilot + §173 human eval, and as the input to the §170
  fine-tuning/glossary decision - not silently ignored.
- Still a pilot blocker (unchanged): admin auth (§116) - ADR-0004. The remaining
  Phase 7 items (real mosque pilot, security review, privacy/legal review,
  release candidate) are real-world activities tracked here, not code.
