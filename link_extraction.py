"""
link_extraction.py — task #48: pulls the real "click here" action link out
of a vendor email's HTML body, for known, ALREADY-CLASSIFIED automated
signal types only (workgraph_signals.py) - never runs on ordinary mail, and
never guesses at a vendor template that hasn't been confirmed against a real
subject/sender pattern (see that module's own "none of this is guessed"
discipline, which this follows the same way).

Gated to LIVE actionable signals only - never surfaces a "sign now"/"approve
now" link for a signal_type that's already closure/fyi (e.g. Adobe Sign's own
"you signed" or "fully executed" notices, DocuSign's "Completed:" emails,
Ariba's "fully approved" notice). That gate is exactly what stops this from
sending Marc to sign something already signed.

Requires the raw_item's staged HTML body (raw_ref, task #43) - a row
ingested before that change, or whose HTML write failed, has nothing to
parse and gets no link, not a guess.

HONEST CAVEAT: the per-vendor domain/keyword patterns below are built from
general knowledge of each vendor's transactional-email template, NOT from a
captured real sample in this session - no row has a staged HTML body yet, no
ingestion has run since task #43 landed. Treat these as "best-effort, worth
confirming against the first real vendor email captured" rather than proven
- same caveat class as the Teams deep-link URL format (task #44).

Word document @-mention/assignment links are DELIBERATELY NOT included here:
workgraph_signals.py has no confirmed real subject-line pattern for that
notification type yet (unlike DocuSign/Adobe Sign/Ariba, which are already
real, confirmed signal_types checked against Jasper's own mailbox) - adding
one without a real example would be exactly the kind of guess that module's
own docstring says not to make.

Computed at request time (called from deep_links.py, not precomputed into a
stored table): a single small HTML file read per evidence row, the same cost
class as the synchronous SQLite reads the issue-detail endpoint already does
inline - and always fresh, with no staleness risk if a signal_type's
classification is ever corrected later.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Optional

import paths

# signal_type (workgraph_signals.py) -> (vendor domain substrings, link_type, label).
# Only LIVE/actionable signal types belong here - see module docstring.
_LIVE_SIGNAL_LINK_SPEC: dict[str, tuple[tuple[str, ...], str, str]] = {
    "signature_requested": (("adobesign.com", "echosign.com"), "adobe_sign", "Open in Adobe Sign"),
    "signature_requested_docusign": (("docusign.net",), "docusign", "Open in DocuSign"),
    "ariba_pr_approval_needed": (("ariba.com",), "ariba", "Open in Ariba"),
}

_ACTION_TEXT_RE = re.compile(r"\b(review|sign|approve|click here|view document|open)\b", re.IGNORECASE)

# Task #303: SharePoint/OneDrive document links, unlike the vendor CTAs
# above, can appear in ANY ordinary message regardless of signal_type -
# nobody classifies "someone shared a file" as its own signal today, and
# it isn't gated the same way (a shared document is never closure/fyi-only
# the way a "you already signed this" notice is). Domain-matched only,
# same _AnchorCollector this module already builds - a genuinely reusable
# piece, not a new parser.
_CLOUD_DOC_DOMAINS = ("sharepoint.com", "1drv.ms", "onedrive.live.com")


class _AnchorCollector(HTMLParser):
    """Collects (href, visible_text) for every <a href=...> - a real parser,
    not a hand-rolled regex, since vendor ESP HTML is often ugly/malformed
    table markup that a naive regex would mishandle."""

    def __init__(self):
        super().__init__()
        self._anchors: list[list] = []
        self._in_a = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self._anchors.append([href, ""])
                self._in_a = True

    def handle_endtag(self, tag):
        if tag.lower() == "a":
            self._in_a = False

    def handle_data(self, data):
        if self._in_a and self._anchors:
            self._anchors[-1][1] += data

    @property
    def anchors(self) -> list[tuple[str, str]]:
        return [(href, text.strip()) for href, text in self._anchors]


def best_link(html_text: str, domains: tuple[str, ...]) -> Optional[str]:
    """Among every <a> whose href contains one of `domains`, prefers one
    whose visible text reads like the primary call-to-action (review/sign/
    approve/etc). Falls back to the first vendor-domain link found if none
    of the text matches - the least-certain path, since a transactional
    email can carry other same-domain links (footer/help/privacy) too."""
    try:
        collector = _AnchorCollector()
        collector.feed(html_text)
    except Exception:
        return None  # malformed HTML must never crash the caller
    candidates = [(href, text) for href, text in collector.anchors
                  if any(d in href.lower() for d in domains)]
    if not candidates:
        return None
    action_matches = [href for href, text in candidates if _ACTION_TEXT_RE.search(text)]
    if action_matches:
        return action_matches[0]
    return candidates[0][0]


def _staged_html_body(raw_item: dict) -> Optional[str]:
    """Shared by extract_link_for_raw_item and extract_cloud_doc_links_for_
    raw_item - both need the same staged HTML body, resolved the same way."""
    raw_ref = raw_item.get("raw_ref")
    if not raw_ref:
        return None
    try:
        ref = json.loads(raw_ref)
    except (TypeError, ValueError):
        return None
    html_rel_path = ref.get("body_html") if isinstance(ref, dict) else None
    if not html_rel_path:
        return None

    html_path = paths.DOCUMENTS_DIR / html_rel_path
    if not html_path.is_file():
        return None
    try:
        return html_path.read_text(encoding="utf-8")
    except OSError:
        return None


def extract_link_for_raw_item(raw_item: dict) -> Optional[dict]:
    """Returns {"link_type","url","label"} or None. `raw_item` is a
    workgraph_store.get_raw_item(s)-shaped row (needs signal_type + raw_ref)."""
    spec = _LIVE_SIGNAL_LINK_SPEC.get(raw_item.get("signal_type"))
    if not spec:
        return None
    domains, link_type, label = spec

    html_text = _staged_html_body(raw_item)
    if html_text is None:
        return None

    url = best_link(html_text, domains)
    if not url:
        return None
    return {"link_type": link_type, "url": url, "label": label}


def extract_cloud_doc_links_for_raw_item(raw_item: dict) -> list[dict]:
    """Task #303: every SharePoint/OneDrive document link in this message's
    body, regardless of signal_type - unlike extract_link_for_raw_item
    above, this isn't gated to already-classified vendor signals, since a
    shared document can show up in any ordinary email. Returns every
    matching anchor (a message can share more than one file), not just
    one "best" link - there's no single primary call-to-action the way a
    vendor signature request has. Each item: {"url", "label"} - label is
    the link's real visible text if it has one, else a generic fallback
    (never invented content, just what the anchor tag actually said)."""
    html_text = _staged_html_body(raw_item)
    if html_text is None:
        return []
    try:
        collector = _AnchorCollector()
        collector.feed(html_text)
    except Exception:
        return []
    seen_urls: set[str] = set()
    links: list[dict] = []
    for href, text in collector.anchors:
        if not any(d in href.lower() for d in _CLOUD_DOC_DOMAINS):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        links.append({"url": href, "label": text.strip() or "Shared document"})
    return links
