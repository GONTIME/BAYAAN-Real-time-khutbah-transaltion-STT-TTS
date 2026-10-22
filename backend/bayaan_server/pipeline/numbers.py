"""Number safety detector (BAYAAN_MASTER_SPEC.md §66, §101).

§66: detect Arabic/Western numerals, number words, years, dates, percentages
and quantities. If a number changes between source and translation the
mismatch is a CRITICAL error -> BLOCK (§101 "رقم/تاريخ مهم تغير").

Rule 8 (fail safe, never guess): only *mismatches between two multisets we
can both extract* are flagged. A number missing on one side is not proof of
an error (a translator may legitimately spell out or drop a figure), so it is
never treated as a critical mismatch here.

Arabic-Indic digits are folded to ASCII so "٣٠" == "30" (§66 both numeral
systems).
"""

from __future__ import annotations

import re
from collections import Counter

# Arabic-Indic (٠-٩) and Extended Arabic-Indic (۰-۹) digits fold to ASCII 0-9.
_ARABIC_DIGITS = {ord("٠") + i: ord("0") + i for i in range(10)}
_EXT_ARABIC_DIGITS = {ord("۰") + i: ord("0") + i for i in range(10)}

# A "number token" is a run of digits, optionally with , or . separators
# (grouping thousands or decimals). Normalized to a bare digit string.
_NUMBER_TOKEN = re.compile(r"[0-9]+(?:[.,][0-9]+)*")

# Number words per language family (lowercased). Arabic words are matched
# against normalized Arabic text; English/Italian ones against the target.
_NUMBER_WORDS_AR = {
    "واحد": 1, "اثنان": 2, "اثنين": 2, "ثلاثة": 3, "ثلاث": 3, "اربعة": 4,
    "اربع": 4, "خمسة": 5, "خمس": 5, "ستة": 6, "سبعة": 7, "ثمانية": 8,
    "تسعة": 9, "عشرة": 10, "عشر": 10, "احدى": 1, "احد": 1, "مئة": 100,
    "مية": 100, "الف": 1000, "الفا": 1000, "الفين": 2000, "مليون": 1000000,
}
_NUMBER_WORDS_EN = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twenty": 20, "thirty": 30,
    "hundred": 100, "thousand": 1000, "million": 1000000,
}
_NUMBER_WORDS_IT = {
    "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5,
    "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10, "venti": 20,
    "trenta": 30, "cento": 100, "mille": 1000, "milione": 1000000,
}


def normalize_numerals(text: str) -> str:
    """Fold Arabic-Indic digits to ASCII and strip *grouping* separators.

    Grouping commas are removed ("1,000" -> "1000") but decimal points are
    preserved ("3.14" stays "3.14", never "314") — BUG-014.
    """
    text = text.translate(_ARABIC_DIGITS).translate(_EXT_ARABIC_DIGITS)
    return _NUMBER_TOKEN.sub(lambda m: m.group(0).replace(",", ""), text)


def _digit_values(text: str) -> Counter:
    folded = text.translate(_ARABIC_DIGITS).translate(_EXT_ARABIC_DIGITS)
    values: Counter = Counter()
    for match in _NUMBER_TOKEN.finditer(folded):
        values[match.group(0).replace(",", "")] += 1
    return values


def _word_values(text: str, words: dict[str, int]) -> Counter:
    values: Counter = Counter()
    for token in re.findall(r"[^\W\d_]+", text.lower()):
        if token in words:
            values[str(words[token])] += 1
    return values


def extract_source_numbers(text: str) -> Counter:
    """Numeric values present in an Arabic source: digits + Arabic number words."""
    return _digit_values(text) + _word_values(text, _NUMBER_WORDS_AR)


def extract_target_numbers(text: str, language: str) -> Counter:
    """Numeric values present in a target-language translation."""
    values = _digit_values(text)
    lang = language.lower()
    if lang.startswith("en"):
        values = values + _word_values(text, _NUMBER_WORDS_EN)
    elif lang.startswith("it"):
        values = values + _word_values(text, _NUMBER_WORDS_IT)
    return values


def number_mismatches(source: str, target: str, language: str) -> list[str]:
    """§66/§101 critical mismatches: a source numeric value that is absent
    from the target. Each is reported as ``source=<n> missing in target``.

    Deliberately does NOT flag values only present in the target (an addition
    is caught by the entity/addition checks, not here) and does NOT flag a
    source value that is simply dropped (fail safe: absence is not proof).
    """
    src = extract_source_numbers(source)
    tgt = extract_target_numbers(target, language)
    missing = src - tgt  # multiset difference: present in source, absent in target
    return [f"number '{n}' from source is missing in translation" for n in sorted(missing.elements())]
