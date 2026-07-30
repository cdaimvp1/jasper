"""
deep_links.py — deterministic action links attached to evidence rows so a
person can jump straight to the real source (the exact Teams chat, the exact
email, a vendor's signature page) instead of hunting for it manually. No LLM
involved; every link here is built purely from fields the ingestion pipeline
already captures. Modeled on workgraph_recommend.py's exact shape (a pure
builder + an attach_*(evidence) mutator called once from the issue-detail
endpoint) since this is the same kind of per-evidence, deterministic add-on.

v1 (task #44): Teams chat deep link, built from a teams_chat raw_item's
thread_key — confirmed against real ingested data this session to already
equal the chat's own Graph API id verbatim (e.g. "19:...@thread.v2" for a
group chat, "19:...@unq.gbl.spaces" for a 1:1 — ingest/normalize.py's
_process_teams_chat sets thread_key = chat_id directly, no parsing needed).

Microsoft's documented chat deep-link format is
https://teams.microsoft.com/l/chat/<chat-id>/0 , with the chat id
percent-encoded (it contains ':' and '@', both meaningful in a URL path).
Built to that published spec — NOT independently click-tested in a live
browser this session (none available) — so treat this as "correct per docs,
worth confirming the first time a real link is clicked" rather than proven.
"""
from __future__ import annotations

from urllib.parse import quote
from typing import Optional

import workgraph_store as ws


def teams_chat_link(chat_id: Optional[str]) -> Optional[dict]:
    if not chat_id:
        return None
    url = f"https://teams.microsoft.com/l/chat/{quote(chat_id, safe='')}/0"
    return {"url": url, "label": "Open Teams chat"}


def _link_for_raw_item(raw_item: dict) -> Optional[dict]:
    source = raw_item.get("source")
    if source == "teams_chat":
        return teams_chat_link(raw_item.get("thread_key"))
    return None


def attach_deep_links(evidence: list[dict]) -> list[dict]:
    """Mutates and returns `evidence`: adds a "deep_link" key ({"url","label"}
    or None) to each row. Batches the raw_item lookup - one query for the
    whole evidence list via get_raw_items_by_ids, not one query per row (same
    N+1 fix already applied to list_evidence_for_issues this session)."""
    raw_item_ids = [ev["raw_item_id"] for ev in evidence if ev.get("raw_item_id") is not None]
    raw_items_by_id = ws.get_raw_items_by_ids(raw_item_ids)
    for ev in evidence:
        raw_item = raw_items_by_id.get(ev.get("raw_item_id"))
        ev["deep_link"] = _link_for_raw_item(raw_item) if raw_item else None
    return evidence
