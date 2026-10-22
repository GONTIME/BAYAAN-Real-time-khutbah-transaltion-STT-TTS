"""Parser tests for the Tanzil loader (§206).

The parser is the only thing standing between a third-party dump and the
scripture store, so the escapes and the key format are tested directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.load_quran_tanzil import Verse, _unescape, merge, parse_dump

from bayaan_server.pipeline.normalizer import TASHKEEL, TATWEEL, ArabicNormalizerV1

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dump(tmp_path, body: str) -> object:
    path = tmp_path / "dump.sql"
    path.write_text(body, encoding="utf-8")
    return parse_dump(path)


def test_parses_rows_and_builds_surah_ayah_keys(tmp_path) -> None:
    verses = _dump(
        tmp_path,
        "CREATE TABLE `t` (...);\n"
        "INSERT INTO `t` (`index`, `sura`, `aya`, `text`) VALUES\n"
        "(1, 1, 1, 'bismillah'),\n"
        "(2, 1, 2, 'alhamdu lillahi');\n",
    )
    assert set(verses) == {"1:1", "1:2"}
    assert verses["1:2"].sura == 1
    assert verses["1:2"].aya == 2
    assert verses["1:2"].text == "alhamdu lillahi"


def test_unescapes_backslash_apostrophe(tmp_path) -> None:
    """en.sahih contains 363 occurrences of \\' - they must become one quote."""
    verses = _dump(
        tmp_path,
        "(104, 2, 97, 'the Qur\\'an down upon your heart');\n",
    )
    assert verses["2:97"].text == "the Qur'an down upon your heart"


def test_unescapes_doubled_quote(tmp_path) -> None:
    assert _unescape("It''s here") == "It's here"


def test_unescape_does_not_corrupt_backslash_pairs() -> None:
    """\\\\' is a literal backslash followed by a string-ending quote."""
    assert _unescape("back\\\\") == "back\\"
    assert _unescape("a\\\\'") == "a\\'"


def test_unescapes_newline_and_tab_escapes() -> None:
    assert _unescape("a\\nb\\tc") == "a\nb\tc"
    assert _unescape("unknown\\x") == "unknownx"  # MySQL drops unknown escapes


def test_preserves_tashkeel_verbatim(tmp_path) -> None:
    """Tanzil terms forbid altering the text: no diacritics may be stripped."""
    # \u0645\u0650\u0646\u064e = m.n with kasra + fatha (tashkeel present).
    raw = "\u0645\u0650\u0646\u064e \u0627\u0644\u0652\u062c\u0650\u0646\u064e\u0651\u064e\u0629\u0650"
    verses = _dump(tmp_path, f"(6236, 114, 6, '{raw}');\n")
    assert verses["114:6"].text == raw
    assert TASHKEEL.search(verses["114:6"].text)


def test_rejects_conflicting_duplicate_key(tmp_path) -> None:
    with pytest.raises(ValueError, match="conflicting text"):
        _dump(tmp_path, "(1, 2, 255, 'one'),\n(2, 2, 255, 'two');\n")


def test_rejects_unparsable_row(tmp_path) -> None:
    """A row with a non-numeric sura is rejected rather than silently skipped."""
    with pytest.raises(ValueError, match="did not parse"):
        _dump(tmp_path, "(1, X, 1, 'bad sura');\n")


def test_rejects_file_without_verses(tmp_path) -> None:
    with pytest.raises(ValueError, match="no verses"):
        _dump(tmp_path, "-- only comments\n")


def test_merge_keeps_arabic_authoritative_and_attaches_languages() -> None:
    arabic = {
        "1:1": Verse(1, 1, "\u0627\u0644\u0639\u0631\u0628\u064a\u0629"),
        "1:2": Verse(1, 2, "\u0646\u0635 \u0622\u062e\u0631"),
    }
    en = {"1:1": Verse(1, 1, "In the name of Allah"), "1:2": Verse(1, 2, "  ")}
    it = {"1:1": Verse(1, 1, "Nel nome di Allah")}

    rows = merge(arabic, {"en": en, "it": it})

    assert rows[0]["translations"] == {
        "en": "In the name of Allah",
        "it": "Nel nome di Allah",
    }
    # A blank translation is dropped rather than stored as a caption.
    assert rows[1]["translations"] == {}
    assert rows[1]["arabic"] == "\u0646\u0635 \u0622\u062e\u0631"
    assert rows[1]["surah"] == "1" and rows[1]["ayah"] == "2"


