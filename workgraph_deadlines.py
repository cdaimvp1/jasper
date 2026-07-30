"""
workgraph_deadlines.py — task #64: "Deadline Radar". Explicitly excludes
Ariba's expiration-date signal (ariba_wo_expiration) - task #61/#72's
research found it wrong ~98% of the time when auto-parsed and trusted as a
real deadline. Rather than trying to fix that one signal's accuracy, this
module applies the lesson structurally: deadlines are split into two tiers
that are NEVER merged into one falsely-precise sorted list.

  structured: issues with a real due date (issues.due) or a linked
    upcoming calendar event (<=CALENDAR_LOOKAHEAD_DAYS out, the same
    window workgraph_nba.py/workgraph_recommend.py already trust) - both
    machine-set real timestamps, safe to sort by actual day-count
    proximity. Empty today in practice (neither issues.due nor calendar
    ingestion has real data yet in this deployment) but built correctly so
    it fills in automatically as either source gets populated, rather than
    inventing a parser to manufacture data that doesn't exist.
  mentioned: raw_item_extractions' dates_mentioned field - genuine
    LLM-extracted judgment (curator's synthesis routine), but free-form
    natural language ("Tuesday, August 11 - tentative press-release
    date"), not a parsed date. Surfaced verbatim for Marc's own reading,
    sorted only by extraction recency (the one honest ordering available)
    - NEVER auto-parsed into a day-count or used to rank anything, which
    is exactly the mistake that made the Ariba signal unreliable.

Zero LLM calls here - same discipline as workgraph_nba.py/workgraph_
recommend.py/workgraph_aristotle.py. The LLM judgment already happened
once, upstream, when curator wrote dates_mentioned; this module only ever
reads it back out.
"""
from __future__ import annotations

import datetime
from typing import Optional

import workgraph_store as ws

CALENDAR_LOOKAHEAD_DAYS = 14.0  # matches workgraph_nba.py / workgraph_recommend.py
DAY = 86400.0
_OPEN_STATES = ("active", "waiting", "blocked")


def _parse_due(due_iso: Optional[str]) -> Optional[float]:
    """Returns a UTC epoch timestamp, or None if unset/unparseable - a
    malformed due string must never crash the radar, just be treated as
    "no due date" (same discipline, same fix, as workgraph_nba._due_urgency:
    a naive datetime's .timestamp() assumes LOCAL time while `now` is a UTC
    epoch - explicit UTC attachment removes that ambient-timezone drift)."""
    if not due_iso:
        return None
    try:
        due_dt = datetime.datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
        if due_dt.tzinfo is None:
            due_dt = due_dt.replace(tzinfo=datetime.timezone.utc)
        return due_dt.timestamp()
    except (ValueError, TypeError):
        return None


def _nearest_upcoming_calendar_ts(evidence: list[dict], now: float) -> Optional[float]:
    upcoming = [e["ts"] for e in evidence if e.get("type") == "calendar" and e.get("ts") and e["ts"] > now]
    return min(upcoming) if upcoming else None


def build_radar(now: float) -> dict:
    """Returns {"structured": [...], "mentioned": [...]}. Both lists are
    reflect-only - this module never writes anything back, and never
    treats a `mentioned` entry as a real deadline to compute urgency from."""
    issues = ws.list_issues(states=list(_OPEN_STATES), limit=1000)
    issue_ids = [i["id"] for i in issues]
    evidence_by_issue = ws.list_evidence_for_issues(issue_ids)
    extractions_by_issue = ws.list_extractions_for_issues(issue_ids)

    structured = []
    for issue in issues:
        due_ts = _parse_due(issue.get("due"))
        source = "due_date" if due_ts is not None else None
        if due_ts is None:
            cal_ts = _nearest_upcoming_calendar_ts(evidence_by_issue.get(issue["id"], []), now)
            if cal_ts is not None:
                due_ts, source = cal_ts, "calendar"
        if due_ts is None:
            continue
        days_out = (due_ts - now) / DAY
        if source == "calendar" and days_out > CALENDAR_LOOKAHEAD_DAYS:
            continue  # a calendar event further out than the lookahead isn't "radar" material yet
        structured.append({
            "issue_id": issue["id"], "title": issue.get("display_title") or issue["title"],
            "state": issue["state"], "due_ts": due_ts, "days_out": round(days_out, 1),
            "overdue": days_out < 0, "source": source,
        })
    structured.sort(key=lambda r: r["due_ts"])

    mentioned = []
    for issue in issues:
        for extraction in extractions_by_issue.get(issue["id"], []):
            dates = (extraction.get("extracted_json") or {}).get("dates_mentioned") or []
            for text in dates:
                if not isinstance(text, str) or not text.strip():
                    continue
                mentioned.append({
                    "issue_id": issue["id"], "title": issue.get("display_title") or issue["title"],
                    "state": issue["state"], "text": text.strip(),
                    "extracted_ts": extraction["extracted_ts"],
                })
    mentioned.sort(key=lambda r: r["extracted_ts"], reverse=True)

    return {"structured": structured, "mentioned": mentioned}
