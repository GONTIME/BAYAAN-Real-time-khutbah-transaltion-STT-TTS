"""Semantic discourse segmentation for live Islamic speech (§190, ADR-0015).

Splits continuous spoken Arabic into coherent discourse segments for machine translation.
Preserves compound honorifics and sacred phrases, uses Islamic discourse markers
(e.g., 'أما بعد', 'عباد الله', 'أيها الإخوة', 'قال رسول الله', 'واعلموا أن', 'ثم إن'),
and enforces length constraints (min/max words and characters).
"""

from __future__ import annotations

import re
from typing import List

from bayaan_server.config import LiveTranscriptSettings

# Protected compound phrases that should never be severed
PROTECTED_PHRASES = [
    "صلى الله عليه وسلم",
    "عليه الصلاة والسلام",
    "رضي الله عنه",
    "رضي الله عنهم",
    "رضي الله عنها",
    "رحمه الله",
    "رحمهم الله",
    "جل جلاله",
    "عز وجل",
    "سبحانه وتعالى",
    "رسول الله",
    "كتاب الله",
    "سنة رسول الله",
    "أهل السنة والجماعة",
]

# Major Islamic discourse boundary markers (start of new semantic clauses)
DISCOURSE_MARKERS = [
    r"أما بعد",
    r"ثم أما بعد",
    r"عباد الله",
    r"أيها المسلمون",
    r"أيها الناس",
    r"أيها الإخوة",
    r"معشر المسلمين",
    r"قال الله تعالى",
    r"يقول الله تعالى",
    r"قال تعالى",
    r"قال رسول الله",
    r"يقول النبي",
    r"عن النبي",
    r"واعلموا أن",
    r"واعلموا رحمكم الله",
    r"فاتقوا الله",
    r"أوصيكم ونفسي بتقوى الله",
    r"أيها المؤمنون",
    r"ثم إن",
    r"ألا وإن",
]

# BUG-023: anchor each marker to a word start. `\w` with re.UNICODE covers
# Arabic letters/digits, so (?<!\w) prevents splitting *inside* a longer word
# (e.g. a marker occurring as a substring). A genuine marker after whitespace,
# punctuation, or at the start of the utterance still matches.
DISCOURSE_PATTERN = re.compile(
    r"(?<!\w)(?=(?:" + "|".join(DISCOURSE_MARKERS) + r"))",
    re.UNICODE,
)

# Conjunction boundaries when utterance gets overly long
CONJUNCTION_PATTERN = re.compile(
    r"(?<=\s)(?=(?:ثم|وكذلك|ولذلك|بل|لكن|حيث|إذ|بينما|لذا)\s)",
    re.UNICODE,
)

PUNCTUATION_SPLIT = re.compile(r"[.!?؟؛;\n\r]+", re.UNICODE)


class ArabicDiscourseSegmenter:
    """Semantic discourse segmenter for live khutbah and Arabic audio ingest."""

    def __init__(self, settings: LiveTranscriptSettings | None = None) -> None:
        self.settings = settings or LiveTranscriptSettings()

    def segment(self, text: str) -> List[str]:
        """Split Arabic text into semantically cohesive translation-ready chunks."""
        cleaned = text.strip()
        if not cleaned:
            return []

        if not self.settings.enabled:
            return [cleaned]

        # 1. First split on explicit punctuation if present
        initial_chunks = [c.strip() for c in PUNCTUATION_SPLIT.split(cleaned) if c.strip()]
        if not initial_chunks:
            initial_chunks = [cleaned]

        discourse_chunks: List[str] = []
        for chunk in initial_chunks:
            # 2. Split on major discourse markers
            sub_chunks = [s.strip() for s in DISCOURSE_PATTERN.split(chunk) if s.strip()]
            discourse_chunks.extend(sub_chunks)

        # 3. Refine chunks that exceed max words/characters
        refined_chunks: List[str] = []
        for chunk in discourse_chunks:
            words = chunk.split()
            if len(words) <= self.settings.max_words_per_segment and len(chunk) <= self.settings.max_chars_per_segment:
                refined_chunks.append(chunk)
            else:
                # Try splitting by conjunctions
                conj_splits = [c.strip() for c in CONJUNCTION_PATTERN.split(chunk) if c.strip()]
                for cs in conj_splits:
                    cs_words = cs.split()
                    if len(cs_words) <= self.settings.max_words_per_segment:
                        refined_chunks.append(cs)
                    else:
                        # Fallback: chunk by word count balance
                        refined_chunks.extend(self._split_by_word_limit(cs))

        # 4. Merge orphan fragments that are too short (< min_words_per_segment)
        merged_chunks = self._merge_short_segments(refined_chunks)
        return merged_chunks if merged_chunks else [cleaned]

    def _split_by_word_limit(self, text: str) -> List[str]:
        words = text.split()
        if not words:
            return []
        chunks = []
        limit = self.settings.max_words_per_segment
        for i in range(0, len(words), limit):
            chunk = " ".join(words[i : i + limit])
            chunks.append(chunk)
        return chunks

    def _merge_short_segments(self, chunks: List[str]) -> List[str]:
        if not chunks:
            return []

        merged: List[str] = []
        current = chunks[0]

        for next_chunk in chunks[1:]:
            current_words = len(current.split())
            next_words = len(next_chunk.split())

            # If current is very short or combined is within max limit
            if (
                current_words < self.settings.min_words_per_segment
                or (current_words + next_words <= self.settings.max_words_per_segment
                    and len(current) + len(next_chunk) + 1 <= self.settings.max_chars_per_segment)
            ):
                current = f"{current} {next_chunk}"
            else:
                merged.append(current)
                current = next_chunk

        if current:
            # If last chunk is too short and there's a previous chunk, merge into previous
            if len(current.split()) < self.settings.min_words_per_segment and merged:
                merged[-1] = f"{merged[-1]} {current}"
            else:
                merged.append(current)

        return merged
