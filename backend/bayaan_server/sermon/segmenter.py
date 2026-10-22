"""Semantic segmentation (BAYAAN_MASTER_SPEC.md §75, §160).

Pseudocode implemented:
    blocks = parse_document(document)
    FOR block: clean -> sentence_split -> merge_too_short -> split_too_long
    preserve original order

§160: segments small enough for latency, big enough for meaning -
no 1-2 word segments (merged), no two-page segments (split).
Bounds live in SermonSettings (§185), never inline.
"""

from __future__ import annotations

import re

from bayaan_server.config import SermonSettings

# Arabic + Latin sentence terminators, Arabic-first (§75 khutbah text).
_SENTENCE_END = re.compile(r"(?<=[\u061f\u06d4\U0001eef1.!?])\s+")


def sentence_split(block: str) -> list[str]:
    """Split a paragraph into sentences on Arabic + Latin terminators."""
    parts = [s.strip() for s in _SENTENCE_END.split(block.strip())]
    return [p for p in parts if p]


def segment_text(text: str, settings: SermonSettings) -> list[str]:
    """Segment a sanitized document into ordered, sized segments."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    sentences: list[str] = []
    for block in blocks:
        sentences.extend(sentence_split(block))

    # merge_too_short: accumulate until the minimum size is reached.
    merged: list[str] = []
    buffer = ""
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if len(candidate) < settings.min_segment_chars:
            buffer = candidate
            continue
        merged.append(candidate)
        buffer = ""
    if buffer:
        if merged:
            merged[-1] = f"{merged[-1]} {buffer}".strip()
        else:
            merged.append(buffer)

    # split_too_long: hard-split oversized segments on word boundaries.
    final: list[str] = []
    for segment in merged:
        while len(segment) > settings.max_segment_chars:
            cut = segment.rfind(" ", 0, settings.max_segment_chars)
            if cut <= 0:
                cut = settings.max_segment_chars
            final.append(segment[:cut].strip())
            segment = segment[cut:].strip()
        if segment:
            final.append(segment)
    return final
