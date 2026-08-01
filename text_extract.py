"""
text_extract.py — resolve the BEST available text for a raw_item, for every
deterministic extraction function (value regex, classify's cue-matching) to
read instead of the 500-char body_preview.

Why this exists (2026-08-01, real-incident follow-up): full-body capture
already exists and works (task #43, outlook_com_ingest.py's _absorb_body) -
verified live this session, a real scan correctly staged body.txt/body.html
for real mail. But nothing downstream ever reads raw_ref - every extraction
function still only ever sees subject + a 500-char preview, even once a
full body is sitting on disk. This module is the missing read side.

Quote-stripping, not diffing: a reply's full body includes the ENTIRE
quoted thread beneath it, so feeding the raw full body into extraction
would mean the same historical text gets re-scanned in every reply on a
thread. Real diffing isn't needed here - Outlook/Gmail-style quote
boundaries are highly predictable (a "From:/Sent:/To:/Subject:" header
block, "-----Original Message-----", "On <date>, X wrote:") - a boundary-
detecting regex keeps only a message's own new top content, matching this
codebase's own preference for cheap deterministic parsing over anything
fuzzier (see workgraph_classify.py/workgraph_nba.py's own docstrings)."""
from __future__ import annotations

import json
import re
from typing import Optional

import paths

_QUOTE_BOUNDARY_RE = re.compile(
    r"(?:"
    r"-{3,}\s*Original Message\s*-{3,}"
    r"|^From:\s*.+?\n(?:Sent|Date):\s*.+?\n(?:To|Cc):\s*.+?\n(?:Cc:\s*.+?\n)?Subject:\s*.+?$"
    r"|^On\s.{3,100}?wrote:\s*$"
    r")",
    re.MULTILINE | re.IGNORECASE,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WHITESPACE_RE = re.compile(r"[ \t]+")


def strip_quoted_reply(text: str) -> str:
    """Keep only the text before the first recognized quote boundary. No
    boundary found -> the whole text is new content, returned unchanged
    (a plain FYI note, or the very first message in a thread, has nothing
    to strip)."""
    if not text:
        return text
    m = _QUOTE_BOUNDARY_RE.search(text)
    return text[:m.start()].rstrip() if m else text


def _html_to_text(html: str) -> str:
    """Crude but deterministic - strip tags, collapse runs of spaces/tabs.
    Good enough for regex/keyword extraction, which is all this feeds;
    never rendered for a human (that's what the real "Open email" deep
    link is for - see deep_links.py)."""
    text = _HTML_TAG_RE.sub(" ", html)
    text = _HTML_WHITESPACE_RE.sub(" ", text)
    return text


def resolve_item_text(item: dict) -> str:
    """Best-available text for a raw_item: the full, quote-stripped body
    when raw_ref points to one, else body_preview (old mail predating task
    #43, or the rare case body absorption failed for this item) - never an
    empty string when a preview exists, never a guess when neither does."""
    raw_ref = item.get("raw_ref")
    if raw_ref:
        try:
            ref = json.loads(raw_ref)
        except (TypeError, ValueError):
            ref = {}
        body_text_rel = ref.get("body_text")
        if body_text_rel:
            full = _read_relative(body_text_rel)
            if full:
                return strip_quoted_reply(full)
        body_html_rel = ref.get("body_html")
        if body_html_rel:
            full_html = _read_relative(body_html_rel)
            if full_html:
                return strip_quoted_reply(_html_to_text(full_html))
    return item.get("body_preview") or ""


def _read_relative(rel_path: str) -> Optional[str]:
    path = paths.DOCUMENTS_DIR / rel_path
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None  # staged file missing/moved - fall through to the next option, never raise
