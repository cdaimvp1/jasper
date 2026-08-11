"""
workgraph_proactive.py - task #287: Marc's own framing (2026-08-08) - "Jasper
executes certain actions in the background, still gated by human approval
before committing... a request comes in at 6pm to review a contract with
the contract attached, jasper executes the contract review and holds it for
my review the next day. someone asks for a status update, jasper drafts the
response but holds it for my review."

Deliberately narrow: exactly two action types, both picked because NEITHER
has an external effect by itself -
  - review_contract: dispatches the same worker skill run Marc's own
    "Review contract" button already triggers (server_lean.py's
    /api/cockpit/actions) - a worker reading a document and writing a
    review has no effect outside Jasper until Marc acts on the result.
  - draft_status_update: drafts a reply and SAVES it to the Drafts folder
    (never Display()s/Send()s it) - Marc reviews and sends it himself,
    same as every other draft in this codebase.
The actual safety gate Marc described is downstream of both: a human
looking at the result before it becomes real (the contract review's own
findings; the draft's own wording before it's sent) - not a pre-approval
click on each individual trigger, which would defeat the entire point
("when I wake up it's already done").

What DOES require Marc's explicit approval, per design doc Section 12.10's
own standing rule (prepared_actions.required_approval is never satisfied
by anything an incoming message's own content says - a supplier's email
can't talk its way into pre-approving its own resulting action): the
config('proactive_actions','enabled') master toggle. Turning it on IS
Marc's approval - granted once, standing, for these two specific pre-
defined action classes - the same "the human's own request is the
approval" posture jasper_request_contract_review's own docstring already
uses. Defaults OFF, same as every other autonomous-leaning capability in
this codebase (personal_patterns, sent-mail learning, etc.).

Zero LLM: both triggers are plain phrase-list matches, same class of
technique as personal_patterns.py's keyword mining and workgraph_signals.py's
regex table - deliberately not a judgment call, so it's exactly as
inspectable/predictable as everything else this codebase gates on
deterministic detection.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

import config
import outlook_actions
import team_room
import workgraph_store as ws

_STATUS_UPDATE_PHRASES = (
    "status update", "any update", "quick update", "give me an update",
    "update me on", "where do things stand", "where are we on",
    "can you update", "can we get an update", "any news on",
)

_CONTRACT_REVIEW_PHRASES = (
    "please review the attached", "can you review the attached",
    "review this contract", "review the attached contract",
    "review the sow", "review this agreement", "please review this",
    "attached for your review", "for your review",
)

_CONTRACT_ATTACHMENT_EXTENSIONS = (".pdf", ".docx", ".doc")

_PROACTIVE_RATIONALE_PREFIX = "Proactive:"


def _text_matches_any(text: str, phrases: tuple) -> bool:
    lowered = (text or "").lower()
    return any(p in lowered for p in phrases)


def _idempotency_key(raw_item_id: int, action_type: str) -> str:
    return hashlib.sha256(f"proactive|{raw_item_id}|{action_type}".encode("utf-8")).hexdigest()


def dispatch_contract_review(issue_id: str, raw_item_id: int) -> Optional[int]:
    """Same underlying primitives server_lean.py's own /api/cockpit/actions
    route uses (create_prepared_action + team_room.post_message +
    create_pending_action) - a second CALLER of the one real dispatch
    mechanism, not a second mechanism. state='approved' at creation is
    Marc's standing settings-toggle approval (see module docstring), the
    same shortcut the human-click route takes for its own click.
    Idempotent per raw_item - a second call for the same raw_item is a
    no-op (returns None), same discipline as the click-path's own
    idempotency_key check."""
    idempotency_key = _idempotency_key(raw_item_id, "review_contract")
    if ws.find_prepared_action_by_idempotency_key(idempotency_key) is not None:
        return None
    prepared_id = ws.create_prepared_action(
        claim_id=None, action_type="review_contract",
        proposed_parameters_json=json.dumps({
            "issue_id": issue_id, "action_kind": "review_contract",
            "worker": "bridge", "trigger": "proactive", "raw_item_id": raw_item_id,
        }),
        evidence_refs_json=json.dumps([raw_item_id]),
        rationale=f"{_PROACTIVE_RATIONALE_PREFIX} incoming message asked for a document review",
        risk_class="low", idempotency_key=idempotency_key, state="approved",
    )
    sender = config.get("manager", "id") or "marc"
    envelope = "@bridge [COCKPIT-ACTION] {}".format(json.dumps(
        {"type": "review_contract", "issue_id": issue_id, "instructions": ""}, ensure_ascii=False))
    ws.update_prepared_action_state(prepared_id, "executing")
    try:
        result = team_room.post_message(sender=sender, body=envelope)
    except ValueError as e:
        ws.update_prepared_action_state(prepared_id, "failed", policy_result=str(e))
        return prepared_id
    ws.create_pending_action(issue_id=issue_id, action_kind="review_contract", worker="bridge",
                              instructions="", message_id=result.get("message_id"))
    # Design doc Section 11: message delivery is not itself authoritative
    # execution state. Dispatching to the Team Room only confirms the
    # request reached bridge - not that the review it names was ever
    # actually completed, so "uncertain" (already a valid prepared_actions
    # state), not "succeeded".
    ws.update_prepared_action_state(prepared_id, "uncertain")
    return prepared_id


def _draft_status_update_body(issue_id: str) -> str:
    """Deterministic, not a fresh LLM call - reuses curator's own already-
    written synthesis summary plus the issue's own open asks, same 'real
    narrative from already-extracted content' posture as
    workgraph_suppliers.weekly_scorecard_draft/workgraph_meetingprep."""
    synthesis = ws.get_synthesis("issue", issue_id)
    summary = (synthesis or {}).get("summary") or "No summary available yet - Marc should fill this in."
    open_asks = [c["text"] for c in ws.list_open_claims_for_issue(issue_id, claim_type="ask")]
    lines = [summary]
    if open_asks:
        lines.append("")
        lines.append("Still open on our side:")
        lines.extend(f"- {a}" for a in open_asks[:5])
    return "\n".join(lines)


def dispatch_status_update_draft(issue_id: str, raw_item_id: int, entry_id: str) -> Optional[int]:
    """Drafts a reply and saves it to the Drafts folder via outlook_actions.
    draft_reply(save_only=True) - never Display()s/Send()s it. Idempotent
    per raw_item, same as dispatch_contract_review."""
    idempotency_key = _idempotency_key(raw_item_id, "draft_status_update")
    if ws.find_prepared_action_by_idempotency_key(idempotency_key) is not None:
        return None
    body = _draft_status_update_body(issue_id)
    prepared_id = ws.create_prepared_action(
        claim_id=None, action_type="draft_status_update",
        proposed_parameters_json=json.dumps({
            "issue_id": issue_id, "raw_item_id": raw_item_id, "entry_id": entry_id,
        }),
        evidence_refs_json=json.dumps([raw_item_id]),
        rationale=f"{_PROACTIVE_RATIONALE_PREFIX} incoming message asked for a status update",
        risk_class="low", idempotency_key=idempotency_key, state="approved",
    )
    ws.update_prepared_action_state(prepared_id, "executing")
    try:
        outlook_actions.draft_reply(entry_id, body=body, save_only=True)
    except RuntimeError as e:
        ws.update_prepared_action_state(prepared_id, "failed", policy_result=str(e))
        return prepared_id
    ws.update_prepared_action_state(prepared_id, "succeeded")
    return prepared_id


def check_raw_item_for_proactive_action(raw_item_id: int) -> Optional[str]:
    """Returns the action_type dispatched, or None (including when the
    master toggle is off - a no-op, not an error, so callers never need
    their own separate config check)."""
    if not config.get("proactive_actions", "enabled", default=False):
        return None
    raw_item = ws.get_raw_item(raw_item_id)
    if raw_item is None or raw_item.get("direction") != "inbound":
        return None
    issue_id = raw_item.get("issue_id")
    if issue_id is None or ws.get_issue(issue_id) is None:
        return None  # no issue yet, or still an unpromoted raw cluster - nothing confirmed to act on
    text = f"{raw_item.get('subject') or ''} {raw_item.get('body_preview') or ''}"

    attachments = ws.list_attachments(entity_type="raw_item", entity_id=str(raw_item_id))
    has_contract_attachment = any(
        (a.get("filename") or "").lower().endswith(_CONTRACT_ATTACHMENT_EXTENSIONS) for a in attachments
    )
    if has_contract_attachment and _text_matches_any(text, _CONTRACT_REVIEW_PHRASES):
        dispatch_contract_review(issue_id, raw_item_id)
        return "review_contract"

    if raw_item.get("entry_id") and _text_matches_any(text, _STATUS_UPDATE_PHRASES):
        dispatch_status_update_draft(issue_id, raw_item_id, raw_item["entry_id"])
        return "draft_status_update"

    return None


_CURSOR_SOURCE = "proactive_actions"
_CURSOR_KEY = "max_raw_item_id_checked"


def run_proactive_actions_sweep(limit: int = 200) -> dict:
    """Wired into scheduled_refresh.py. A plain incrementing-id cursor
    (get_cursor/set_cursor), not time-windowed, so a slow or missed tick
    never skips an item - it just catches up further on the next one.
    Returns {"enabled": bool, "checked": int, "dispatched": int} - checked/
    dispatched are 0 (not an error) whenever the master toggle is off."""
    if not config.get("proactive_actions", "enabled", default=False):
        return {"enabled": False, "checked": 0, "dispatched": 0}
    last_checked = int(ws.get_cursor(_CURSOR_SOURCE, _CURSOR_KEY) or 0)
    candidates = ws.list_classified_inbound_raw_items_after_id(last_checked, limit=limit)
    dispatched = 0
    max_id = last_checked
    for raw_item in candidates:
        max_id = max(max_id, raw_item["id"])
        if check_raw_item_for_proactive_action(raw_item["id"]) is not None:
            dispatched += 1
    if max_id != last_checked:
        ws.set_cursor(_CURSOR_SOURCE, _CURSOR_KEY, str(max_id))
    return {"enabled": True, "checked": len(candidates), "dispatched": dispatched}
