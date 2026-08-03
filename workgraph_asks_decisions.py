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
            # Fixed 2026-07-30 (hardening pass #3): a malformed extraction
            # row (e.g. a non-list value for this field) must not crash this
            # whole rollup or iterate characters of a stray string - treat
            # anything that isn't really a list as "no entries", not a guess.
            if not isinstance(values, list):
                continue
            for text in values:
                if not isinstance(text, str) or not text.strip():
                    continue
                entries.append({
                    "issue_id": issue["id"], "title": issue.get("display_title") or issue["title"],
                    "state": issue["state"], "text": text.strip(),
                    "extracted_ts": extraction["extracted_ts"],
                    "raw_item_id": extraction.get("raw_item_id"),
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


def _texts_for_issues(issue_ids: list[str], field_name: str) -> dict[str, list[dict]]:
    """Batched form of _texts_for_issue below - one list_extractions_for_issues
    call across every issue instead of one per issue (real bug found 2026-08-02:
    api_project_detail was calling the singular list_asks_for_issue/
    list_decisions_for_issue/etc. once PER MEMBER ISSUE, each independently
    re-querying the same underlying raw_item_extractions table despite
    list_extractions_for_issues already being the batched-safe primitive - a
    real, measured multi-second latency on every project-detail load/action
    for even a 4-issue project, confirmed live via curl timing before this
    fix. _texts_for_issue itself now just calls this with a single-element
    list, so its own behavior/callers are unchanged."""
    extractions_by_issue = ws.list_extractions_for_issues(issue_ids)
    out: dict[str, list[dict]] = {}
    for iid in issue_ids:
        texts = []
        for extraction in extractions_by_issue.get(iid, []):
            values = (extraction.get("extracted_json") or {}).get(field_name) or []
            if not isinstance(values, list):
                continue
            for text in values:
                if isinstance(text, str) and text.strip():
                    texts.append({"text": text.strip(), "raw_item_id": extraction.get("raw_item_id")})
        out[iid] = texts
    return out


def _texts_for_issue(issue_id: str, field_name: str) -> list[dict]:
    """Enhancement #87 (issue detail panel): the same real field, scoped to
    ONE issue's own extractions rather than every open issue - no state
    filter, since Marc looking at a specific issue (open or closed) should
    still see what was actually asked/decided on it. Returns {text,
    raw_item_id} (2026-08-01, checklist rework) rather than a bare string -
    raw_item_id is what lets a caller attach the real deep link (Ariba/Adobe
    Sign/Outlook) the source email already carries, via deep_links.
    attach_deep_links, instead of this ask ever floating free of its
    source."""
    return _texts_for_issues([issue_id], field_name).get(issue_id, [])


def list_asks_for_issue(issue_id: str) -> list[dict]:
    return _texts_for_issue(issue_id, "asks")


def list_decisions_for_issue(issue_id: str) -> list[dict]:
    return _texts_for_issue(issue_id, "decisions")


def list_asks_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched sibling for api_project_detail - see _texts_for_issues."""
    return _texts_for_issues(issue_ids, "asks")


def list_decisions_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched sibling for api_project_detail - see _texts_for_issues."""
    return _texts_for_issues(issue_ids, "decisions")
