"""
workgraph_reconcile.py — claim-resolution suggestions (2026-08-04, P1,
architecture-review follow-up, task #155).

The claims ledger materializes and dedupes correctly now (workgraph_
claims.py), but nothing in it ever proposes that an open claim IS
resolved - a checklist item only ever closes via a direct human action
(sync_checklist_action_to_claim). This module is a suggest-only layer on
top of that: it never changes a claim's status itself except through an
EXPLICIT human confirm (confirm_claim_suggestion) - every suggestion sits
in pending_claim_suggestions until reviewed, same shape as
pending_project_suggestions' merge/link queue.

Deliberately exactly two evidence types, both a closed enum on the
pending_claim_suggestions.evidence_type CHECK constraint - no fuzzy/
heuristic "this claim is probably done" scoring of any kind:

  explicit_resolution_signal (suggestion_kind='resolve') - the SAME
    curator-extraction step that already produces repeat_signals
    (SYNTHESIS_ROUTINE.md) also produces resolution_signals: populated
    ONLY when a raw_item's own content directly and unambiguously states
    that a SPECIFIC earlier open claim on this issue was fulfilled - "the
    signed SOW you asked for is attached," a clear "Approved." reply to a
    named ask. Never a guess, same discipline repeat_signals already
    established. generate_resolution_signal_suggestions turns each into
    one suggestion against the exact open claim it names (matched by
    byte-exact text, same find_open_claim_by_text used throughout the
    claims ledger - no fuzzy pairing here either).

  issue_closed_with_open_claims (suggestion_kind='contradiction') - a
    deliberate CONTRADICTION signal, never a completion inference: an
    issue moving to a closed state (done/dismissed/noise-archived) is
    NEVER treated as evidence that its still-open claims got resolved (a
    real human decision to close an issue can easily leave loose ends,
    and inferring completion from it would silently mark real
    outstanding asks/commitments as done with no evidence they actually
    happened). detect_issue_closed_with_open_claims_contradictions
    surfaces the mismatch instead, for a human to reconcile - confirming
    this kind of suggestion only acknowledges the mismatch; it never
    touches the claim.

Both paths dedupe against any existing PENDING suggestion for the same
(claim_id, evidence_type) pair (workgraph_store.create_claim_suggestion) -
re-running either sweep never produces a duplicate pending suggestion for
a claim already flagged.
"""
from __future__ import annotations

from typing import Optional

import workgraph_store as ws

_RESOLUTION_SIGNAL_CLAIM_TYPES = ("ask", "decision", "commitment")

_ISSUE_CLOSED_STATES = ("done", "dismissed", "noise-archived")


def generate_resolution_signal_suggestions(raw_item_id: int) -> int:
    """Reads this raw_item's extraction for resolution_signals (curator-
    judged, same shape/discipline as repeat_signals) and turns each into
    a suggest-only 'resolve' suggestion against the specific open claim
    it names. A signal that doesn't match a currently-open claim (already
    resolved by the time this runs, or a bad match) is silently skipped -
    never an error, since there's nothing to suggest. Returns the number
    of signals that matched a real open claim (a signal that hits an
    already-pending duplicate still counts - the suggestion queue is
    correct either way, this return value is a processing count, not a
    strict new-rows count)."""
    raw_item = ws.get_raw_item(raw_item_id)
    if not raw_item or not raw_item.get("issue_id"):
        return 0
    issue_id = raw_item["issue_id"]

    extraction = ws.get_extraction(raw_item_id)
    if not extraction:
        return 0
    blob = extraction.get("extracted_json") or {}
    signals = blob.get("resolution_signals")
    if not isinstance(signals, list):
        return 0

    matched = 0
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        claim_type = signal.get("claim_type")
        claim_text = signal.get("claim_text")
        if claim_type not in _RESOLUTION_SIGNAL_CLAIM_TYPES or not claim_text:
            continue
        claim = ws.find_open_claim_by_text(issue_id, claim_type, claim_text)
        if claim is None:
            continue
        ws.create_claim_suggestion(
            claim_id=claim["id"], suggestion_kind="resolve",
            evidence_type="explicit_resolution_signal",
            evidence_note=signal.get("resolution_note"), raw_item_id=raw_item_id,
        )
        matched += 1
    return matched


def detect_issue_closed_with_open_claims_contradictions() -> dict:
    """Batched sweep - one query for closed issue ids, one batched query
    for their open claims (list_open_claims_for_issues), never a
    per-issue query in a loop. Safe to re-run: dedup is per (claim_id,
    evidence_type), so an issue that's been closed for weeks with the
    same still-open claim only ever accumulates ONE pending
    suggestion."""
    closed_issue_ids = ws.list_issue_ids_by_state(list(_ISSUE_CLOSED_STATES))
    if not closed_issue_ids:
        return {"issues_scanned": 0, "suggestions_created": 0}
    open_by_issue = ws.list_open_claims_for_issues(closed_issue_ids)
    flagged = 0
    for issue_id, claims in open_by_issue.items():
        for claim in claims:
            ws.create_claim_suggestion(
                claim_id=claim["id"], suggestion_kind="contradiction",
                evidence_type="issue_closed_with_open_claims",
                evidence_note=f"issue {issue_id} is closed but this claim is still open",
            )
            flagged += 1
    return {"issues_scanned": len(closed_issue_ids), "suggestions_created": flagged}


def list_pending_claim_suggestions_for_issue(issue_id: str) -> list[dict]:
    return ws.list_pending_claim_suggestions(issue_id=issue_id)


def confirm_claim_suggestion(suggestion_id: int, *, actor: str) -> bool:
    """The ONLY path in this module that changes a claim's status, and
    only on an explicit human confirm of a 'resolve' suggestion.
    Confirming a 'contradiction' suggestion just acknowledges the
    mismatch - it deliberately never marks the claim done, since an
    issue closing is not evidence the claim was actually fulfilled (see
    module docstring); a human who wants that claim closed does so
    through the normal checklist action instead. Returns False if the
    suggestion doesn't exist or was already resolved."""
    suggestion = ws.get_claim_suggestion(suggestion_id)
    if suggestion is None or suggestion["status"] != "pending":
        return False
    ws.resolve_claim_suggestion(suggestion_id, "confirmed")
    if suggestion["suggestion_kind"] == "resolve":
        ws.update_claim_status(suggestion["claim_id"], "done", actor=actor)
        ws.log_claim_event(
            suggestion["claim_id"], "complete", actor=actor,
            note="confirmed via claim-resolution suggestion",
            raw_item_id=suggestion.get("raw_item_id"),
        )
    return True


def reject_claim_suggestion(suggestion_id: int, *, actor: str) -> bool:
    suggestion = ws.get_claim_suggestion(suggestion_id)
    if suggestion is None or suggestion["status"] != "pending":
        return False
    ws.resolve_claim_suggestion(suggestion_id, "rejected")
    return True
