"""
workgraph_commitments.py — task #73: Commitments Tracker. Surfaces
raw_item_extractions' `commitments` field (curator's real LLM judgment,
the same extraction pass that produces dates_mentioned - see
workgraph_deadlines.py, task #64) across every open issue.

Named "Commitments Tracker" rather than a literal "What I Owe" list
because the underlying extraction doesn't attribute WHO made each
commitment - real data was checked before building this: only 5 of 79
real extracted commitments even mention Marc by name, and even those
aren't uniformly "things Marc owes" (some are things others owe TO him,
e.g. "Eversana will include Marc in the process going forward"). A
keyword filter for "Marc"/"I will" would be exactly the kind of
unreliable guess the Ariba expiration-date signal already showed the cost
of (task #61/#72, reused again for #64's "mentioned" tier). So this
surfaces every real commitment mentioned in an open thread, verbatim, and
leaves the "is this mine" judgment to Marc's own reading rather than
pretending a filter can make that call reliably.
"""
from __future__ import annotations

import workgraph_store as ws

_OPEN_STATES = ("active", "waiting", "blocked")


def list_open_commitments() -> list[dict]:
    """Returns one entry per real extracted commitment across every open
    issue, most-recently-extracted first. Reflect-only - never writes
    anything, never guesses ownership."""
    issues = ws.list_issues(states=list(_OPEN_STATES), limit=1000)
    issue_ids = [i["id"] for i in issues]
    extractions_by_issue = ws.list_extractions_for_issues(issue_ids)

    entries = []
    for issue in issues:
        for extraction in extractions_by_issue.get(issue["id"], []):
            commitments = (extraction.get("extracted_json") or {}).get("commitments") or []
            for text in commitments:
                if not isinstance(text, str) or not text.strip():
                    continue
                entries.append({
                    "issue_id": issue["id"], "title": issue.get("display_title") or issue["title"],
                    "state": issue["state"], "text": text.strip(),
                    "extracted_ts": extraction["extracted_ts"],
                })
    entries.sort(key=lambda e: e["extracted_ts"], reverse=True)
    return entries
