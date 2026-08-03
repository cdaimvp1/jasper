"""
workgraph_repeat_signals.py — surfaces every claim (design doc Section 9,
Phase 3) that curator's repeat_signals judgment has touched at least once:
a real restatement of an existing open ask/commitment/decision, per
ingest/SYNTHESIS_ROUTINE.md's dedup discipline (never a guess, omitted
entirely for a normal new one).

Rewritten 2026-08-03 (Section 9.8/9.3) to read the `claims` table instead of
raw_item_extractions' `repeat_signals` field directly - same public
functions, same return shape, so the existing caller (server_lean.py) is
unaffected. What changed underneath: a repeat now updates the SAME claim
row (workgraph_store.touch_claim) instead of writing a second, independent
repeat_signals entry per raw_item - so this module returns one row per
distinct repeated claim (its most recent touch), not one row per historical
occurrence. A claim counts as "touched" when last_seen_ts moved past
first_seen_ts - that only happens via a repeat match (Section 9.3), never on
first materialization.

Deliberately NOT a global rollup card (Marc explicitly removed the top-of-
page rollup cards) - issue-detail-panel-only, same as before.
"""
from __future__ import annotations

import workgraph_store as ws

_REPEATABLE_CLAIM_TYPES = ("ask", "commitment", "decision")


def list_repeat_signals_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched sibling of list_repeat_signals_for_issue - one
    list_open_claims_for_issues call across every issue instead of one per
    issue (same N+1 fix this module already applied once for the old
    extraction-blob path)."""
    if not issue_ids:
        return {}
    claims_by_issue = ws.list_open_claims_for_issues(issue_ids)
    out: dict[str, list[dict]] = {}
    for iid in issue_ids:
        signals_out = []
        for claim in claims_by_issue.get(iid, []):
            if claim["claim_type"] not in _REPEATABLE_CLAIM_TYPES:
                continue
            if claim["last_seen_ts"] <= claim["first_seen_ts"]:
                continue  # never touched as a repeat
            days = (claim["last_seen_ts"] - claim["first_seen_ts"]) / 86400.0
            signals_out.append({
                "ask_text": claim["text"],
                "days_since_first_ask": days,
                "escalated": bool(claim.get("escalated")),
                "escalation_note": claim.get("escalation_note"),
                "extracted_ts": claim["last_seen_ts"],
                "raw_item_id": claim.get("raw_item_id"),
            })
        signals_out.sort(key=lambda e: e["extracted_ts"], reverse=True)
        out[iid] = signals_out
    return out


def list_repeat_signals_for_issue(issue_id: str) -> list[dict]:
    """Every claim on this issue that's been genuinely restated at least
    once. Each entry: {ask_text, days_since_first_ask, escalated,
    escalation_note} - ask_text is the field name regardless of whether the
    underlying claim is an ask, commitment, or decision (kept for backward
    compatibility with every existing caller of this module)."""
    return list_repeat_signals_for_issues([issue_id]).get(issue_id, [])
