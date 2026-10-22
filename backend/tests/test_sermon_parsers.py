"""Phase 3 parser tests (§155-§159): txt/md/docx/pdf, sanitization,
header stripping, and explicit failure modes."""

from __future__ import annotations

import io
import struct
import wave
import zipfile

import pytest

from bayaan_server.sermon.parsers import (
    NoExtractableTextError,
    UnsupportedDocumentError,
    parse_document,
    sanitize_text,
)

AR_TEXT = "الحمد لله رب العالمين. والصلاة والسلام على رسول الله."


def test_txt_parses_and_sanitizes() -> None:
    raw = f"{AR_TEXT}\x00\x07<script>alert(1)</script>".encode("utf-8")
    text = parse_document(raw, "khutbah.txt")
    assert AR_TEXT in text
    assert "\x00" not in text
    assert "<script>" not in text


def test_md_accepted_as_text() -> None:
    raw = f"# خطبة\n\n{AR_TEXT}".encode("utf-8")
    text = parse_document(raw, "khutbah.md")
    assert AR_TEXT in text


def test_unsupported_extension_is_loud() -> None:
    with pytest.raises(UnsupportedDocumentError):
        parse_document(b"data", "khutbah.odt")


def test_repeated_headers_stripped() -> None:
    body = "\n\n".join(
        ["مسجد النور - خطبة الجمعة", AR_TEXT, "مسجد النور - خطبة الجمعة", AR_TEXT]
        + ["مسجد النور - خطبة الجمعة"]
    )
    text = parse_document(body.encode("utf-8"), "k.txt")
    assert text.count("مسجد النور") == 0
    assert AR_TEXT in text


def _minimal_docx(paragraphs: list[str], deleted: list[str] | None = None) -> bytes:
    """Build a minimal DOCX in-memory (no python-docx needed)."""
    deleted = deleted or []
    body_xml = ""
    for p in paragraphs:
        body_xml += f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>"
    for p in deleted:
        body_xml += (
            f"<w:p><w:r><w:delText>{p}</w:delText></w:r></w:p>"
            f"<w:p><w:pPr><w:del/></w:pPr><w:r><w:t>{p}</w:t></w:r></w:p>"
        )
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_xml}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc_xml)
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
    return buf.getvalue()


def test_docx_parses_body_text() -> None:
    data = _minimal_docx([AR_TEXT, "الخطبة الثانية"])
    text = parse_document(data, "khutbah.docx")
    assert AR_TEXT in text
    assert "الخطبة الثانية" in text


def test_docx_excludes_tracked_deletions() -> None:
    """§158: tracked deletions are not accepted text."""
    data = _minimal_docx([AR_TEXT], deleted=["نص محذوف يجب ألا يظهر"])
    text = parse_document(data, "khutbah.docx")
    assert AR_TEXT in text
    assert "نص محذوف" not in text


def test_docx_invalid_zip_is_loud() -> None:
    with pytest.raises(UnsupportedDocumentError):
        parse_document(b"not a zip", "khutbah.docx")


def test_sanitize_retains_paragraph_boundaries() -> None:
    text = sanitize_text("فقرة أولى\n\n\n\nفقرة ثانية")
    assert text == "فقرة أولى\n\nفقرة ثانية"


def test_pdf_without_text_is_loud() -> None:
    """§157: image-only PDF -> NoExtractableTextError, never silent."""
    from pypdf import PdfWriter

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buf)
    with pytest.raises(NoExtractableTextError):
        parse_document(buf.getvalue(), "scan.pdf")


def _wav_bytes(text_marker: str = "") -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", 3000) for _ in range(16000)))
    return buf.getvalue()


def test_windows1256_controlled_error_bug011() -> None:
    """BUG-011: legacy encoding is a controlled ValueError, never a crash."""
    from bayaan_server.sermon.parsers import parse_txt
    import pytest
    data = "مرحبا بالعالم".encode("windows-1256")
    with pytest.raises(ValueError, match="encoding"):
        parse_txt(data, "sermon.txt")
