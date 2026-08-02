"""
attachment_extract.py — deterministic text extraction from attachment
files, so the same regex-based extraction (value figures, asks/decisions
cues) that already reads email bodies can also read what's actually
attached - a real order-form PDF or pricing XLSX sitting on disk, ignored
by every extraction function until now (task #29, 2026-08-01).

Text-layer / cell-value only, no OCR - a scanned-image-only PDF returns
empty text, which is the honest answer ("nothing extractable here"), not a
guess. Matches this codebase's own standing preference for cheap,
deterministic extraction over anything fuzzier (see workgraph_nba.py/
workgraph_classify.py's own docstrings) - OCR would be a real, separate,
much bigger feature if ever needed.
"""
from __future__ import annotations

from pathlib import Path

import openpyxl
import pypdf


def extract_pdf_text(path: Path) -> str:
    """Every page's text layer, concatenated. Empty string (never an
    exception) for an encrypted, corrupt, or scanned-image-only PDF - one
    bad attachment must never break the ingest batch it arrived in."""
    try:
        reader = pypdf.PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def extract_xlsx_text(path: Path) -> str:
    """Every cell's value across every worksheet, as text - deliberately
    simple (no attempt to reconstruct table structure) since this only
    feeds keyword/regex extraction, never rendered for a human. data_only
    reads the last-computed value of a formula cell, not the formula
    itself, and read_only keeps large workbooks from loading fully into
    memory."""
    try:
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        try:
            parts = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    for cell in row:
                        if cell is not None:
                            parts.append(str(cell))
            return " ".join(parts)
        finally:
            wb.close()
    except Exception:
        return ""


_EXTRACTORS = {
    ".pdf": extract_pdf_text,
    ".xlsx": extract_xlsx_text,
    ".xlsm": extract_xlsx_text,
}


def extract_text(path: Path) -> str:
    """Dispatches on file extension. Empty string for any type with no
    registered extractor (e.g. .docx today - a real gap, not silently
    pretended to be handled; add a real extractor before claiming
    coverage for a new type, never a placeholder that returns junk)."""
    extractor = _EXTRACTORS.get(path.suffix.lower())
    return extractor(path) if extractor else ""
