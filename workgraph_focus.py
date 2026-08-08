"""
workgraph_focus.py - task #283: the real aggregation behind "what should I
focus on today?" - three sections (urgent actions, today's meeting prep,
deliverables due soon), each reusing an existing, already-correct reader
rather than inventing new scoring. Zero LLM, zero new extraction.
"""
from __future__ import annotations

import time
from typing import Optional

import workgraph_deadlines
import workgraph_meetingprep
import workgraph_nba
import workgraph_store as ws

_OPEN_STATES = ("active", "waiting", "blocked")

# "Today" for meeting prep - the default 48h lookahead
# (workgraph_meetingprep.DEFAULT_LOOKAHEAD_HOURS) is tuned for the standalone
# meeting-prep-candidates surface; a same-day focus view wants just today.
_MEETING_LOOKAHEAD_HOURS = 24.0
_TOP_ACTIONS_LIMIT = 8
# A hard deadline more than a week out belongs in Renewal Radar
# (find_renewal_outreach_candidates' own 30-90 day window), not "today."
_DELIVERABLE_DAYS_OUT = 7.0


def build_focus_today_summary(now: Optional[float] = None) -> dict:
    if now is None:
        now = time.time()

    top_actions = workgraph_nba.rank_actions(limit=_TOP_ACTIONS_LIMIT, now=now)
    meetings_today = workgraph_meetingprep.find_upcoming_meeting_prep_candidates(
        now=now, lookahead_hours=_MEETING_LOOKAHEAD_HOURS,
    )

    issues = ws.list_issues(states=list(_OPEN_STATES), limit=5000)
    workgraph_deadlines.attach_deadline_info(issues, now=now)
    deliverables_due_soon = [
        {
            "issue_id": i["id"], "title": i.get("display_title") or i["title"],
            "days_out": i["due_date_info"]["days_out"],
            "overdue": i["due_date_info"]["overdue"],
            "source": i["due_date_info"]["source"],
        }
        for i in issues
        if i.get("due_date_info") and i["due_date_info"]["days_out"] <= _DELIVERABLE_DAYS_OUT
    ]
    deliverables_due_soon.sort(key=lambda d: d["days_out"])

    return {
        "top_actions": top_actions,
        "meetings_today": meetings_today,
        "deliverables_due_soon": deliverables_due_soon,
    }
