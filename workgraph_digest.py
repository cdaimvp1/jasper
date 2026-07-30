"""
workgraph_digest.py — task #76: Weekly Digest. A single rollup of what
changed and what's outstanding over the last 7 days, built entirely from
data this app already computes elsewhere (workgraph_nba's priority
scoring and value extraction, workgraph_deadlines' hard/soft
classification) - no new extraction, no LLM, just a weekly-scoped view
over already-real numbers. Read-only; never writes anything.
"""
from __future__ import annotations

import time
from typing import Optional

import workgraph_deadlines
import workgraph_nba
import workgraph_store as ws

WEEK_SECONDS = 7 * 86400.0
_OPEN_STATES = ("active", "waiting", "blocked")


def _slim(issue: dict) -> dict:
    return {
        "id": issue["id"], "title": issue.get("display_title") or issue["title"],
        "state": issue["state"], "priority_score": issue.get("priority_score"),
    }


def build_digest(now: Optional[float] = None) -> dict:
    if now is None:
        now = time.time()
    since = now - WEEK_SECONDS

    all_issues = ws.list_issues(states=None, limit=10000)
    opened_this_week = [i for i in all_issues if (i.get("opened_at") or 0) >= since]
    closed_this_week = [i for i in all_issues if i["state"] == "done" and (i.get("updated_at") or 0) >= since]

    open_issues = [i for i in all_issues if i["state"] in _OPEN_STATES]
    workgraph_deadlines.attach_deadline_info(open_issues, now)
    hard_deadline_issues = [i for i in open_issues if i.get("has_hard_deadline")]

    top_priority = sorted(open_issues, key=lambda i: i.get("priority_score") or 0.0, reverse=True)[:5]
    rollup = workgraph_nba.value_at_risk_rollup()

    return {
        "as_of": now,
        "since": since,
        "opened_count": len(opened_this_week),
        "closed_count": len(closed_this_week),
        "open_count": len(open_issues),
        "hard_deadline_count": len(hard_deadline_issues),
        "value_found_total": rollup["total"],
        "top_priority": [_slim(i) for i in top_priority],
        "closed_this_week": [_slim(i) for i in closed_this_week],
        "hard_deadline_issues": [_slim(i) for i in hard_deadline_issues[:10]],
    }
