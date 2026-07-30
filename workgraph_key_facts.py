"""
workgraph_key_facts.py — enhancement #2 (Key Facts panel): surfaces
raw_item_extractions' `key_facts` field (curator's real LLM judgment, the
same extraction pass as asks/decisions/commitments/dates_mentioned) across
every open issue.

Real data checked before building this: 398 real extraction rows carry at
least one key fact - the largest of the four previously-unsurfaced/
under-surfaced extraction fields. Grepping the whole codebase found
`key_facts` referenced only in the store schema comment, ingest/
SYNTHESIS_ROUTINE.md, and tests - nothing anywhere reads it back out.
This is the cleanest instance of "curator already extracts it, nothing
shows it" this session found. Zero new extraction, zero LLM calls here -
a plain, reflect-only rollup, same shape as workgraph_commitments.py/
workgraph_asks_decisions.py.
"""
from __future__ import annotations

import workgraph_store as ws

_OPEN_STATES = ("active", "waiting", "blocked")


def list_open_key_facts() -> list[dict]:
    """Returns one entry per real extracted key fact across every open
    issue, most-recently-extracted first. Reflect-only."""
    issues = ws.list_issues(states=list(_OPEN_STATES), limit=1000)
    issue_ids = [i["id"] for i in issues]
    extractions_by_issue = ws.list_extractions_for_issues(issue_ids)

    entries = []
    for issue in issues:
        for extraction in extractions_by_issue.get(issue["id"], []):
            facts = (extraction.get("extracted_json") or {}).get("key_facts") or []
            for text in facts:
                if not isinstance(text, str) or not text.strip():
                    continue
                entries.append({
                    "issue_id": issue["id"], "title": issue.get("display_title") or issue["title"],
                    "state": issue["state"], "text": text.strip(),
                    "extracted_ts": extraction["extracted_ts"],
                })
    entries.sort(key=lambda e: e["extracted_ts"], reverse=True)
    return entries


def list_key_facts_for_issue(issue_id: str) -> list[str]:
    """Enhancement #87 (issue detail panel): the same real field, scoped to
    ONE issue's own extractions rather than every open issue - no state
    filter, same reasoning as workgraph_asks_decisions._texts_for_issue."""
    extractions_by_issue = ws.list_extractions_for_issues([issue_id])
    out = []
    for extraction in extractions_by_issue.get(issue_id, []):
        for text in (extraction.get("extracted_json") or {}).get("key_facts") or []:
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
    return out
