"""
workgraph_meetingprep.py — enhancement idea panel #20 (Meeting-prep
auto-draft, worker capability): a deterministic, zero-LLM prep briefing
for an issue's next upcoming calendar meeting, built entirely from real
data already ingested - open claims (asks/decisions/commitments), key
facts, deadline info, and real attendee/agenda data (enhancement idea
panel #7's richer calendar ingestion). Same "narrative built from real
numbers" posture as workgraph_suppliers.weekly_scorecard_draft (E15). A
genuine DRAFT: Marc reviews before the meeting; nothing here is ever
auto-sent or auto-surfaced as settled fact.
"""
from __future__ import annotations

import time
from typing import Optional

import workgraph_store as ws
import workgraph_claims
import workgraph_key_facts
import workgraph_deadlines

_OPEN_STATES = ("active", "waiting", "blocked")
DEFAULT_LOOKAHEAD_HOURS = 48.0


def find_upcoming_meeting_prep_candidates(now: Optional[float] = None,
                                           lookahead_hours: float = DEFAULT_LOOKAHEAD_HOURS) -> list[dict]:
    """Batched: one issues query, one raw_items query across every open
    issue - never a per-issue loop. Returns one entry per open issue
    that has a real calendar-source raw_item landing within the
    lookahead window - {issue_id, title, meeting_raw_item_id, subject,
    occurred_ts, hours_out}, soonest-first."""
    if now is None:
        now = time.time()
    issues = ws.list_issues(states=list(_OPEN_STATES), limit=2000)
    issue_ids = [i["id"] for i in issues]
    titles = {i["id"]: i["title"] for i in issues}
    raw_items_by_issue = ws.get_raw_items_for_issues(issue_ids)

    cutoff = now + lookahead_hours * 3600.0
    candidates = []
    for issue_id, items in raw_items_by_issue.items():
        upcoming = [
            item for item in items
            if item.get("source") == "calendar" and item.get("occurred_ts")
            and now < item["occurred_ts"] <= cutoff
        ]
        if not upcoming:
            continue
        nearest = min(upcoming, key=lambda i: i["occurred_ts"])
        candidates.append({
            "issue_id": issue_id, "title": titles.get(issue_id),
            "meeting_raw_item_id": nearest["id"], "subject": nearest.get("subject"),
            "occurred_ts": nearest["occurred_ts"],
            "hours_out": round((nearest["occurred_ts"] - now) / 3600.0, 1),
        })
    candidates.sort(key=lambda c: c["occurred_ts"])
    return candidates


def _attendee_names(attendees) -> str:
    names = []
    for a in attendees[:8]:
        if isinstance(a, dict):
            names.append(a.get("name") or a.get("email") or "unknown")
        else:
            names.append(str(a))
    return ", ".join(names)


def meeting_prep_draft(issue_id: str, now: Optional[float] = None) -> Optional[dict]:
    """The actual prep content for ONE issue's nearest upcoming meeting -
    a narrative built entirely from real, already-ingested data. Returns
    None if this issue has no upcoming calendar meeting within the
    default lookahead window."""
    if now is None:
        now = time.time()
    candidates = [c for c in find_upcoming_meeting_prep_candidates(now=now) if c["issue_id"] == issue_id]
    if not candidates:
        return None
    candidate = candidates[0]

    meetings = ws.list_calendar_meetings_for_issue(issue_id)
    meeting = next((m for m in meetings if m["raw_item_id"] == candidate["meeting_raw_item_id"]), None)

    open_asks = workgraph_claims.list_open_claims_for_issue(issue_id, claim_type="ask")
    open_decisions = workgraph_claims.list_open_claims_for_issue(issue_id, claim_type="decision")
    open_commitments = workgraph_claims.list_open_claims_for_issue(issue_id, claim_type="commitment")
    key_facts = workgraph_key_facts.list_key_facts_for_issue(issue_id)

    issue = ws.get_issue(issue_id)
    issues_for_deadline = [issue] if issue else []
    workgraph_deadlines.attach_deadline_info(issues_for_deadline, now=now)
    has_hard_deadline = bool(issues_for_deadline and issues_for_deadline[0].get("has_hard_deadline"))

    lines = [f"Prep for: {candidate['subject'] or candidate['title']} ({candidate['hours_out']}h out)"]
    if meeting:
        attendees = meeting.get("attendees_detailed") or meeting.get("participants") or []
        if attendees:
            lines.append(f"Attendees: {_attendee_names(attendees)}")
        agenda = meeting.get("full_agenda_text")
        if agenda:
            lines.append(f"Agenda: {agenda[:500]}")

    if open_asks:
        lines.append(f"- {len(open_asks)} open ask(s) to raise: " + "; ".join(a["text"] for a in open_asks[:5]))
    if open_decisions:
        lines.append(f"- {len(open_decisions)} open decision(s) pending: "
                      + "; ".join(d["text"] for d in open_decisions[:5]))
    if open_commitments:
        lines.append(f"- {len(open_commitments)} open commitment(s): "
                      + "; ".join(c["text"] for c in open_commitments[:5]))
    if has_hard_deadline:
        lines.append("- this issue carries a hard deadline - confirm status before/during the meeting")
    if key_facts:
        lines.append("Key facts: " + "; ".join(key_facts[:5]))
    if not (open_asks or open_decisions or open_commitments or key_facts):
        lines.append("- no open items or key facts on record for this issue yet")

    return {
        "issue_id": issue_id, "meeting_raw_item_id": candidate["meeting_raw_item_id"],
        "subject": candidate["subject"], "occurred_ts": candidate["occurred_ts"],
        "hours_out": candidate["hours_out"], "narrative": "\n".join(lines),
        "open_ask_count": len(open_asks), "open_decision_count": len(open_decisions),
        "open_commitment_count": len(open_commitments), "key_fact_count": len(key_facts),
        "has_hard_deadline": has_hard_deadline,
    }
