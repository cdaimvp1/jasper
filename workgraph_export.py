"""
workgraph_export.py — task #68: CSV export of a date-ranged issue list.
Deterministic, zero LLM - a plain field dump of workgraph_store.list_issues
for whatever date range/state filter the caller asks for, so Marc can pull
a slice into Excel/Sheets for offline review or sharing. Dates filter on
issues.updated_at (the same "when did this last move" field the Morning
Queue's own staleness/age display already uses), inclusive on both ends.
"""
from __future__ import annotations

import csv
import io
from typing import Optional

import workgraph_store as ws

COLUMNS = [
    "id", "title", "state", "category", "priority", "confidence_tier",
    "priority_score", "nba_reason", "has_unmet_prerequisite", "due", "owner",
    "project_id", "external_companies", "opened_at", "updated_at",
]


def issues_csv(start_ts: float, end_ts: float, states: Optional[list[str]] = None) -> str:
    """Returns CSV text (header + one row per matching issue), ordered by
    updated_at ASC (oldest-first - a natural "what happened over this
    period, in order" read). start_ts/end_ts are UTC epoch bounds,
    inclusive on both ends. states=None (the default) means every state,
    matching workgraph_store.list_issues' own "no filter" semantics."""
    issues = ws.list_issues(states=states, limit=10000)
    matching = [i for i in issues if start_ts <= i["updated_at"] <= end_ts]
    matching.sort(key=lambda i: i["updated_at"])

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for issue in matching:
        writer.writerow({col: issue.get(col, "") for col in COLUMNS})
    return buf.getvalue()
