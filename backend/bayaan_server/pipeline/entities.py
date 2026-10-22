"""Entity extraction and preservation (BAYAAN_MASTER_SPEC.md §68, §101).

§68 entity kinds: PERSON, PLACE, BOOK, SURA, HADITH_COLLECTION, NUMBER, DATE.
The validator uses these to detect omission, hallucination and unexpected
mutation (§68). Extraction is deliberately conservative and surface-based:
it never invents an entity, it only recognizes surface patterns (§0 rule 8).

For Arabic, PERSON names are approximated by the common prophetic-honorifics
and a curated marker list (a full NER model is out of MVP scope). NUMBER and
DATE reuse the number detector (§66) so a single source of truth governs
numeric entities.
"""

from __future__ import annotations

import re
from collections import Counter

from .numbers import extract_source_numbers, extract_target_numbers

# Arabic honorifics that almost always introduce a person (§68 PERSON).
_AR_PERSON_MARKERS = re.compile(
    r"(?:محمد|أحمد|عيسى|موسى|إبراهيم|نوح|يوسف|يعقوب|داود|سليمان|"
    r"أبو بكر|عمر|عثمان|علي|فاطمة|عائشة|خديجة|الرسول|النبي|الإمام)"
)

# Arabic month names (a DATE signal). Hijri + Gregorian Arabic spellings.
_AR_MONTHS = re.compile(
    r"(?:يناير|فبراير|مارس|أبريل|مايو|يونيو|يوليو|أغسطس|سبتمبر|"
    r"أكتوبر|نوفمبر|ديسمبر|محرّم|محرم|صفر|ربيع|جمادى|رجب|شعبان|"
    r"رمضان|شوال|ذو القعدة|ذو الحجة)"
)

# Surah marker (﴿ or "سورة") + hadith collection names (§68).
_AR_SURA = re.compile(r"(?:سورة\s+\S+|[\uFD3F\uFD3E])")
_AR_HADITH_COLLECTION = re.compile(
    r"(?:صحيح البخاري|صحيح مسلم|سنن أبي داود|سنن الترمذي|سنن النسائي|"
    r"سنن ابن ماجه|موطأ مالك|مسند أحمد)"
)


def _latin_capitalized(text: str) -> Counter:
    # A crude PERSON/PLACE/BOOK heuristic for Latin targets: capitalized words
    # that are not sentence-initial-only are candidate proper nouns.
    return Counter(re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", text))


def extract_entities(arabic_source: str) -> dict[str, Counter]:
    """§68 entities found in the Arabic SOURCE (only what we can surface)."""
    entities: dict[str, Counter] = {
        "PERSON": Counter(),
        "SURA": Counter(),
        "HADITH_COLLECTION": Counter(),
        "DATE": Counter(),
        "NUMBER": Counter(),
    }
    entities["PERSON"].update(_AR_PERSON_MARKERS.findall(arabic_source))
    entities["SURA"].update(_AR_SURA.findall(arabic_source))
    entities["HADITH_COLLECTION"].update(_AR_HADITH_COLLECTION.findall(arabic_source))
    entities["DATE"].update(_AR_MONTHS.findall(arabic_source))
    entities["NUMBER"].update(extract_source_numbers(arabic_source))
    return entities


def extract_target_entities(target: str, language: str) -> dict[str, Counter]:
    """§68 entities found in the TARGET (Latin proper nouns + numbers)."""
    entities: dict[str, Counter] = {
        "PERSON": Counter(),
        "SURA": Counter(),
        "HADITH_COLLECTION": Counter(),
        "DATE": Counter(),
        "NUMBER": extract_target_numbers(target, language),
    }
    if language.lower().startswith(("en", "it")):
        # Latin proper nouns: a rough proxy for the transliterated Arabic names.
        entities["PERSON"].update(_latin_capitalized(target))
    return entities


def entity_preservation_violations(
    arabic_source: str, target: str, language: str
) -> list[str]:
    """§68: only NUMBER entities are strictly enforced (they map to the §66
    critical rule and to transliteration we cannot verify). PERSON/PLACE are
    transliterated differently across languages, so their absence in a naive
    Latin scan is NOT treated as omission (fail safe, §0 rule 8)."""
    violations: list[str] = []
    src_numbers = extract_source_numbers(arabic_source)
    tgt_numbers = extract_target_numbers(target, language)
    for value in sorted((src_numbers - tgt_numbers).elements()):
        violations.append(f"entity NUMBER '{value}' omitted in translation")
    return violations
