"""Curated target-language catalog (ADR-0017, §63).

The project ships a **curated** catalog instead of accepting any ISO code:
every entry here is a language the pipeline can be configured for, and each one
carries the audience-facing endonym used by the room page, the Telegram mirror
and the operator UIs. Adding a language is a one-line change *here*, so prompts,
labels and documentation can never drift apart (§185: no magic values).

Why curated and not "anything ISO-639-1" (§0 rule 8 - never guess):
  * a bare code with no label reaches worshippers as `ur` instead of `اردو`;
  * a code nobody vetted can silently become a target that has no provider
    support, no glossary, no voice - configuration that only fails at runtime.

The catalog is ordered the way the operator pickers should present it: the two
default targets first, then the rest of the supported set.
"""

from __future__ import annotations

from dataclasses import dataclass

# Source language of the khutbah. Kept in the catalog so a translation *to* the
# source language can never be configured by accident (§63: Arabic in, targets
# out).
SOURCE_LANGUAGE = "ar"


@dataclass(frozen=True)
class Language:
    """One supported language: machine code + audience-facing labels."""

    code: str
    endonym: str  # what the reader calls their own language
    english: str  # English name, for operator-facing docs/pickers


LANGUAGE_CATALOG: tuple[Language, ...] = (
    Language("ar", "العربية", "Arabic"),  # source language (§63)
    Language("it", "Italiano", "Italian"),
    Language("en", "English", "English"),
    Language("fr", "Français", "French"),
    Language("ur", "اردو", "Urdu"),
    Language("id", "Bahasa Indonesia", "Indonesian"),
    Language("bn", "বাংলা", "Bengali"),
    Language("fa", "فارسی", "Persian"),
    Language("ps", "پښتو", "Pashto"),
    Language("tr", "Türkçe", "Turkish"),
    Language("sw", "Kiswahili", "Swahili"),
    Language("de", "Deutsch", "German"),
    Language("es", "Español", "Spanish"),
)

#: Every code the server accepts anywhere (source + targets).
LANGUAGE_CODES: frozenset[str] = frozenset(entry.code for entry in LANGUAGE_CATALOG)

#: Codes that may be configured as *target* languages.
TARGET_LANGUAGE_CODES: frozenset[str] = LANGUAGE_CODES - {SOURCE_LANGUAGE}

_LABELS: dict[str, str] = {entry.code: entry.endonym for entry in LANGUAGE_CATALOG}
_ENGLISH: dict[str, str] = {entry.code: entry.english for entry in LANGUAGE_CATALOG}


def language_label(code: str) -> str:
    """Endonym for a code; an unknown code falls back to the code itself.

    Never invent a name for a code we do not know (§0 rule 8): the raw code is
    honest and still machine-parseable.
    """
    return _LABELS.get(code, code)


def language_english_name(code: str) -> str:
    """English name for a code (operator pickers/docs); falls back to the code."""
    return _ENGLISH.get(code, code)


def is_target_language(code: str) -> bool:
    return code in TARGET_LANGUAGE_CODES


def is_language(code: str) -> bool:
    return code in LANGUAGE_CODES


def supported_codes_csv() -> str:
    """``"it, en, fr, ..."`` - the pick-list used in error messages and docs."""
    return ", ".join(entry.code for entry in LANGUAGE_CATALOG)


def target_codes_csv() -> str:
    """Supported *target* codes, in catalog order (source excluded)."""
    return ", ".join(
        entry.code for entry in LANGUAGE_CATALOG if entry.code in TARGET_LANGUAGE_CODES
    )


def catalog_payload() -> list[dict[str, str]]:
    """Serializable catalog for the operator UIs (code + both labels)."""
    return [
        {"code": entry.code, "label": entry.endonym, "english": entry.english}
        for entry in LANGUAGE_CATALOG
    ]
