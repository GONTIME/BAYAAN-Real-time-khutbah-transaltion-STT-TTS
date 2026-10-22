"""Sermon document parsing (BAYAAN_MASTER_SPEC.md §75, §155-§159).

§156 rules: preserve paragraph order, strip repeated headers/footers,
identify tables without treating them as sermon text, never assume OCR.

§158 DOCX caveat: comments, tracked deletions, hidden text and hyperlink
targets are EXCLUDED from the final text - only accepted body text counts.

§159 sanitization: strip control chars, normalize Unicode, remove unsafe
markup, retain paragraph boundaries.

§157 PDF caveat: image-only PDFs yield a loud NoExtractableTextError,
never a silent empty sermon. OCR (if ever added) requires sandbox +
Arabic language + human review.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

# Control chars are stripped; paragraph boundaries (\n) are retained (§159).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNSAFE_MARKUP = re.compile(r"<[^>]*>")


class NoExtractableTextError(ValueError):
    """§157: PDF has no extractable text (image-only). No OCR assumed."""


class UnsupportedDocumentError(ValueError):
    """Extension or payload is not an accepted sermon document."""


def sanitize_text(text: str) -> str:
    """§159: control chars + unsafe markup stripped, Unicode normalized,
    paragraph boundaries retained."""
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_CHARS.sub("", text)
    text = _UNSAFE_MARKUP.sub("", text)
    # Normalize line endings, collapse 3+ blank lines to a paragraph break.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def _strip_repeated_headers(paragraphs: list[str]) -> list[str]:
    """§156: drop lines repeated verbatim 3+ times (running headers/footers).

    A genuine repeated refrain inside a khutbah is rare; a mechanical
    page header is common. Only exact repeats are removed - nothing fuzzy.
    """
    counts: dict[str, int] = {}
    for p in paragraphs:
        key = p.strip()
        if key:
            counts[key] = counts.get(key, 0) + 1
    repeated = {k for k, v in counts.items() if v >= 3}
    if not repeated:
        return paragraphs
    return [p for p in paragraphs if p.strip() not in repeated]


def parse_txt(data: bytes, filename: str) -> str:
    """TXT/MD: decode (UTF-8 strict, UTF-8-SIG fallback) and sanitize.

    Markdown formatting markers (*, #, >) are kept as plain characters -
    no markdown interpretation is assumed.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            # BUG-011: legacy encodings (e.g. windows-1256) are a controlled
            # client error, never a 500. The upload route maps ValueError->
            # verify: parsers raise UnsupportedDocumentError/NoExtractableTextError (ValueError subclasses).
            raise UnsupportedDocumentError(
                f"{filename}: unsupported text encoding (UTF-8 required; legacy encodings such as windows-1256 are not accepted)"
            ) from exc
    # §156 header/footer stripping applies to every format, plain text too.
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    text = sanitize_text("\n\n".join(_strip_repeated_headers(paragraphs)))
    if not text:
        raise NoExtractableTextError(f"{filename}: no extractable text")
    return text

def parse_docx(data: bytes, filename: str) -> str:
    """DOCX via stdlib zip+XML (§158 caveat, no python-docx dependency).

    document.xml body paragraphs in order; tracked deletions (<w:del>,
    <w:delText>), paragraph-level deletion marks, hidden (w:vanish)
    runs and comment ranges (comments.xml is never read) are EXCLUDED.
    Hyperlink display text is kept, targets dropped. Tables: each row
    stays its own paragraph (§156 - tables are not prose).
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        xml_bytes = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise UnsupportedDocumentError(f"{filename}: not a valid DOCX") from exc
    root = ET.fromstring(xml_bytes)
    body = root.find("w:body", _DOCX_NS)
    if body is None:
        raise NoExtractableTextError(f"{filename}: no extractable text")

    def _run_text(run: ET.Element) -> str:
        if run.find("w:delText", _DOCX_NS) is not None:
            return ""
        if run.find("w:del", _DOCX_NS) is not None:
            return ""
        return "".join(t.text or "" for t in run.findall("w:t", _DOCX_NS))

    def _para_text(paragraph: ET.Element) -> str:
        ppr = paragraph.find("w:pPr", _DOCX_NS)
        if ppr is not None and ppr.find("w:del", _DOCX_NS) is not None:
            return ""  # paragraph-level tracked deletion
        if paragraph.find(".//w:vanish", _DOCX_NS) is not None:
            return ""  # hidden text run: excluded
        texts = [_run_text(r) for r in paragraph.findall("w:r", _DOCX_NS)]
        for link in paragraph.findall("w:hyperlink", _DOCX_NS):
            texts.extend(_run_text(r) for r in link.findall("w:r", _DOCX_NS))
        return "".join(texts).strip()

    paragraphs: list[str] = []
    for child in body:
        local = child.tag.split("}")[-1]
        if local == "p":
            text = _para_text(child)
            if text:
                paragraphs.append(text)
        elif local == "tbl":
            for row in child.findall("w:tr", _DOCX_NS):
                cells = [
                    "".join(t.text or "" for t in cell.findall(".//w:t", _DOCX_NS)).strip()
                    for cell in row.findall("w:tc", _DOCX_NS)
                ]
                row_text = " | ".join(c for c in cells if c)
                if row_text:
                    paragraphs.append(row_text)
        # sectPr and other body children are layout, not content.
    paragraphs = _strip_repeated_headers(paragraphs)
    text = sanitize_text("\n\n".join(paragraphs))
    if not text:
        raise NoExtractableTextError(f"{filename}: no extractable text")
    return text


def parse_pdf(data: bytes, filename: str) -> str:
    """PDF via pypdf (§157: image-only PDF -> loud error, no OCR).

    Pages extracted in order; blocks joined with paragraph breaks.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency present
        raise UnsupportedDocumentError("PDF support requires pypdf") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise UnsupportedDocumentError(f"{filename}: not a valid PDF") from exc
    paragraphs: list[str] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page must not kill the doc
            page_text = ""
        paragraphs.extend(p for p in page_text.split("\n") if p.strip())
    paragraphs = _strip_repeated_headers(paragraphs)
    text = sanitize_text("\n\n".join(paragraphs))
    if not text:
        raise NoExtractableTextError(
            f"{filename}: no extractable text (image-only PDF? OCR is not enabled)"
        )
    return text


def parse_document(data: bytes, filename: str) -> str:
    """Dispatch by extension (§185: the allowed set is configuration)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("txt", "md"):
        return parse_txt(data, filename)
    if ext == "docx":
        return parse_docx(data, filename)
    if ext == "pdf":
        return parse_pdf(data, filename)
    raise UnsupportedDocumentError(
        f"{filename}: unsupported extension '.{ext}' (txt/md/docx/pdf only)"
    )



_DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
