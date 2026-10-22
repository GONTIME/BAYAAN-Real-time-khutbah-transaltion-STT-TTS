"""Glossary unit tests (§22, §150, §180).

Glossary terms are Arabic; hits are computed against the Arabic source
segment and a violation is an approved source term whose approved target
translation is missing from the draft. Unapproved terms never influence
output (§180).
"""

from __future__ import annotations

from bayaan_server.domain.sermon import GlossaryTerm
from bayaan_server.sermon.glossary import (
    glossary_hits,
    glossary_version,
    glossary_violations,
)


def _term(term_ar: str, translations: dict[str, str], approved: bool = True):
    return GlossaryTerm(
        term_ar=term_ar, normalized_ar=term_ar,
        translations=translations, approved=approved,
    )


def test_glossary_hits_against_arabic_source() -> None:
    terms = [_term("صلاة", {"it": "preghiera", "en": "prayer"})]
    # The definite article attaches directly to the word: "الصلاة" contains
    # the term "صلاة" (recall over precision, documented MVP limitation).
    hits = glossary_hits("أقم الصلاة كما أمرك", terms)
    assert [t.term_ar for t in hits] == ["صلاة"]
    # A completely absent term is not reported.
    assert glossary_hits("الحمد لله رب العالمين", terms) == []


def test_unapproved_terms_never_influence_output() -> None:
    # §180: only approved terms are considered.
    pending = [_term("صلاة", {"it": "preghiera"}, approved=False)]
    assert glossary_hits("أقم الصلاة", pending) == []
    assert glossary_violations("أقم الصلاة", "qualcosa", "it", pending) == []


def test_violation_when_approved_translation_missing() -> None:
    terms = [_term("صلاة", {"it": "preghiera"})]
    # Source contains the term; draft lacks the approved Italian translation.
    assert glossary_violations("أقم الصلاة", "fa la cosa", "it", terms) == [
        "[it] 'صلاة' should be 'preghiera'"
    ]
    # Approved translation present -> no violation.
    assert glossary_violations("أقم الصلاة", "fa la preghiera", "it", terms) == []


def test_no_violation_when_source_term_absent() -> None:
    terms = [_term("صلاة", {"it": "preghiera"})]
    # Term not in source -> nothing to enforce.
    assert glossary_violations("الحمد لله", "lode a Dio", "it", terms) == []


def test_no_violation_when_term_has_no_translation_for_language() -> None:
    terms = [_term("صلاة", {"it": "preghiera"})]  # no "en" translation
    assert glossary_violations("أقم الصلاة", "do the thing", "en", terms) == []


def test_glossary_version_stamp() -> None:
    assert glossary_version(0) == "g0"
    assert glossary_version(7) == "g7"
