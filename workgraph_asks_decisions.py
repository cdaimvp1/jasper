"""
workgraph_asks_decisions.py — enhancement #2 (Asks & Decisions Tracker):
surfaces raw_item_extractions' `asks` and `decisions` fields (curator's
real LLM judgment, the same extraction pass that produces commitments -
workgraph_commitments.py, task #73 - and dates_mentioned - workgraph_
deadlines.py, task #64) across every open issue.

Real data checked before building this: 205 real extraction rows carry at
least one ask, 54 carry at least one decision - genuine, substantial
content curator already writes on every wake. Grepping the whole codebase
found exactly one place either field is ever read: workgraph_synthesis.py
checks `asks`/`decisions` for plain truthiness to decide whether a
synthesis needs updating - neither field is ever actually displayed
anywhere. This module closes that gap the same way workgraph_commitments.py
already did for `commitments`: a plain, reflect-only rollup, zero new
extraction, zero LLM calls here.
"""
from __future__ import annotations

import workgraph_store as ws

_OPEN_STATES = ("active", "waiting", "blocked")


def _rollup(field_name: str) -> list[dict]:
    issues = ws.list_issues(states=list(_OPEN_STATES), limit=1000)
    issue_ids = [i["id"] for i in issues]
    extractions_by_issue = ws.list_extractions_for_issues(issue_ids)

    entries = []
    for issue in issues:
        for extraction in extractions_by_issue.get(issue["id"], []):
            values = (extraction.get("extracted_json") or {}).get(field_name) or []
            for text in values:
                if not isinstance(text, str) or not text.strip():
                    continue
                entries.append({
                    "issue_id": issue["id"], "title": issue.get("display_title") or issue["title"],
                    "state": issue["state"], "text": text.strip(),
                    "extracted_ts": extraction["extracted_ts"],
                })
    entries.sort(key=lambda e: e["extracted_ts"], reverse=True)
    return entries


def list_open_asks() -> list[dict]:
    """Returns one entry per real extracted ask across every open issue,
    most-recently-extracted first. Reflect-only."""
    return _rollup("asks")


def list_open_decisions() -> list[dict]:
    """Same, for the `decisions` field."""
    return _rollup("decisions")


def _texts_for_issue(issue_id: str, field_name: str) -> list[str]:
    """Enhancement #87 (issue detail panel): the same real field, scoped to
    ONE issue's own extractions rather than every open issue - no state
    filter, since Marc looking at a specific issue (open or closed) should
    still see what was actually asked/decided on it."""
    extractions_by_issue = ws.list_extractions_for_issues([issue_id])
    out = []
    for extraction in extractions_by_issue.get(issue_id, []):
        for text in (extraction.get("extracted_json") or {}).get(field_name) or []:
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
    return out


def list_asks_for_issue(issue_id: str) -> list[str]:
    return _texts_for_issue(issue_id, "asks")


def list_decisions_for_issue(issue_id: str) -> list[str]:
    return _texts_for_issue(issue_id, "decisions")
