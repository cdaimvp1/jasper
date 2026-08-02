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

v2 (task #46): direct link to the exact email. Unlike Teams, there's no
reliable client-side URL that opens one specific Outlook desktop item, so
this is a server-side action (POST /api/action/open-email, see
outlook_actions.py) rather than a plain href — hence the "kind" field below:
"url" rows render as a plain link, "action" rows render as a button that
calls the endpoint.

v3 (task #47): a second action, "draft reply" (POST /api/action/draft-reply),
can apply to the SAME email as "open email" — one evidence row can now carry
more than one action, so this returns a LIST (evidence["deep_links"], plural)
rather than the single "deep_link" v1/v2 shipped with. Both require the
raw_item's entry_id (task #43) - rows ingested before that change, or with a
source none of this covers, get an empty list rather than a broken button.

v4 (task #48): a vendor action link (DocuSign/Adobe Sign/Ariba - see
link_extraction.py) is appended as a fourth possible entry for outlook_mail
rows whose signal_type is a recognized LIVE signature/approval request.

v5 (task #16, 2026-08-01): a third action, "draft forward" (POST
/api/action/draft-forward, outlook_actions.draft_forward), mirrors draft
reply exactly - same entry_id requirement, same Outlook-COM-draft-then-
Display() shape, just Forward() instead of Reply()/ReplyAll(). Marc's own
framing: reply and forward should both always be offered as options on a
mail item, whichever he actually wants to use.
"""
from __future__ import annotations

from urllib.parse import quote
from typing import Optional

import workgraph_store as ws
import link_extraction


def teams_chat_link(chat_id: Optional[str]) -> Optional[dict]:
    if not chat_id:
        return None
    url = f"https://teams.microsoft.com/l/chat/{quote(chat_id, safe='')}/0"
    return {"kind": "url", "url": url, "label": "Open Teams chat"}


def open_email_action(raw_item: dict) -> Optional[dict]:
    if raw_item.get("source") != "outlook_mail":
        return None
    if not raw_item.get("entry_id"):
        return None  # ingested before task #43 - no EntryID stored, nothing to open by
    return {"kind": "action", "endpoint": "/api/action/open-email",
            "raw_item_id": raw_item["id"], "label": "Open email"}


def draft_reply_action(raw_item: dict) -> Optional[dict]:
    if raw_item.get("source") != "outlook_mail":
        return None
    if not raw_item.get("entry_id"):
        return None
    return {"kind": "action", "endpoint": "/api/action/draft-reply",
            "raw_item_id": raw_item["id"], "label": "Draft reply"}


def draft_forward_action(raw_item: dict) -> Optional[dict]:
    if raw_item.get("source") != "outlook_mail":
        return None
    if not raw_item.get("entry_id"):
        return None
    return {"kind": "action", "endpoint": "/api/action/draft-forward",
            "raw_item_id": raw_item["id"], "label": "Draft forward"}


def vendor_action_link(raw_item: dict) -> Optional[dict]:
    extracted = link_extraction.extract_link_for_raw_item(raw_item)
    if not extracted:
        return None
    return {"kind": "url", "url": extracted["url"], "label": extracted["label"]}


def _links_for_raw_item(raw_item: dict) -> list[dict]:
    source = raw_item.get("source")
    if source == "teams_chat":
        link = teams_chat_link(raw_item.get("thread_key"))
        return [link] if link else []
    if source == "outlook_mail":
        return [a for a in (open_email_action(raw_item), draft_reply_action(raw_item),
                             draft_forward_action(raw_item), vendor_action_link(raw_item)) if a]
    return []


def attach_deep_links(evidence: list[dict]) -> list[dict]:
    """Mutates and returns `evidence`: adds a "deep_links" key to each row -
    a list (possibly empty), each entry either a {"kind":"url",...} row
    (client-side link) or a {"kind":"action",...} row (a button that calls a
    server endpoint). Batches the raw_item lookup - one query for the whole
    evidence list via get_raw_items_by_ids, not one query per row (same N+1
    fix already applied to list_evidence_for_issues this session).

    Also attaches "occurred_ts" (2026-08-02, detail-panel port) - checklist
    rows (asks/decisions/commitments/repeat_signals) carry a real
    raw_item_id but never surfaced the underlying message's real date, so
    the new per-row date in the checklist UI had nothing to read. Piggybacks
    on the SAME raw_item lookup this function already does for deep links -
    zero extra queries, not a second batch fetch."""
    raw_item_ids = [ev["raw_item_id"] for ev in evidence if ev.get("raw_item_id") is not None]
    raw_items_by_id = ws.get_raw_items_by_ids(raw_item_ids)
    for ev in evidence:
        raw_item = raw_items_by_id.get(ev.get("raw_item_id"))
        ev["deep_links"] = _links_for_raw_item(raw_item) if raw_item else []
        ev["occurred_ts"] = raw_item.get("occurred_ts") if raw_item else None
    return evidence
