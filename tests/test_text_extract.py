"""Regression tests for text_extract.py (2026-08-01, real-incident follow-up):
resolving the full quote-stripped body instead of the 500-char preview."""
from __future__ import annotations

import json

import text_extract as te


# --- strip_quoted_reply --------------------------------------------------

def test_strip_quoted_reply_outlook_original_message_marker():
    text = "Sounds good, let's proceed.\n\n-----Original Message-----\nFrom: Bob\nSent: yesterday\nOld quoted content here."
    assert te.strip_quoted_reply(text) == "Sounds good, let's proceed."


def test_strip_quoted_reply_outlook_header_block():
    text = (
        "Thanks Thomas, no questions from me. You are so close!!\n\n"
        "From: Thomas Turner <tturner@lilly.com>\n"
        "Date: Friday, 24 July 2026 at 15:49\n"
        "To: Jade D Kas <kas_jade_d@lilly.com>\n"
        "Subject: Fw: Workday Early Renewal Order Form\n\n"
        "Jade/Aoife, here is the latest order form..."
    )
    assert te.strip_quoted_reply(text) == "Thanks Thomas, no questions from me. You are so close!!"


def test_strip_quoted_reply_gmail_style_wrote_marker():
    text = "Because it's not a CRM product or add-on. Completely different platform\nOn Wed, Jul 29, 2026 at 4:37 PM Marc Lane <lane_marc@lilly.com> wrote:\nHey, I think I remember..."
    assert te.strip_quoted_reply(text) == "Because it's not a CRM product or add-on. Completely different platform"


def test_strip_quoted_reply_no_boundary_returns_unchanged():
    text = "Just a plain standalone note with no quoted history at all."
    assert te.strip_quoted_reply(text) == text


def test_strip_quoted_reply_empty_string_is_safe():
    assert te.strip_quoted_reply("") == ""
    assert te.strip_quoted_reply(None) is None


# --- resolve_item_text ---------------------------------------------------

def test_resolve_item_text_reads_full_body_text_when_raw_ref_present(isolated_paths):
    rel = "raw_items/42/body.txt"
    full_path = isolated_paths.DOCUMENTS_DIR / rel
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(
        "The real total contract value is $50,000,000.\n\n"
        "-----Original Message-----\nOld quoted stuff with a different number $999.",
        encoding="utf-8",
    )
    item = {"raw_ref": json.dumps({"body_text": rel}), "body_preview": "The real total..."}

    result = te.resolve_item_text(item)

    assert "$50,000,000" in result
    assert "$999" not in result  # quoted history correctly stripped


def test_resolve_item_text_falls_back_to_body_html_when_no_text_file(isolated_paths):
    rel = "raw_items/43/body.html"
    full_path = isolated_paths.DOCUMENTS_DIR / rel
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text("<html><body><p>Real content <b>$50M</b> here</p></body></html>", encoding="utf-8")
    item = {"raw_ref": json.dumps({"body_html": rel}), "body_preview": "short preview"}

    result = te.resolve_item_text(item)

    assert "$50M" in result
    assert "<" not in result


def test_resolve_item_text_falls_back_to_preview_when_no_raw_ref():
    item = {"raw_ref": None, "body_preview": "just the short preview"}
    assert te.resolve_item_text(item) == "just the short preview"


def test_resolve_item_text_falls_back_to_preview_when_staged_file_missing(isolated_paths):
    """A raw_ref that points at a file that isn't actually there (moved,
    cleaned up, or from before this mechanism existed) must fail open to
    the preview, never raise."""
    item = {"raw_ref": json.dumps({"body_text": "raw_items/999/body.txt"}), "body_preview": "fallback text"}
    assert te.resolve_item_text(item) == "fallback text"


def test_resolve_item_text_malformed_raw_ref_json_falls_back_safely():
    item = {"raw_ref": "not valid json", "body_preview": "fallback text"}
    assert te.resolve_item_text(item) == "fallback text"


def test_resolve_item_text_no_body_preview_either_is_empty_string():
    assert te.resolve_item_text({}) == ""
