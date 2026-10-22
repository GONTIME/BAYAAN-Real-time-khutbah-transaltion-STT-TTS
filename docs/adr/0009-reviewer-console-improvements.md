# ADR-0009 — Reviewer console improvements (idempotent drafts, approve-all, delete, real MT, real DB)

- **Status:** Accepted
- **Date:** 2026-10-12
- **Context:** Operator feedback on the reviewer console (`/admin`): clicking
  **Generate drafts** produced the same text again (the only translation
  provider was the `mock` echo), and clicking it again duplicated drafts with no
  recognition that they already existed. Requested: real translations, a real
  database that can be browsed/downloaded, delete-after-upload, and an
  "approve all" shortcut.

## Decisions

1. **Idempotent draft generation (§75, §169, §41).** `generate_drafts` now skips
   any `(segment, language)` pair that already has a draft and returns
   `{created, skipped, total}`. A DB-level `UniqueConstraint(segment_id,
   language)` backstops it. A PUBLISHED version is still refused (409) up front,
   so §41 immutability is preserved. Human corrections/approvals are never
   silently overwritten (§169).

2. **Real, provider-neutral MT adapter (§63, §69, §186).** New
   `providers/libre.py` `LibreTranslateProvider` implements the existing
   `TranslationProvider` port against a self-hostable LibreTranslate endpoint
   (offline in a mosque, no vendor lock-in). Registered as provider name `libre`
   with config `BAYAAN_PROVIDERS__*`; the `mock` provider stays the default and
   is wired as a fail-safe fallback (§87). Full §69 provenance travels on every
   result; a failed call raises the typed `ProviderOutageError` (§0 rule 8) and
   never fabricates a translation.

3. **Approve-all (§96).** New `POST /sermons/versions/{vid}/approve-all` approves
   every draft of a version in one audited pass; publish still requires all
   drafts approved (§119). The console gains an **Approve all drafts** button.

4. **Delete after upload (§119 DELETED).** New `DELETE /sermons/{id}` cascades
   drafts → segments → versions → sermon. A sermon with any PUBLISHED version is
   refused (409, §41). Audited as `SERMON_DELETED` (§98). The console gains a
   per-sermon **✕** delete (disabled for published).

5. **Real database wiring (ADR-0001).** No code change — the app is already
   engine-agnostic. Added `docs/DATABASE.md`, `backend/.env.example`, and
   `docker-compose.yml` (PostgreSQL + LibreTranslate) so an operator can browse
   (pgAdmin/DBeaver/psql), export (`pg_dump`/`COPY`), and restore the site's
   `.sql`. SQL stays the chosen interchange format (XML is an export, not a DB,
   and is rejected by the `txt/md/docx/pdf` uploader — §157/§158).

## Consequences

- Behaviour beyond the master spec is gated by §41/§98 exactly as the rest of
  the lifecycle; nothing unsafe is auto-published or auto-spoken.
- Real translation quality still needs the §173 human eval; the adapter only
  removes the mock-echo blocker.
- Schema gained one unique constraint; existing SQLite dev DBs should be deleted
  and recreated (`create_all`), or migrated with Alembic for production.

## Alternatives considered

- Keep mock translation and document only → rejected: the core complaint was
  "no real translation".
- Delete as soft-`DELETED` status → rejected for now; hard delete with a §41
  published-guard matches the request and keeps §41 guarantees. A retention
  policy (soft delete/archive) can be layered later per §119.

