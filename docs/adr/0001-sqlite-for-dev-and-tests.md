# ADR-0001: SQLite fallback for local development and tests

**Status:** Accepted (deviation to be revisited before production)

**Context:** BAYAAN_MASTER_SPEC.md §117 mandates PostgreSQL as the metadata +
approved-text store. The current development environment has no Docker and no
PostgreSQL instance available.

**Decision:** SQLAlchemy async models are written to be engine-agnostic.
Configuration defaults to a local SQLite (aiosqlite) database for development
and tests via `BAYAAN_DATABASE__URL`. Production configuration MUST point to
PostgreSQL (`postgresql+asyncpg://...`) before any real deployment.

**Consequences:**
- Phase 0 tests run without external services.
- `Uuid`, `String`, `DateTime` column types used are portable.
- Production parity (migrations via Alembic, backup/restore per §108) is a
  pre-production release gate (§198).
