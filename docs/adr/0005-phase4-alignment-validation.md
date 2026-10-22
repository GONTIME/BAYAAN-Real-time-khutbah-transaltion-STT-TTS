# ADR-0005: Phase 4 alignment, validation, and religious-content routing

**Status:** Accepted

**Context:** §193 Phase 4 wires the engines the earlier phases deferred behind
feature flags: the §77 sermon aligner, the §79 validation chain, the
§78/§86/§206 religious-content routing, and the §166/§167 confidence
composition. Until now `alignment_enabled`/`validation_enabled` were `false`
and their signals were excluded from the §167 composite (ADR-0003 decision 3).

**Decisions:**

1. **All new logic is pure and provider-free.** `pipeline/aligner.py`,
   `pipeline/validators.py` (+ the `Validator` class), `pipeline/numbers.py`,
   `pipeline/negation.py`, `pipeline/entities.py`, `pipeline/confidence.py`,
   `pipeline/translation_router.py`, and `sermon/scripture.py` take plain text
   and typed settings and return immutable verdicts. No DB, no network, no
   globals — so each is unit-testable in isolation and the live pipeline just
   orchestrates them (§69 reproducibility).

2. **Stdlib-only fuzzy matching; semantic signal is absent, never faked.**
   Alignment and scripture tiers use `difflib.SequenceMatcher` plus a lexical
   Jaccard/continuity score. No embedding model is bundled, so
   `AlignmentResult.semantic_available` is recorded `False` (§0 rule 8: absent
   ≠ zero). `rapidfuzz`/`numpy` are not introduced.

3. **Criticals always BLOCK; risk is max-weighted, never averaged**
   (§101/§23/§167). A changed number (§66), a negation flip (§67), unverified
   sacred text (§78/§206) or an empty target is `CRITICAL` and forces
   `SafetyDecision.BLOCK` regardless of any high composite. Non-critical
   warnings contribute `max(weight)` risk and bucket to `DISPLAY_ONLY`/`WARNING`
   rather than blocking.

4. **Quran is never machine-translated** (§206). With no approved reference
   the router returns `blocked` and the public client sees the §134 fallback
   message only; the operator-facing reason is written to the audit log
   (§98/§168). Hadith falls back to a general, **non-attributed** translation
   when unverified (§86) — we never claim a citation we cannot verify.

5. **Absent signals are `None`, not `0.0`** (§166). When there is no prepared
   sermon the alignment signal is `None` and is excluded from the §167
   composite instead of tripping the safety gate; the same holds for a provider
   that reports no confidence. This keeps disabled/absent engines from being
   faked with silent zero scores (carried forward from ADR-0003).

6. **Governance is data, not code.** Scripture references are seeded and
   approved through the new `/api/v1/scripture` router; only `approved` verses
   are reachable live (§180/§0 rule 8). The verse store ships empty by default,
   so every Quran hit is `UNKNOWN` and blocks — the safe default until a
   curated, licence-cleared dataset (§216) is loaded.

**Consequences:**
- §193 Phase 4 acceptance is exercised live: an unverified Quran line is
  blocked with a public fallback, while an approved reference broadcasts the
  approved translation; ordinary speech still translates and now carries a
  routing tag + §95 speak flag.
- Full suite grows to 117 tests (42 new: validators, aligner, scripture
  routing, confidence composition, end-to-end WS safety, and the §96 review
  panel's `validation` array).
- The review panel (`/review`) now emits a per-draft `validation` array with
  verdicts and explainable warning codes (§96/§168).
- Still a pilot blocker (unchanged): admin auth (§116) — `/api/v1/scripture`
  and `/admin` rely on operator network trust until it lands (ADR-0004).
