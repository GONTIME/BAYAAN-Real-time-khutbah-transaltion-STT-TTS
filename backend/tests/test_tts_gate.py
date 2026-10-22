"""TTS gate tests (BAYAAN_MASTER_SPEC.md §80 tts_gate.py, §95, §66, §201).

The §80 gate is the chokepoint behind the Phase 5 acceptance criterion
"unsafe outputs never reach TTS". It is strictly stricter than the text path:
each rule here is a permanent regression guard.
"""

from __future__ import annotations

from bayaan_server.config import SafetySettings, TtsSettings
from bayaan_server.domain.enums import ContentType, SafetyDecision
from bayaan_server.domain.tts import TtsCandidate, allow_tts


def _candidate(**overrides) -> TtsCandidate:
    base = dict(
        text="hello world",
        language="en",
        content_type=ContentType.GENERAL,
        safety_decision=SafetyDecision.ALLOW,
        validation_decision=SafetyDecision.ALLOW,
        translation_confidence=0.99,
        age_ms=0,
        approved_reference=None,
    )
    base.update(overrides)
    return TtsCandidate(**base)


def test_allows_a_clean_high_confidence_general_caption() -> None:
    verdict = allow_tts(_candidate(), SafetySettings(), TtsSettings())
    assert verdict.speak is True
    assert verdict.reasons == []


def test_refuses_when_safety_not_allow() -> None:
    # §80 (1): status must be PASS (ALLOW).
    verdict = allow_tts(
        _candidate(safety_decision=SafetyDecision.FALLBACK),
        SafetySettings(),
        TtsSettings(),
    )
    assert verdict.speak is False
    assert any("safety_fallback" in r for r in verdict.reasons)


def test_refuses_when_validation_not_allow() -> None:
    # §80 (2): a §79 WARNING/DISPLAY_ONLY is not PASS - TTS is stricter.
    verdict = allow_tts(
        _candidate(validation_decision=SafetyDecision.WARNING),
        SafetySettings(),
        TtsSettings(),
    )
    assert verdict.speak is False
    assert any("validation_warning" in r for r in verdict.reasons)


def test_refuses_low_confidence_uses_stricter_tts_floor() -> None:
    # §80 (3): the TTS floor is higher than the text translation floor.
    safety = SafetySettings(tts_min_confidence=0.95)
    # Passes the text translation minimum (0.70) but not the TTS floor.
    verdict = allow_tts(
        _candidate(translation_confidence=0.80), safety, TtsSettings()
    )
    assert verdict.speak is False
    assert any("tts_confidence_below" in r for r in verdict.reasons)


def test_refuses_missing_translation_confidence() -> None:
    # §166/§0 rule 8: absent confidence is never treated as speakable.
    verdict = allow_tts(
        _candidate(translation_confidence=None), SafetySettings(), TtsSettings()
    )
    assert verdict.speak is False
    assert "no_translation_confidence" in verdict.reasons


def test_optout_allows_missing_translation_confidence() -> None:
    # §80 (3) operator opt-in: with require_translation_confidence=False (the
    # "trust the approved chain" path for real MT providers - ollama /
    # ollama_cloud / libre - which emit no confidence, §0 rule 8), a MISSING
    # provider confidence no longer blocks speech; the other §80 rules still
    # gate independently.
    tts = TtsSettings(require_translation_confidence=False)
    verdict = allow_tts(
        _candidate(translation_confidence=None), SafetySettings(), tts
    )
    assert verdict.speak is True
    assert "no_translation_confidence" not in verdict.reasons


def test_optout_still_refuses_low_numeric_confidence() -> None:
    # The opt-out waives only the MISSING-confidence case. A provider that DOES
    # report a score below the TTS floor is still never spoken.
    tts = TtsSettings(require_translation_confidence=False)
    safety = SafetySettings(tts_min_confidence=0.9)
    verdict = allow_tts(
        _candidate(translation_confidence=0.5), safety, tts
    )
    assert verdict.speak is False
    assert any("tts_confidence_below" in r for r in verdict.reasons)


def test_refuses_quran_without_approved_reference() -> None:
    # §80 (4)/§206: sacred text is never spoken unverified.
    verdict = allow_tts(
        _candidate(content_type=ContentType.QURAN, approved_reference=None),
        SafetySettings(),
        TtsSettings(),
    )
    assert verdict.speak is False
    assert "sacred_text_unapproved" in verdict.reasons


def test_allows_quran_with_approved_reference() -> None:
    verdict = allow_tts(
        _candidate(content_type=ContentType.QURAN, approved_reference="94:6"),
        SafetySettings(),
        TtsSettings(),
    )
    assert verdict.speak is True


def test_refuses_hadith_without_approved_reference() -> None:
    verdict = allow_tts(
        _candidate(content_type=ContentType.HADITH, approved_reference=None),
        SafetySettings(),
        TtsSettings(),
    )
    assert verdict.speak is False
    assert "sacred_text_unapproved" in verdict.reasons


def test_refuses_stale_audio() -> None:
    # §80 (5)/§88: audio older than the staleness window is dropped.
    tts = TtsSettings(max_staleness_ms=8000)
    verdict = allow_tts(_candidate(age_ms=8001), SafetySettings(), tts)
    assert verdict.speak is False
    assert any("stale_age_ms" in r for r in verdict.reasons)


def test_allows_audio_within_staleness_window() -> None:
    tts = TtsSettings(max_staleness_ms=8000)
    verdict = allow_tts(_candidate(age_ms=7999), SafetySettings(), tts)
    assert verdict.speak is True


def test_refuses_empty_text() -> None:
    # §95/§0 rule 5: nothing to say is never spoken.
    verdict = allow_tts(_candidate(text="   "), SafetySettings(), TtsSettings())
    assert verdict.speak is False
    assert "empty_text" in verdict.reasons


def test_reasons_are_explainable_and_collected() -> None:
    # §168: every refusal reason is recorded, none silently dropped.
    verdict = allow_tts(
        _candidate(
            safety_decision=SafetyDecision.BLOCK,
            validation_decision=SafetyDecision.BLOCK,
            translation_confidence=0.1,
            content_type=ContentType.QURAN,
            age_ms=99999,
            text="",
        ),
        SafetySettings(),
        TtsSettings(),
    )
    assert verdict.speak is False
    assert len(verdict.reasons) >= 5
