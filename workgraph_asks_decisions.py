"""
workgraph_asks_decisions.py — Asks & Decisions Tracker. Surfaces open `ask`
and `decision` claims (design doc Section 9, Phase 3) across every open
issue.

Rewritten 2026-08-03 (Section 9.8) to read the materialized `claims` table
instead of raw_item_extractions' `asks`/`decisions` fields directly - same
public functions, same return shapes, so every existing caller is
unaffected. What changed underneath: a restated ask now updates its
existing open claim instead of appearing a second time (Section 9.3's
repeat_signals-driven dedup), and each claim carries a real `author`/
`owner` (Section 9.4) - not surfaced by this module's return shape, but
queryable directly via workgraph_claims for any caller that wants it.
"""
from __future__ import annotations

import workgraph_store as ws

_OPEN_STATES = ("active", "waiting", "blocked")


def _rollup(claim_type: str) -> list[dict]:
    issues = ws.list_issues(states=list(_OPEN_STATES), limit=1000)
    issue_ids = [i["id"] for i in issues]
    claims_by_issue = ws.list_open_claims_for_issues(issue_ids, claim_type=claim_type)

    entries = []
    for issue in issues:
        for claim in claims_by_issue.get(issue["id"], []):
            entries.append({
                "issue_id": issue["id"], "title": issue.get("display_title") or issue["title"],
                "state": issue["state"], "text": claim["text"],
                "extracted_ts": claim["first_seen_ts"],
                "raw_item_id": claim.get("raw_item_id"),
            })
    entries.sort(key=lambda e: e["extracted_ts"], reverse=True)
    return entries


def list_open_asks() -> list[dict]:
    """Returns one entry per open ask claim across every open issue,
    most-recently-seen first. Reflect-only."""
    return _rollup("ask")


def list_open_decisions() -> list[dict]:
    """Same, for `decision` claims."""
    return _rollup("decision")


def _texts_for_issues(issue_ids: list[str], claim_type: str) -> dict[str, list[dict]]:
    """Batched form of _texts_for_issue below - one list_open_claims_for_issues
    call across every issue instead of one per issue (same N+1 fix this
    module already applied once for the old extraction-blob path)."""
    claims_by_issue = ws.list_open_claims_for_issues(issue_ids, claim_type=claim_type)
    return {
        iid: [{"text": c["text"], "raw_item_id": c.get("raw_item_id")} for c in claims_by_issue.get(iid, [])]
        for iid in issue_ids
    }


def _texts_for_issue(issue_id: str, claim_type: str) -> list[dict]:
    """The same real claim type, scoped to ONE issue - no state filter,
    since Marc looking at a specific issue (open or closed) should still
    see what was actually asked/decided on it. Returns {text, raw_item_id}
    so a caller can attach the source email's real deep link."""
    return _texts_for_issues([issue_id], claim_type).get(issue_id, [])


def list_asks_for_issue(issue_id: str) -> list[dict]:
    return _texts_for_issue(issue_id, "ask")


def list_decisions_for_issue(issue_id: str) -> list[dict]:
    return _texts_for_issue(issue_id, "decision")


def list_asks_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched sibling for api_project_detail - see _texts_for_issues."""
    return _texts_for_issues(issue_ids, "ask")


def list_decisions_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched sibling for api_project_detail - see _texts_for_issues."""
    return _texts_for_issues(issue_ids, "decision")
