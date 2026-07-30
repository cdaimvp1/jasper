"""
workgraph_deadlines.py — task #64/#80: deadline info attached to each
issue's own detail, not a standalone panel (Marc's direct feedback on the
first version: a separate "Deadline Radar" card was the wrong shape -
this belongs on the issue/project it's about, and needs to be filterable
in the inbox).

Two sources, kept structurally distinct:
  due_date_info: issues.due (when set) or a linked upcoming calendar
    event (<=CALENDAR_LOOKAHEAD_DAYS out, the same window workgraph_nba.py/
    workgraph_recommend.py already trust) - both real timestamps, treated
    as inherently "hard" (a due date or a scheduled meeting is a fact, not
    a guess).
  deadline_mentions: raw_item_extractions' dates_mentioned field - real
    LLM judgment from curator's synthesis routine (SYNTHESIS_ROUTINE.md),
    now classified per-mention as "hard" (a real, binding date with a
    named consequence - contract must-sign-by, notice-of-non-renewal/
    termination, an SLA cutoff) or "soft" (aspirational - "shooting for
    next week"). Entries written before this classification existed are
    plain strings and normalize to kind=None ("unclassified") rather than
    being guessed at retroactively by a keyword filter - checked before
    building the original version of this feature: keyword-guessing
    hard/soft from already-summarized free text would repeat exactly the
    failure mode that made the Ariba expiration-date signal wrong ~98% of
    the time (task #61/#72). The classification only happens where a real
    reader (curator) already has the actual email in front of them.

has_hard_deadline is True when either a real due_date_info exists OR any
mention is explicitly kind="hard" - the flag the Morning Queue filters/
sorts by.
"""
from __future__ import annotations

import datetime
import time
from typing import Optional

import workgraph_store as ws

CALENDAR_LOOKAHEAD_DAYS = 14.0  # matches workgraph_nba.py / workgraph_recommend.py
DAY = 86400.0
_OPEN_STATES = ("active", "waiting", "blocked")


def _parse_due(due_iso: Optional[str]) -> Optional[float]:
    """Returns a UTC epoch timestamp, or None if unset/unparseable - a
    malformed due string must never crash this, just be treated as "no
    due date" (same discipline, same fix, as workgraph_nba._due_urgency:
    a naive datetime's .timestamp() assumes LOCAL time while `now` is a
    UTC epoch - explicit UTC attachment removes that ambient-timezone
    drift)."""
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


def _normalize_date_mention(entry) -> Optional[dict]:
    """Accepts either the current {"text": str, "kind": "hard"|"soft"}
    shape or a legacy plain string (written before this classification
    existed) - returns {"text": str, "kind": "hard"|"soft"|None}, or None
    if the entry is blank/unusable. A malformed kind value normalizes to
    None (unclassified) rather than being silently miscategorized."""
    if isinstance(entry, str):
        text = entry.strip()
        return {"text": text, "kind": None} if text else None
    if isinstance(entry, dict):
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        kind = entry.get("kind")
        if kind not in ("hard", "soft"):
            kind = None
        return {"text": text.strip(), "kind": kind}
    return None


def attach_deadline_info(issues: list[dict], now: Optional[float] = None) -> list[dict]:
    """Mutates and returns `issues`: adds `due_date_info` (dict or None),
    `deadline_mentions` (list of {"text","kind"}), and `has_hard_deadline`
    (bool) to each. Computed at READ time - same pattern as workgraph_
    lessons.attach_learned / workgraph_recommend.attach_recommendations -
    so this stays current without a separate write path to keep in sync."""
    if not issues:
        return issues
    if now is None:
        now = time.time()
    issue_ids = [i["id"] for i in issues]
    evidence_by_issue = ws.list_evidence_for_issues(issue_ids)
    extractions_by_issue = ws.list_extractions_for_issues(issue_ids)

    for issue in issues:
        due_ts = _parse_due(issue.get("due"))
        source = "due_date" if due_ts is not None else None
        if due_ts is None:
            cal_ts = _nearest_upcoming_calendar_ts(evidence_by_issue.get(issue["id"], []), now)
            if cal_ts is not None and (cal_ts - now) / DAY <= CALENDAR_LOOKAHEAD_DAYS:
                due_ts, source = cal_ts, "calendar"

        if due_ts is not None:
            days_out = (due_ts - now) / DAY
            issue["due_date_info"] = {"days_out": round(days_out, 1), "overdue": days_out < 0, "source": source}
        else:
            issue["due_date_info"] = None

        mentions = []
        for extraction in extractions_by_issue.get(issue["id"], []):
            for raw in (extraction.get("extracted_json") or {}).get("dates_mentioned") or []:
                normalized = _normalize_date_mention(raw)
                if normalized:
                    mentions.append(normalized)
        issue["deadline_mentions"] = mentions
        issue["has_hard_deadline"] = (
            issue["due_date_info"] is not None or any(m["kind"] == "hard" for m in mentions)
        )

    return issues
