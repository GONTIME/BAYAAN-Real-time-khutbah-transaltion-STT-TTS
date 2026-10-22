"""Negation safety detector (BAYAAN_MASTER_SPEC.md §67, §101).

§67: detect negation particles and compare against the target-language
equivalents. A polarity flip is a CRITICAL error -> BLOCK (§101 "تغيير النفي").

§67 is explicit that this is a *safety signal, not proof of translation
quality*: we only fire on a clear source-negative / target-positive mismatch
(affirmative in the target with no negation marker), never on the ambiguous
target-negative / source-positive case. Fail safe: on doubt we stay quiet and
let the higher-level validator decide.
"""

from __future__ import annotations

import re

# Arabic negation particles (matched on normalized Arabic). Taskheel-free.
_AR_NEGATION = re.compile(r"\b(?:لا|لم|لن|ليس|ليست|ما|إلا|غير|بدون|دون)\b")

# Target-language negation markers (lowercased, word-boundary matched).
_EN_NEGATION = re.compile(
    r"\b(?:not|no|never|cannot|can't|won't|isn't|aren't|don't|doesn't|didn't|"
    r"without|neither|nor|none|nothing)\b"
)
_IT_NEGATION = re.compile(
    r"\b(?:non|no|mai|senza|niente|nulla|nessuno|né)\b"
)

_NEGATION_BY_LANG = {"en": _EN_NEGATION, "it": _IT_NEGATION}


def source_is_negated(arabic_source: str) -> bool:
    return bool(_AR_NEGATION.search(arabic_source))


def target_is_negated(target: str, language: str) -> bool:
    marker = _NEGATION_BY_LANG.get(language.lower()[:2])
    if marker is None:
        return False
    return bool(marker.search(target.lower()))


def negation_mismatch(arabic_source: str, target: str, language: str) -> list[str]:
    """§67: only a source-negative -> target-affirmative flip is reported.

    The reverse (source affirmative, target negative) is intentionally NOT
    reported: negation can legitimately be added for target grammar, so
    treating it as critical would produce false BLOCKs (§101 critical is
    reserved for meaning-changing flips).
    """
    if source_is_negated(arabic_source) and not target_is_negated(target, language):
        return ["negation present in source is absent in translation"]
    return []
