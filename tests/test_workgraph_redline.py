"""Tests for workgraph_redline.py (task #142, E19): deterministic
paragraph-level diff between two attachments' already-extracted text.
No relationship-discovery guessing - the caller names both attachment
ids explicitly.

Fixtures use single-newline-separated "paragraphs" (not blank-line-
separated) to match what attachment_extract.py's real extractors
actually produce - confirmed live against real attachments (see
workgraph_redline._paragraphs' own docstring)."""
from __future__ import annotations

import pytest

import workgraph_redline as wr


def _attachment(ws_db, filename, extracted_text, sha256="deadbeef"):
    return ws_db.create_attachment(
        entity_type="issue", entity_id="marc-1", kind="upload", filename=filename,
        stored_path=f"fake/{filename}", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=100, sha256_hex=sha256, uploaded_by="marc", extracted_text=extracted_text,
    )


def test_identical_text_produces_no_diff(ws_db):
    text = "Clause 1.\nClause 2.\nClause 3."
    a = _attachment(ws_db, "v1.docx", text, sha256="hash1")
    b = _attachment(ws_db, "v1_copy.docx", text, sha256="hash2")

    result = wr.compare_attachments(a, b)

    assert result["identical"] is True
    assert result["added"] == [] and result["removed"] == [] and result["changed"] == []
    assert result["unchanged_count"] == 3


def test_added_paragraph_is_detected(ws_db):
    a = _attachment(ws_db, "v1.docx", "Clause 1.\nClause 2.", sha256="hash3")
    b = _attachment(ws_db, "v2.docx", "Clause 1.\nClause 2.\nClause 3 (new).", sha256="hash4")

    result = wr.compare_attachments(a, b)

    assert result["added"] == ["Clause 3 (new)."]
    assert result["removed"] == []
    assert result["identical"] is False


def test_removed_paragraph_is_detected(ws_db):
    a = _attachment(ws_db, "v1.docx", "Clause 1.\nClause 2.\nClause 3.", sha256="hash5")
    b = _attachment(ws_db, "v2.docx", "Clause 1.\nClause 3.", sha256="hash6")

    result = wr.compare_attachments(a, b)

    assert result["removed"] == ["Clause 2."]


def test_changed_paragraph_is_detected(ws_db):
    a = _attachment(ws_db, "v1.docx", "Payment terms: net 30.", sha256="hash7")
    b = _attachment(ws_db, "v2.docx", "Payment terms: net 45.", sha256="hash8")

    result = wr.compare_attachments(a, b)

    assert result["changed"] == [{"old": "Payment terms: net 30.", "new": "Payment terms: net 45."}]
    assert result["added"] == [] and result["removed"] == []


def test_unchanged_paragraphs_surrounding_a_change_are_not_flagged(ws_db):
    a = _attachment(ws_db, "v1.docx", "Intro.\nPayment: net 30.\nSignature block.", sha256="hash9")
    b = _attachment(ws_db, "v2.docx", "Intro.\nPayment: net 45.\nSignature block.", sha256="hash10")

    result = wr.compare_attachments(a, b)

    assert result["unchanged_count"] == 2
    assert len(result["changed"]) == 1


def test_missing_attachment_raises_value_error(ws_db):
    a = _attachment(ws_db, "v1.docx", "text", sha256="hash11")
    with pytest.raises(ValueError, match="no such attachment"):
        wr.compare_attachments(a, 999999)


def test_attachment_without_extracted_text_raises_value_error(ws_db):
    a = _attachment(ws_db, "v1.docx", "text", sha256="hash12")
    b = _attachment(ws_db, "v2.docx", None, sha256="hash13")
    with pytest.raises(ValueError, match="no extracted text"):
        wr.compare_attachments(a, b)
