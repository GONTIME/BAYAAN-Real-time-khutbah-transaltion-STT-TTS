"""Scripture verification + translation routing tests (§76, §78, §86, §206).

Quran is NEVER machine-translated: with no approved reference it is BLOCKED.
Hadith falls back to a general (non-attributed) translation when unverified.
Only VERIFIED / VERIFIED_NEAR carry an approved reference (§0 rule 8).
"""

from __future__ import annotations

from bayaan_server.config import ScriptureSettings
from bayaan_server.domain.enums import VerificationLevel
from bayaan_server.sermon.scripture import ScriptureMatcher, ScriptureReference
from bayaan_server.pipeline.translation_router import TranslationRouter


AYAH = "ان مع العسر يسرا"


def _ref(key="94:6", arabic=AYAH, translations=None):
    return ScriptureReference(
        key=key,
        normalized_arabic=arabic,
        kind="QURAN",
        translations=translations or {"en": "with hardship comes ease", "it": "con la difficolta viene la facilita"},
        approved=True,
    )


class _MT:
    async def translate(self, text, source, target):
        class R:
            pass
        r = R()
        r.text = f"[MT:{target}] {text}"
        return r


def _matcher(refs=None):
    return ScriptureMatcher(ScriptureSettings(), refs if refs is not None else [_ref()])


def test_exact_match_is_verified() -> None:
    match = _matcher().detect(AYAH)
    assert match.level is VerificationLevel.VERIFIED
    assert match.verified
    assert match.reference is not None
    assert match.approved_translation("en") == "with hardship comes ease"


def test_quran_decoration_does_not_break_an_exact_match() -> None:
    """§65 marks Quran with ornate parentheses; the store keeps the verse
    verbatim (§42.2), so a decorated live line must still verify."""
    decorated = f"\ufd3f{AYAH}\ufd3e"
    match = _matcher().detect(decorated)
    assert match.level is VerificationLevel.VERIFIED
    assert match.score == 1.0
    assert match.reference is not None
    assert match.reference.key == "94:6"


def test_decoration_is_not_confused_with_content() -> None:
    """Stripping the marker must not make unrelated text match."""
    match = _matcher().detect("\ufd3f\ufd3e كلام عام لا علاقة له بالاية")
    assert not match.verified
    assert match.reference is None


def test_near_match_is_verified_near() -> None:
    # One extra word: below exact, above near threshold (0.85).
    match = _matcher().detect(AYAH + " حقا")
    assert match.level in (VerificationLevel.VERIFIED_NEAR, VerificationLevel.MANUAL_REVIEW_REQUIRED)


def test_weak_match_never_claims_a_reference() -> None:
    match = _matcher().detect("كلام عام لا علاقة له بالاية")
    assert not match.verified
    assert match.reference is None


def test_empty_store_means_every_hit_is_unknown() -> None:
    # No curated dataset -> UNKNOWN -> nothing may be cited (§0 rule 8).
    match = _matcher([]).detect(AYAH)
    assert match.level is VerificationLevel.UNKNOWN
    assert not match.verified


async def test_quran_without_reference_is_blocked() -> None:
    router = TranslationRouter(_matcher([]), ScriptureSettings())
    decision = await router.route_quran(AYAH, "en")
    assert decision.blocked
    assert decision.text == ""
    assert decision.content_type.value == "QURAN"


async def test_quran_with_reference_uses_approved_translation() -> None:
    router = TranslationRouter(_matcher(), ScriptureSettings())
    decision = await router.route_quran(AYAH, "en")
    assert not decision.blocked
    assert decision.text == "with hardship comes ease"
    assert decision.approved_reference == "94:6"


async def test_hadith_unverified_falls_back_to_general_mt() -> None:
    # §86: hadith with no approved reference -> general translation, no claim.
    router = TranslationRouter(_matcher([]), ScriptureSettings())
    decision = await router.route_hadith("قال رسول الله انما الاعمال بالنيات", "en", _MT())
    assert not decision.blocked
    assert decision.route == "hadith_general_mt"
    assert decision.approved_reference is None
    assert decision.text.startswith("[MT:en]")


async def test_hadith_strict_policy_blocks_without_reference() -> None:
    router = TranslationRouter(
        _matcher([]),
        ScriptureSettings(hadith_fallback_general=False),
    )
    decision = await router.route_hadith("قال رسول الله شيئا", "en", _MT())
    assert decision.blocked
