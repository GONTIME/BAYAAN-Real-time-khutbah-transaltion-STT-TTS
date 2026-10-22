"""Seed ``scripture_verses`` from Tanzil.net Quran dumps (§42.1/§42.2/§206).

§206: Quran is never machine translated in the live path - it is retrieved from
an approved store. This loader turns the three Tanzil SQL dumps into exactly
those rows, one per ayah, with the approved English/Italian translations attached:

    quran-simple.sql  -> arabic (+ normalized_arabic via ArabicNormalizerV1)
    en.sahih.sql      -> translations["en"]
    it.piccardo.sql   -> translations["it"]

Design notes:

* **Repo-only writes (§187).** Rows go through ``ScriptureRepository.upsert``,
  the same code path the API uses, so no second writer exists.
* **Idempotent.** The upsert keys on (kind, key), so re-running updates in place
  instead of duplicating - safe to re-run after adding another translation.
* **Text is verbatim.** Tanzil's terms forbid changing the text, so the Basmala
  that Tanzil prepends to ayah 1 of each surah (except surah 1) is preserved
  as-is. See the matching caveat in docs/DATABASE.md.
* **Approval is explicit (§180).** Loading alone does not make verses reachable
  live; pass ``--approve`` for that operator decision (it is audit-logged).

Usage (from backend/)::

    python -m tools.load_quran_tanzil --root .. --approve
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bayaan_server.config import DatabaseSettings, Settings
from bayaan_server.db.engine import create_engine, init_db
from bayaan_server.db.orm import ScriptureVerseRow
from bayaan_server.db.repository import AuditLogRepository, ScriptureRepository
from bayaan_server.domain.enums import AuditAction
from bayaan_server.pipeline.normalizer import ArabicNormalizerV1
from bayaan_server.sermon.scripture import strip_quran_decoration

# One ayah per line: (index, sura, aya, 'text'),
_ROW = re.compile(r"^\((\d+),\s*(\d+),\s*(\d+),\s*'(.*)'\),?;?\s*$")
# MySQL backslash escapes and doubled-quote escapes, resolved in ONE pass so
# that \\' cannot be mistaken for an escaped quote.
_ESCAPE = re.compile(r"\\(.)|''")

_MAP = {"n": "\n", "r": "\r", "t": "\t", "0": "\0"}

ARABIC_FILE = "quran-simple.sql"
EDITIONS = {
    "en": ("en.sahih.sql", "Saheeh International"),
    "it": ("it.piccardo.sql", "Hamza Roberto Piccardo"),
}
EDITION = "quran-simple-1.1 + en.sahih + it.piccardo"
LICENSE = (
    "Quran text: Tanzil Quran Text 1.1, CC BY 3.0 (tanzil.net); "
    "en: Saheeh International; it: H. R. Piccardo - both via Tanzil.net."
)


def _unescape(raw: str) -> str:
    """Resolve SQL string escapes without corrupting backslashes."""

    def repl(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return _MAP.get(match.group(1), match.group(1))
        return "'"

    return _ESCAPE.sub(repl, raw)


@dataclass(frozen=True)
class Verse:
    sura: int
    aya: int
    text: str

    @property
    def key(self) -> str:
        """§42.2 stable source id, e.g. "2:255" (matches eval/golden.py)."""
        return f"{self.sura}:{self.aya}"


def parse_dump(path: Path) -> dict[str, Verse]:
    """Parse a Tanzil phpMyAdmin dump into {key: Verse}."""
    verses: dict[str, Verse] = {}
    unparsed: list[int] = []
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line.startswith("("):
                continue
            match = _ROW.match(line)
            if match is None:
                unparsed.append(lineno)
                continue
            _index, sura, aya, raw = match.groups()
            verse = Verse(sura=int(sura), aya=int(aya), text=_unescape(raw))
            previous = verses.get(verse.key)
            if previous is not None and previous.text != verse.text:
                raise ValueError(
                    f"{path.name}:{lineno}: conflicting text for {verse.key}"
                )
            verses[verse.key] = verse
    if unparsed:
        raise ValueError(
            f"{path.name}: {len(unparsed)} row(s) did not parse, "
            f"first at line {unparsed[0]}"
        )
    if not verses:
        raise ValueError(f"{path.name}: no verses found")
    return verses


def merge(
    arabic: dict[str, Verse], translations: dict[str, dict[str, Verse]]
) -> list[dict[str, object]]:
    """Join the Arabic text with each translation on (sura, aya)."""
    for lang, rows in translations.items():
        absent = set(arabic) - set(rows)
        if absent:
            print(
                f"  ! {lang}: {len(absent)} ayah(s) missing from that edition "
                f"(kept without that language), e.g. {sorted(absent)[:3]}",
                file=sys.stderr,
            )
    merged: list[dict[str, object]] = []
    for key, verse in arabic.items():
        merged.append(
            {
                "key": key,
                "arabic": verse.text,
                "surah": str(verse.sura),
                "ayah": str(verse.aya),
                "translations": {
                    lang: rows[key].text
                    for lang, rows in translations.items()
                    if key in rows and rows[key].text.strip()
                },
            }
        )
    return merged


async def load(
    *,
    database_url: str,
    root: Path,
    approve: bool,
    batch_size: int,
) -> dict[str, int]:
    """Parse the dumps and upsert every ayah. Idempotent by (kind, key)."""
    normalizer = ArabicNormalizerV1()
    print(f"parsing dumps in {root} ...")
    arabic = parse_dump(root / ARABIC_FILE)
    translations = {
        lang: parse_dump(root / filename) for lang, (filename, _who) in EDITIONS.items()
    }
    for lang, (filename, who) in EDITIONS.items():
        print(f"  {lang:<2} {filename:<18} {len(translations[lang]):>5} ayat  ({who})")
    print(f"  ar {ARABIC_FILE:<18} {len(arabic):>5} ayat")

    rows = merge(arabic, translations)
    # Stable order keeps re-runs byte-for-byte comparable.
    rows.sort(key=lambda r: (int(str(r["surah"])), int(str(r["ayah"]))))

    settings = Settings(database=DatabaseSettings(url=database_url))
    engine = create_engine(settings)
    await init_db(engine)

    created = updated = 0
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            repo = ScriptureRepository(db)
            for position, row in enumerate(rows, start=1):
                existing = await db.scalar(
                    select(ScriptureVerseRow.id).where(
                        ScriptureVerseRow.kind == "QURAN",
                        ScriptureVerseRow.key == row["key"],
                    )
                )
                await repo.upsert(
                    kind="QURAN",
                    key=str(row["key"]),
                    arabic=str(row["arabic"]),
                    # §78 matches on normalized text, so the stored index must
                    # come from the very normalizer the live path uses, with the
                    # same §65 decoration handling on both sides.
                    normalized_arabic=strip_quran_decoration(
                        normalizer.normalize(str(row["arabic"]))
                    ),
                    translations=row["translations"],  # type: ignore[arg-type]
                    surah=str(row["surah"]),
                    ayah=str(row["ayah"]),
                    edition=EDITION,
                    license=LICENSE,
                    approved=approve,
                )
                if existing is None:
                    created += 1
                else:
                    updated += 1
                if position % batch_size == 0:
                    await db.commit()
                    print(f"  {position}/{len(rows)} ...", flush=True)
            approved_now = 0
            if approve:
                # Approval is a separate governance act (§180): upsert must not
                # (un)approve, so a re-run needs this explicit pass.
                approved_now = await repo.approve_all("QURAN")
            await AuditLogRepository(db).record(
                AuditAction.GLOSSARY_UPDATED,  # §98 nearest governance event
                metadata={
                    "scripture": "QURAN",
                    "edition": EDITION,
                    "ayat": len(rows),
                    "created": created,
                    "updated": updated,
                    "approved": approve,
                    "approved_in_this_run": approved_now,
                    "source": "tanzil.net",
                },
            )
            await db.commit()
    finally:
        await engine.dispose()

    return {
        "ayat": len(rows),
        "created": created,
        "updated": updated,
        "approved": approved_now,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed scripture_verses (§206).")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="folder holding the .sql dumps (default: repo root)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="override the configured database URL",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="mark verses approved so they are reachable live (§78/§180). "
        "Off by default: approval is an explicit operator decision.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and report only; never touches the database",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    for name in (ARABIC_FILE, *(f for f, _ in EDITIONS.values())):
        if not (args.root / name).is_file():
            parser.error(f"missing {args.root / name}")

    if args.dry_run:
        arabic = parse_dump(args.root / ARABIC_FILE)
        translations = {
            lang: parse_dump(args.root / filename)
            for lang, (filename, _who) in EDITIONS.items()
        }
        rows = merge(arabic, translations)
        both = sum(1 for r in rows if len(r["translations"]) == 2)  # type: ignore[arg-type]
        print(f"\ndry run: {len(rows)} ayat, {both} with both en+it (no writes)")
        return 0

    url = args.database_url or Settings().database.url
    print(f"database: {url}")
    print(f"approve : {args.approve}")
    result = asyncio.run(
        load(
            database_url=url,
            root=args.root,
            approve=args.approve,
            batch_size=args.batch_size,
        )
    )
    print(
        f"\ndone: {result['ayat']} ayat "
        f"({result['created']} new, {result['updated']} updated); "
        f"approved={args.approve}"
        + (f" ({result['approved']} newly approved)" if args.approve else "")
    )
    if not args.approve:
        print(
            "note: verses are NOT reachable live yet - approve with "
            "POST /api/v1/scripture/{id}/approve or re-run with --approve."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
