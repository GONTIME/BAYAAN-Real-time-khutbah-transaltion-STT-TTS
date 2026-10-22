"""Rule-based content detection (BAYAAN_MASTER_SPEC.md §65 step 1 of 5).

The full classification pipeline is:
    rule detector -> lexical detector -> reference matcher -> ML -> policy

This module implements ONLY the conservative rule detector (Phase 1).
It defaults to GENERAL and never claims QURAN/HADITH without explicit
surface markers; stricter religious paths (retrieval, §193) arrive in
Phase 4, at which point this detector remains the cheapest first stage.
"""

from __future__ import annotations

import re

from bayaan_server.domain.enums import ContentType
from .normalizer import ArabicNormalizerV1, Normalizer

_QURAN_MARKERS = re.compile("[\uFD3F\uFD3E]")  # ornate parentheses ﴾ ﴿
_HADITH_MARKERS = re.compile(
    r"قال رسول الله|قال النبي|قال النبي صلى الله عليه وسلم|"
    r"قال \(صلى الله عليه وسلم\)"
)
_DUA_MARKERS = re.compile(r"اللهم")


class RuleContentDetector:
    def __init__(self, normalizer: Normalizer | None = None) -> None:
        self._normalizer = normalizer or ArabicNormalizerV1()

    def detect(self, text: str) -> ContentType:
        normalized = self._normalizer.normalize(text)
        if _QURAN_MARKERS.search(normalized):
            return ContentType.QURAN
        if _HADITH_MARKERS.search(normalized):
            return ContentType.HADITH
        if _DUA_MARKERS.search(normalized):
            return ContentType.DUA
        return ContentType.GENERAL