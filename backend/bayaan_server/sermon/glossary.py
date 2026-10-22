"""Glossary service (BAYAAN_MASTER_SPEC.md §22, §180).

Only APPROVED terms influence preparation/validation (§180 governance:
glossary approval = reviewer + mosque admin). Matching is on normalized
Arabic token boundaries, longest-first, so multi-word terms win.
"""

from __future__ import annotations

from bayaan_server.domain.sermon import GlossaryTerm


def glossary_version(revision: int) -> str:
    """Provenance stamp §69: the glossary version travels with every draft."""
    return f"g{revision}"


def _present(arabic_source: str, needle: str) -> bool:
    """Substring membership on NORMALIZED Arabic.

    Arabic clitics (the definite article ال, conjunctions و/ف, prepositions
    ب/ل/ك) attach directly to the word with no space, so strict word-boundary
    matching would miss the very common "الصلاة" for the term "صلاة". We
    therefore match on substring after normalization (tashkeel/tatweel
    stripped, alef variants unified), favoring recall over precision.

    Documented MVP limitation: an embedded substring of an unrelated word
    (e.g. a term that is part of "مصلاة") yields a false hit. A light Arabic
    stemmer/segmenter is a Phase 4+ improvement, out of scope here.
    """
    if not needle:
        return False
    return needle in arabic_source


def glossary_hits(arabic_source: str, terms: list[GlossaryTerm]) -> list[GlossaryTerm]:
    """Approved glossary terms present in the Arabic SOURCE segment.

    §96 review panel shows "glossary hits" against the Arabic source -
    the terms are Arabic, so this must never run against a translated draft.
    Longest-first so multi-word terms are reported before their substrings.
    """
    hits: list[GlossaryTerm] = []
    for term in sorted(terms, key=lambda t: len(t.normalized_ar), reverse=True):
        if not term.approved:
            continue  # unapproved terms never influence output (§180)
        if _present(arabic_source, term.normalized_ar):
            hits.append(term)
    return hits


def glossary_violations(
    arabic_source: str, draft_text: str, language: str, terms: list[GlossaryTerm]
) -> list[str]:
    """§22/§150 glossary_violation: an approved term appears in the Arabic
    source but its approved target-language translation is absent from the
    draft. Unapproved terms and terms without a translation for this
    language are ignored (nothing to enforce)."""
    violations: list[str] = []
    for term in glossary_hits(arabic_source, terms):
        expected = term.translations.get(language)
        if not expected:
            continue  # no approved translation to enforce for this language
        if expected not in draft_text:
            violations.append(
                f"[{language}] '{term.term_ar}' should be '{expected}'"
            )
    return violations