@pytest.mark.parametrize(
    ("filename", "count"),
    [("quran-simple.sql", 6236), ("en.sahih.sql", 6236), ("it.piccardo.sql", 6236)],
)
def test_real_dumps_parse_completely(filename: str, count: int) -> None:
    """The shipped dumps must parse in full - 114 surahs, 6236 ayat."""
    verses = parse_dump(REPO_ROOT / filename)
    assert len(verses) == count
    assert max(int(k.split(":")[0]) for k in verses) == 114
    assert "2:255" in verses and "114:6" in verses


def test_all_three_editions_agree_on_every_key() -> None:
    """en/it must describe the same ayat as the Arabic text (§78 keys align)."""
    arabic = parse_dump(REPO_ROOT / "quran-simple.sql")
    en = parse_dump(REPO_ROOT / "en.sahih.sql")
    it = parse_dump(REPO_ROOT / "it.piccardo.sql")
    assert set(en) == set(arabic)
    assert set(it) == set(arabic)


def test_ayat_al_kursi_translations_are_the_expected_ones() -> None:
    """Spot-check a well-known ayah so a silent mis-join would fail here."""
    normalizer = ArabicNormalizerV1()
    arabic = parse_dump(REPO_ROOT / "quran-simple.sql")["2:255"].text
    en = parse_dump(REPO_ROOT / "en.sahih.sql")["2:255"].text
    it = parse_dump(REPO_ROOT / "it.piccardo.sql")["2:255"].text

    # Arabic literals use \u escapes: a cp1252 console must never be able to
    # corrupt this assertion.
    assert normalizer.normalize(arabic).startswith(
        "\u0627\u0644\u0644\u0647 \u0644\u0627 \u0627\u0644\u0627\u0647"
    )
    assert en.startswith("Allah - there is no deity except Him")
    assert it.startswith("Allah! Non c'è altro dio che Lui")
    # Same ayah, all three editions: no cross-surah mis-join.
    assert len(arabic) > 200 and len(en) > 200 and len(it) > 200


def test_normalized_index_matches_the_live_normalizer() -> None:
    """§78 matches on normalized_arabic: the stored index must be the exact
    output of the live pipeline's normalizer, dagger alif and all."""
    normalizer = ArabicNormalizerV1()
    for key in ("1:2", "2:255", "94:6", "114:6"):
        verse = parse_dump(REPO_ROOT / "quran-simple.sql")[key]
        normalized = normalizer.normalize(verse.text)
        # Diacritics/tatweel are stripped for matching...
        assert not TASHKEEL.search(normalized) and not TATWEEL.search(normalized)
        # ...but no letter is lost: the dagger alif in \u0625\u0650\u0644\u064e\u0640\u0670\u0647\u064e
        # becomes a plain alif, so the word keeps four letters.
        assert normalizer.normalize(normalized) == normalized  # idempotent

    kursi = normalizer.normalize(parse_dump(REPO_ROOT / "quran-simple.sql")["2:255"].text)
    # The dagger alif in the original became a plain alef, so the word keeps
    # alef-lam-alef-heh (no letter was lost).
    assert kursi.startswith("\u0627\u0644\u0644\u0647 \u0644\u0627 \u0627\u0644\u0627\u0647")
    assert "\u0627\u0644\u0627\u0647" in kursi
    # Al-Fatiha 1:2 is a stable, well-known normalized string.
    assert normalizer.normalize(
        parse_dump(REPO_ROOT / "quran-simple.sql")["1:2"].text
    ) == (
        "\u0627\u0644\u062d\u0645\u062f \u0644\u0644\u0647 \u0631\u0628 "
        "\u0627\u0644\u0639\u0627\u0644\u0645\u064a\u0646"
    )


def test_escaped_apostrophes_survive_the_real_english_dump() -> None:
    """en.sahih 2:97 has Qur\\'an - the backslash must not survive."""
    assert "Qur'an down upon your heart" in parse_dump(REPO_ROOT / "en.sahih.sql")[
        "2:97"
    ].text
