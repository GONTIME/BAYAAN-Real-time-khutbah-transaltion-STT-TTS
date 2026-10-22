"""Sermon preparation package (§192 Phase 3)."""

from .glossary import glossary_hits, glossary_version, glossary_violations
from .parsers import (
    NoExtractableTextError,
    UnsupportedDocumentError,
    parse_document,
    sanitize_text,
)
from .segmenter import segment_text

__all__ = [
    "NoExtractableTextError",
    "UnsupportedDocumentError",
    "glossary_hits",
    "glossary_version",
    "glossary_violations",
    "parse_document",
    "sanitize_text",
    "segment_text",
]
