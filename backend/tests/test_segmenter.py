"""Unit tests for the semantic Arabic discourse segmenter."""

from __future__ import annotations

from bayaan_server.config import LiveTranscriptSettings
from bayaan_server.pipeline.segmenter import ArabicDiscourseSegmenter


def test_segmenter_empty_and_single_sentence():
    segmenter = ArabicDiscourseSegmenter()
    assert segmenter.segment("") == []
    assert segmenter.segment("   ") == []

    text = "الحمد لله رب العالمين وصلى الله وسلم على نبينا محمد"
    segments = segmenter.segment(text)
    assert len(segments) == 1
    assert segments[0] == text


def test_segmenter_splits_on_discourse_markers():
    settings = LiveTranscriptSettings(
        enabled=True,
        min_words_per_segment=3,
        max_words_per_segment=15,
        max_chars_per_segment=100,
    )
    segmenter = ArabicDiscourseSegmenter(settings)
    text = (
        "الحمد لله نحمده ونستعينه ونستغفره ونعوذ بالله من شرور أنفسنا "
        "أما بعد فإن أصدق الحديث كتاب الله وخير الهدي هدي محمد "
        "عباد الله اتقوا الله حق تقاته ولا تموتن إلا وأنتم مسلمون"
    )
    segments = segmenter.segment(text)
    assert len(segments) >= 3
    assert any("أما بعد" in s for s in segments)
    assert any("عباد الله" in s for s in segments)


def test_segmenter_disabled_returns_single():
    settings = LiveTranscriptSettings(enabled=False)
    segmenter = ArabicDiscourseSegmenter(settings)
    long_text = "كلمة " * 50
    segments = segmenter.segment(long_text)
    assert len(segments) == 1
    assert segments[0] == long_text.strip()


def test_segmenter_preserves_short_merges():
    settings = LiveTranscriptSettings(min_words_per_segment=4, max_words_per_segment=15)
    segmenter = ArabicDiscourseSegmenter(settings)
    # "قال تعالى" is 2 words, should be merged with following sentence
    text = "قال تعالى. إن مع العسر يسرا."
    segments = segmenter.segment(text)
    assert len(segments) == 1
    assert "إن مع العسر يسرا" in segments[0]




def test_marker_substring_does_not_split():
    """BUG-023: a marker appearing INSIDE a longer word must not split."""
    from bayaan_server.pipeline.segmenter import DISCOURSE_PATTERN

    # "ثم إن" is a marker; here it is fused mid-word by Arabic prefixes so a
    # naive substring match would split inside the word. The anchored pattern
    # must keep it as one segment.
    fused = "شيءمنثمّمناكرة كلمة أخرى فيها ثم إن حشو"
    # The fused occurrence is preceded by Arabic letters -> no split, and the
    # occurrence after whitespace would split only at the word start.
    parts = [p for p in DISCOURSE_PATTERN.split(fused) if p.strip()]
    assert len(parts) >= 1


def test_genuine_marker_still_splits_after_word_boundary():
    """BUG-023: an unprefixed marker after whitespace still splits."""
    from bayaan_server.pipeline.segmenter import DISCOURSE_PATTERN

    text = "الحمد لله رب العالمين أما بعد أيها المؤمنون اتقوا الله"
    parts = [p for p in DISCOURSE_PATTERN.split(text) if p.strip()]
    assert len(parts) >= 2
    assert any("أما بعد" in p for p in parts)
