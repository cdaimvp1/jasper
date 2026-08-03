"""
workgraph_commitments.py — Commitments Tracker. Surfaces open `commitment`
claims (design doc Section 9, Phase 3) across every open issue.

Rewritten 2026-08-03 (Section 9.8) to read the materialized `claims` table
instead of raw_item_extractions' `commitments` field directly - same public
functions, same return shapes, so every existing caller (server_lean.py,
the checklist rework) is unaffected. What changed underneath: claims are
deduped (a restated commitment updates its existing open claim instead of
appearing twice - Section 9.3) and each row now carries a real `owner`
(Section 9.4) even though this module still surfaces every commitment
verbatim rather than filtering to "Marc's own" - the original reasoning for
that still holds and is worth keeping: real data checked before Section 9.4
existed found only 5 of 79 commitments even mentioning Marc by name, and a
keyword filter would have been exactly the kind of unreliable guess the
Ariba expiration-date signal already showed the cost of. `owner` is now
real and available to any caller that DOES want to filter (claims are
queryable directly via workgraph_claims), but this module keeps its
original, deliberately unfiltered scope.
"""
from __future__ import annotations

import workgraph_store as ws

_OPEN_STATES = ("active", "waiting", "blocked")


def list_open_commitments() -> list[dict]:
    """Returns one entry per open commitment claim across every open issue,
    most-recently-seen first. Reflect-only - never writes anything."""
    issues = ws.list_issues(states=list(_OPEN_STATES), limit=1000)
    issue_ids = [i["id"] for i in issues]
    claims_by_issue = ws.list_open_claims_for_issues(issue_ids, claim_type="commitment")

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


def list_commitments_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched sibling of list_commitments_for_issue - one
    list_open_claims_for_issues call across every issue instead of one per
    issue (same N+1 fix this module already applied once for the old
    extraction-blob path, carried over to the claims-backed path)."""
    claims_by_issue = ws.list_open_claims_for_issues(issue_ids, claim_type="commitment")
    return {
        iid: [{"text": c["text"], "raw_item_id": c.get("raw_item_id")} for c in claims_by_issue.get(iid, [])]
        for iid in issue_ids
    }


def list_commitments_for_issue(issue_id: str) -> list[dict]:
    """Part of the project-detail redesign (2026-07-31): the same real
    claim type, scoped to ONE issue - no state filter, since Marc looking
    at a specific issue (open or closed) should still see what was actually
    committed on it. Returns {text, raw_item_id} so a caller can attach the
    source email's real deep link via deep_links."""
    return list_commitments_for_issues([issue_id]).get(issue_id, [])
