# Database — connect, browse, download (BAYAAN)

BAYAAN already talks to a **real relational database** through SQLAlchemy
async. By default it uses a local SQLite **file** for development
(`sqlite+aiosqlite:///./bayaan_dev.db`, see ADR-0001). For a mosque pilot you
point it at **PostgreSQL**. The models are engine-agnostic, so the switch is a
single environment variable — no code change.

## 1. SQL vs XML (the site's download formats)

Use **SQL**, not XML.

- **`.sql`** — a database dump. Restore it straight into PostgreSQL and BAYAAN
  can read/query it (see §4). This is the format you want.
- **`.xml`** — a data *export/interchange* file, not a database. BAYAAN's
  sermon uploader only accepts `txt/md/docx/pdf` (§158/§157); a `.xml` or `.sql`
  file is rejected with a loud `415`. XML is for moving data between tools, not
  for BAYAAN to store or query.

## 2. Point BAYAAN at PostgreSQL

```powershell
# 1) one-time: install the async Postgres driver
cd "D:\GENERAL AI\Trans_Sys\backend"
.\\.venv\\Scripts\\python.exe -m pip install -e ".[postgres]"

# 2) start a local Postgres (Docker) — or use any Postgres you already have
docker compose up -d db

# 3) set the connection URL for the session you run the server in
$env:BAYAAN_DATABASE__URL = "postgresql+asyncpg://bayaan:bayaan@localhost:5432/bayaan"

# 4) run as usual — tables are created on startup (init_db / create_all)
.\\.venv\\Scripts\\python.exe -m uvicorn bayaan_server.api.app:create_app --factory --reload
```

The URL format is `postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DBNAME`.
See `backend/.env.example` for a copy-paste template.

> Production note: `init_db` uses `create_all`, which is fine for dev/first
> boot. Before a real deployment, generate Alembic migrations for schema
> changes (the code marks this as a TODO in `db/engine.py`).

## 3. Browse the database

Any Postgres client works. Pick one:

| Tool | How |
|---|---|
| **pgAdmin 4** | GUI. Add a server: host `localhost`, port `5432`, user `bayaan`, password `bayaan`, db `bayaan`. |
| **DBeaver** (free) | New PostgreSQL connection with the same values; browse tables under `bayaan → Schemas → public → Tables`. |
| **psql** (CLI) | `docker compose exec db psql -U bayaan -d bayaan` then `\dt` to list tables. |

Key tables: `sermons`, `sermon_versions`, `sermon_segments`,
`translation_drafts`, `glossary_terms`, `scripture_verses`, `audit_log`.

### Quran scripture store (§206)

Quran text is never machine translated live (§206) - it is retrieved from
`scripture_verses`. Seed it from the Tanzil dumps in the repo root:

```powershell
cd backend
python -m tools.load_quran_tanzil --root .. --dry-run    # parse + report only
python -m tools.load_quran_tanzil --root ..              # load (unapproved)
python -m tools.load_quran_tanzil --root .. --approve    # load + make reachable live
```

| File | Lands in |
|---|---|
| `quran-simple.sql` | `arabic` + `normalized_arabic` (via `ArabicNormalizerV1`) |
| `en.sahih.sql` | `translations_json -> "en"` |
| `it.piccardo.sql` | `translations_json -> "it"` |

Result: **6236 rows**, `kind='QURAN'`, `key = "<surah>:<ayah>"` (e.g. `2:255`),
with `edition`/`license` recorded per row.

* **Idempotent** - keyed on `(kind, key)`, so re-running updates in place
  (`0 new, 6236 updated`). Safe to re-run after adding another translation.
* **Writes go through `ScriptureRepository.upsert`** (§187), the same code path
  the API uses, and the load is recorded in `audit_log` (§98).
* **Approval is separate (§180).** Without `--approve`, verses exist but are not
  reachable live; `GET /api/v1/scripture` still lists them, and
  `POST /api/v1/scripture/{id}/approve` approves individually. Approval is only
  ever additive — an API re-upsert never un-approves a governed verse.
* **Text is verbatim.** Tanzil's terms forbid altering the text.

#### Caveat: the prepended Basmala

Tanzil prepends the Basmala to **ayah 1 of every surah except 1 and 9** (112
rows), e.g. `2:1` is `"بِسْمِ اللَّهِ الرَّحْمَـٰنِ الرَّحِيمِ الم"`. The loader keeps
this verbatim (changing it would violate the license), which means a speaker who
recites ayah 1 *without* the Basmala will not exact-match and will land in the
§76 fuzzy tier instead. Options if that matters operationally:

* load an edition without the prefixed Basmala (e.g. `quran-simple-clean`), or
* raise `BAYAAN_SCRIPTURE__NEAR_MATCH_THRESHOLD` (default `0.85`) so the fuzzy
  tier accepts it, or
* keep it as-is: the safe default blocks an unverified Quran line rather than
  captioning it wrongly (§206).

Verify a load:

```powershell
docker compose exec db psql -U bayaan -d bayaan -c ^
  "select count(*), count(*) filter (where approved) from scripture_verses where kind='QURAN';"
```

Expected: `6236 | 6236`.

> **Note on which database:** the loader uses the configured
> `BAYAAN_DATABASE__URL` and falls back to the SQLite dev file. Pass
> `--database-url` (or export the variable) when you mean the Docker Postgres —
> loading twice into two different stores is easy to miss otherwise.

## 4. Download / export

```bash
# Full database dump (SQL) — portable, restore-able
docker compose exec db pg_dump -U bayaan -d bayaan --format=custom -f /tmp/bayaan.dump
docker compose exec db pg_dump -U bayaan -d bayaan > bayaan.sql   # plain SQL

# One table as CSV (e.g. all approved translations)
docker compose exec db psql -U bayaan -d bayaan -c \
  "COPY (SELECT * FROM translation_drafts) TO STDOUT WITH CSV HEADER" > drafts.csv
```

Copy the file out of the container if needed:
`docker compose cp db:/tmp/bayaan.dump ./bayaan.dump`.

## 5. Restore the site's `.sql` into Postgres

```bash
# plain SQL dump
docker compose exec -T db psql -U bayaan -d bayaan < your_download.sql

# custom-format dump
docker compose exec -T db pg_restore -U bayaan -d bayaan --clean --if-exists your_download.dump
```

Then set `BAYAAN_DATABASE__URL` (§2) and start the server — BAYAAN reads the
same tables it would have written itself.

## 6. Backups

Schedule `pg_dump` (above) on a timer, or snapshot the `bayaan_pgdata` Docker
volume. Keep dumps off the mosque PC's user folders and encrypted (see
PRIVACY.md).

## 7. Dev fallback (no Docker)

Leave `BAYAAN_DATABASE__URL` unset to keep using the SQLite file
(`backend/bayaan_dev.db`). It is a real database file you can open with
**DB Browser for SQLite** to browse/export, but it is single-writer — use
PostgreSQL for anything beyond local testing.
