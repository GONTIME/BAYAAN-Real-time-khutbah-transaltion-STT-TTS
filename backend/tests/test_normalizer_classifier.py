"""Normalizer + rule detector tests (§48, §65 step 1)."""

from __future__ import annotations

from bayaan_server.domain.enums import ContentType
from bayaan_server.pipeline.classifier import RuleContentDetector
from bayaan_server.pipeline.normalizer import ArabicNormalizerV1


def test_strips_tashkeel_and_tatweel() -> None:
    n = ArabicNormalizerV1()
    assert n.normalize("ٱلْحَمْدُ للهِ رَبّ الْعَـٰلَمِين") == "الحمد لله رب العالمين"


def test_unifies_alef_variants() -> None:
    n = ArabicNormalizerV1()
    assert n.normalize("أحمد إبراهيم آدم") == "احمد ابراهيم ادم"


def test_collapse_whitespace() -> None:
    n = ArabicNormalizerV1()
    assert n.normalize("  نص\tمتجدد   ") == "نص متجدد"


def test_detector_quran_by_ornate_brackets() -> None:
    d = RuleContentDetector()
    assert d.detect("﴿إِنَّ مَعَ الْعُسْرِ يُسْرًا﴾") is ContentType.QURAN


def test_detector_hadith_by_explicit_marker() -> None:
    d = RuleContentDetector()
    assert d.detect("قال رسول الله صلى الله عليه وسلم: إنما الأعمال بالنيات") is ContentType.HADITH


def test_detector_dua() -> None:
    d = RuleContentDetector()
    assert d.detect("اللهم ارحمنا") is ContentType.DUA


def test_detector_defaults_to_general_and_never_guesses_scripture() -> None:
    d = RuleContentDetector()
    assert d.detect("أهلاً وسهلاً بكم في مسجدنا") is ContentType.GENERAL
    # The word "اللهم" inside a longer quote is the only DUA trigger;
    # ordinary khutbah speech must stay GENERAL.
    assert d.detect("اليوم نتكلم عن أهمية الصدق") is ContentType.GENERAL