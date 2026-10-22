"""Text normalization (BAYAAN_MASTER_SPEC.md §48 "Text Normalization").

Versioned because §69 provenance requires reproducible results. V1 performs
only lossless-for-matching Arabic orthography cleanup:
- strips tashkeel (diacritics) and tatweel,
- unifies alef variants,
- collapses whitespace.

It deliberately does NOT touch words that could change meaning; a more
aggressive normalizer would need a new version and review.
"""

from __future__ import annotations

import re

from typing import Protocol

TASHKEEL = re.compile(r"[\u064B-\u0652]")
TATWEEL = re.compile("\u0640")
# Dagger alif (ٰ) IS an alif: convert it, never delete it (deleting would
# lose a letter of the word, e.g. العالمين -> علمين).
DAGGER_ALIF = re.compile("\u0670")
ALEF_VARIANTS = re.compile("[\u0623\u0625\u0622\u0671]")
MULTI_SPACE = re.compile(r"\s+")


class Normalizer(Protocol):
    version: str

    def normalize(self, text: str) -> str: ...


class ArabicNormalizerV1:
    version = "ar_norm_v1"

    def normalize(self, text: str) -> str:
        text = DAGGER_ALIF.sub("\u0627", text)
        text = TASHKEEL.sub("", text)
        text = TATWEEL.sub("", text)
        text = ALEF_VARIANTS.sub("\u0627", text)
        text = MULTI_SPACE.sub(" ", text)
        return text.strip()