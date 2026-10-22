# ADR-0010 — Seed the Quran scripture store from Tanzil dumps (§42.2, §78, §206)

- **Status:** Accepted
- **Date:** 2026-10-12
- **Context:** The operator supplied `quran-simple.sql` (Arabic Uthmani-simple),
  `en.sahih.sql` (Saheeh International) and `it.piccardo.sql` (H. R. Piccardo) and
  asked for them merged into the database in a suitable format. `scripture_verses`
  (§42.1/§42.2) is the store the live path reads Quran from — §206 forbids machine
  translating the Quran, so without a populated store every Quran line is
  `UNKNOWN` and blocked, and no approved translation can ever surface (§78).

## Decisions

1. **Map the dumps onto the existing `scripture_verses` schema — no schema
   change.** One row per ayah: `key = "<surah>:<ayah>"` (the §42.2 stable id, same
   format `eval/golden.py` uses), `kind = 'QURAN'`, `surah`/`ayah` split out,
   `arabic` from `quran-simple.sql`, and `translations_json = {"en": …, "it": …}`
   joined on `(surah, ayah)`. `edition` and `license` are recorded per row for
   §69/§180 provenance.

2. **`normalized_arabic` comes from `ArabicNormalizerV1`**, the very normalizer the
   live path uses. This is the load-bearing detail: §78 matches on the normalized
   string, so a store indexed with a different normalizer (or with diacritics
   left in) would silently never match and every Quran line would still block.

3. **A loader, not a bulk SQL import: `backend/tools/load_quran_tanzil.py`.**
   The dumps are MySQL/phpMyAdmin format (`int(4)`, backticks, `\'` and `''`
   escapes — 363 and 2277 occurrences respectively) and cannot be fed to Postgres
   as-is. Writes go through `ScriptureRepository.upsert` so §187 still holds (one
   writer, the same path as the API), the load is recorded in `audit_log` (§98),
   and it is **idempotent** on `(kind, key)` — a re-run reports
   `0 new, 6236 updated` rather than duplicating, so adding another language later
   is safe. `--dry-run` parses and reports without writing.

4. **Loading does not imply approval (§180).** Verses load unapproved by default;
   `--approve` (or the existing `POST /api/v1/scripture/{id}/approve`) is the
   explicit governance act that makes them reachable live. §0 rule 8 is preserved:
   nothing is claimed before it is verified.

   `ScriptureRepository.upsert` deliberately does **not** set `approved` on the
   update branch, so an API re-upsert can never silently *un*-approve an
   already-governed verse. A new `approve_all(kind)` performs the bulk governance
   step, which is what `--approve` calls — without it a second `--approve` run
   would report success while approving nothing.

5. **§65 decoration is stripped symmetrically.** The rule detector marks Quran
   with ornate parentheses (﴾ ﴿), but Tanzil text has none. Left unhandled, a
   bracket-marked live line could only ever reach the fuzzy tier, so every real
   recitation would have fallen through to BLOCKED. A shared
   `strip_quran_decoration()` is now applied both when a verse is indexed (the
   loader and `POST /scripture`) and when a line is matched (§76). The versioned
   normalizer itself is untouched (§69 provenance: existing indices stay
   reproducible).

6. **Text is stored verbatim.** Tanzil's terms forbid altering the text, so the
   **Basmala that Tanzil prepends to ayah 1 of every surah except 1 and 9**
   (112 rows) is kept. Documented in `docs/DATABASE.md` with the operational
   consequence and the three options if bare-ayah recitation must match.

## Consequences

- 6236 Quran rows in `scripture_verses`, `en` + `it` on all of them, and §78
  verified end-to-end against PostgreSQL: a bracket-marked 2:255 flows through
  the real live WS pipeline and broadcasts `route=scripture_verified` with the
  approved Saheeh International and Piccardo text (score 1.000), never machine
  translation; ordinary speech still claims no reference (§0 rule 8).
- Accented Italian (`è`, `ù`) and `Qur'an`-style apostrophes round-trip intact;
  0 rows carry mojibake.
- Two editions only, so Hadith (§86) is still unseeded — the same loader shape
  applies but the source data does not exist yet.
- Approved translations are human-authored but were not re-reviewed for this
  project; the §173 human eval on live captions still applies.
- Test suite grew from 196 to 215 (17 parser tests + 2 decoration tests).

## Alternatives considered

- Direct `psql` restore into a `quran_text` table → rejected: the dumps are MySQL
  syntax and would not load, and a second scripture table would bypass §206's
  single store and split the §78 read path.
- Normalize/strip the Basmala during load → rejected: it modifies licensed text.
  Documented as a threshold/edition choice for the operator instead.
