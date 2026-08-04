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
import deep_links

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


_DEADLINE_TYPES = ("renewal_notice", "contract_expiration", "signature_deadline", "sla_cutoff", "other")
RENEWAL_DEADLINE_TYPES = ("renewal_notice", "contract_expiration")


def _normalize_date_mention(entry, raw_item_id: Optional[int] = None) -> Optional[dict]:
    """Accepts either the current {"text": str, "kind": "hard"|"soft",
    "deadline_type": ..., "resolved_date": ...} shape or a legacy plain
    string (written before this classification existed) - returns
    {"text": str, "kind": "hard"|"soft"|None, "raw_item_id": int|None,
    "deadline_type": str|None, "resolved_date": float|None (a real
    epoch timestamp, already parsed - see _parse_due)}, or None if the
    entry is blank/unusable. A malformed kind value normalizes to None
    (unclassified) rather than being silently miscategorized.
    deadline_type/resolved_date (task #141, 2026-08-04) are only ever
    trusted when kind == "hard" - curator is only asked to populate them
    for hard deadlines, so a soft or unclassified entry carrying either
    is treated as malformed input, not a signal to act on. raw_item_id
    (enhancement idea panel #5) is the enclosing extraction's own
    raw_item_id - a real deep-link target the extraction row already
    carries (rie.raw_item_id), never guessed."""
    if isinstance(entry, str):
        text = entry.strip()
        return ({"text": text, "kind": None, "raw_item_id": raw_item_id,
                  "deadline_type": None, "resolved_date": None} if text else None)
    if isinstance(entry, dict):
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        kind = entry.get("kind")
        if kind not in ("hard", "soft"):
            kind = None
        deadline_type = entry.get("deadline_type")
        resolved_date = _parse_due(entry.get("resolved_date"))
        if kind != "hard":
            deadline_type, resolved_date = None, None
        elif deadline_type not in _DEADLINE_TYPES:
            deadline_type = None
        return {"text": text.strip(), "kind": kind, "raw_item_id": raw_item_id,
                 "deadline_type": deadline_type, "resolved_date": resolved_date}
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
                normalized = _normalize_date_mention(raw, raw_item_id=extraction.get("raw_item_id"))
                if normalized:
                    mentions.append(normalized)
        # Enhancement idea panel #5: deep-link each deadline mention to its
        # source email/chat - reuses deep_links.attach_deep_links verbatim
        # (the SAME mechanism evidence rows already get), not a second
        # link-building path, now that each mention carries a real
        # raw_item_id above.
        deep_links.attach_deep_links(mentions)
        issue["deadline_mentions"] = mentions
        issue["has_hard_deadline"] = (
            issue["due_date_info"] is not None or any(m["kind"] == "hard" for m in mentions)
        )

    return issues


DEFAULT_RENEWAL_OUTREACH_WINDOW_DAYS = (30.0, 90.0)


def find_renewal_outreach_candidates(now: Optional[float] = None,
                                      window_days: tuple = DEFAULT_RENEWAL_OUTREACH_WINDOW_DAYS) -> list[dict]:
    """Enhancement idea panel #18 (Renewal-window early-outreach draft,
    worker capability): a real, resolved renewal/contract-expiration date
    (curator-judged `deadline_type`/`resolved_date` on a `kind: "hard"`
    dates_mentioned entry - see SYNTHESIS_ROUTINE.md) falling within the
    outreach window (30-90 days out by default - early enough to matter,
    not so early it's noise). Deliberately requires a real `resolved_date`
    - never guesses a date from free text downstream (see _normalize_
    date_mention's own docstring for why that's the one thing this
    module refuses to do).

    Batched: one issues query, one extractions query across every open
    issue - never a per-issue loop. Returns one entry per (issue,
    mention) - {issue_id, title, deadline_type, resolved_date, days_out,
    text, raw_item_id}, nearest-deadline-first."""
    if now is None:
        now = time.time()
    lo_days, hi_days = window_days
    issues = ws.list_issues(states=list(_OPEN_STATES), limit=2000)
    issue_ids = [i["id"] for i in issues]
    titles = {i["id"]: i["title"] for i in issues}
    extractions_by_issue = ws.list_extractions_for_issues(issue_ids)

    candidates = []
    for issue_id in issue_ids:
        for extraction in extractions_by_issue.get(issue_id, []):
            for raw in (extraction.get("extracted_json") or {}).get("dates_mentioned") or []:
                mention = _normalize_date_mention(raw, raw_item_id=extraction.get("raw_item_id"))
                if not mention or mention["deadline_type"] not in RENEWAL_DEADLINE_TYPES:
                    continue
                if mention["resolved_date"] is None:
                    continue
                days_out = (mention["resolved_date"] - now) / DAY
                if not (lo_days <= days_out <= hi_days):
                    continue
                candidates.append({
                    "issue_id": issue_id, "title": titles.get(issue_id),
                    "deadline_type": mention["deadline_type"], "resolved_date": mention["resolved_date"],
                    "days_out": round(days_out, 1), "text": mention["text"],
                    "raw_item_id": mention["raw_item_id"],
                })
    candidates.sort(key=lambda c: c["days_out"])
    return candidates


def renewal_outreach_draft(issue_id: str, now: Optional[float] = None) -> Optional[dict]:
    """The actual draft content for ONE renewal candidate - recipient,
    subject, and body text, built from real data (the issue's own
    external party, the resolved deadline, the issue's title/category).
    A genuine DRAFT: this returns DATA for a human to review and send
    themselves (same posture as weekly_scorecard_draft/draft_reply/
    draft_forward) - it never touches Outlook and never sends anything.
    No live Outlook 'compose new mail' action exists yet (task #35 is
    still pending) to wire this into a real draft window; this is the
    content that action would use once it does.

    Returns None if this issue has no renewal candidate within the
    window, or no identifiable external party to address the draft to
    (nothing to draft without a real recipient)."""
    candidates = [c for c in find_renewal_outreach_candidates(now=now) if c["issue_id"] == issue_id]
    if not candidates:
        return None
    candidate = candidates[0]

    parties = ws.list_parties_for_issue(issue_id)
    external = [p for p in parties if p.get("affiliation") == "external" and p.get("primary_email")]
    if not external:
        return None
    recipient = external[0]

    deadline_label = "renewal notice" if candidate["deadline_type"] == "renewal_notice" else "contract expiration"
    days_out = candidate["days_out"]
    subject = f"Renewal timing — {candidate['title'] or 'upcoming deadline'}"
    greeting_name = recipient.get("display_name") or "there"
    body_lines = [
        f"Hi {greeting_name},",
        "",
        f"Flagging early that the {deadline_label} date on this is coming up "
        f"({int(round(days_out))} days out) — wanted to get ahead of it rather than "
        f"deal with it at the last minute.",
        "",
        f"Context from our records: \"{candidate['text']}\"",
        "",
        "Can we get some time on the calendar to align on next steps before that date?",
        "",
        "Thanks,",
        "Marc",
    ]
    return {
        "issue_id": issue_id, "recipient_email": recipient["primary_email"],
        "recipient_name": recipient.get("display_name"), "subject": subject,
        "body": "\n".join(body_lines), "deadline_type": candidate["deadline_type"],
        "resolved_date": candidate["resolved_date"], "days_out": days_out,
    }
