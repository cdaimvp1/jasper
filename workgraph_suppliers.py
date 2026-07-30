"""
workgraph_suppliers.py — task #75: Supplier Relationship Dashboard. Groups
issues by external party company (the same "supplier" grouping key
Aristotle's match_on='supplier' rules and workgraph_store.
list_issues_for_company already use), so Marc can see a whole
relationship's shape at a glance - how many open items, how much dollar
value is in play, how recently anything moved, whether anything has a
hard deadline - instead of piecing it together issue by issue.

Zero LLM. Every number here is a live reflection of already-real data
(parties, issues, workgraph_nba's own dollar-value extraction,
workgraph_deadlines' own hard/soft classification) - nothing new is
computed or guessed, just grouped and summed.
"""
from __future__ import annotations

from typing import Optional

import workgraph_deadlines
import workgraph_nba
import workgraph_store as ws

_OPEN_STATES = ("active", "waiting", "blocked")


def list_suppliers() -> list[dict]:
    """One entry per distinct external company with at least one issue,
    sorted by open-issue count descending (ties broken by most-recent
    activity). A company with 0 currently-open issues still appears (real
    history, not hidden), just at the bottom."""
    parties = ws.list_parties(affiliation="external")
    companies = sorted({p["company"] for p in parties if p.get("company")})

    out = []
    for company in companies:
        issue_ids = ws.list_issues_for_company(company)
        if not issue_ids:
            continue
        issues = [i for i in (ws.get_issue(iid) for iid in issue_ids) if i is not None]
        if not issues:
            continue
        open_issues = [i for i in issues if i["state"] in _OPEN_STATES]

        value_found = sum(workgraph_nba.value_amount_for_issue(i["id"]) for i in open_issues)
        workgraph_deadlines.attach_deadline_info(open_issues)
        has_hard_deadline = any(i.get("has_hard_deadline") for i in open_issues)
        last_activity_ts = max((i["updated_at"] for i in issues), default=None)

        out.append({
            "company": company,
            "open_issue_count": len(open_issues),
            "total_issue_count": len(issues),
            "value_found": value_found,
            "has_hard_deadline": has_hard_deadline,
            "last_activity_ts": last_activity_ts,
        })

    out.sort(key=lambda s: (s["open_issue_count"], s["last_activity_ts"] or 0), reverse=True)
    return out


def last_closed_issue_for_company(company: str, exclude_issue_id: Optional[str] = None) -> Optional[dict]:
    """Task #77 (supplier precedent comparison): the most recently closed
    (state='done') issue for this company - "last time with this
    supplier," a real reference point rather than a guess. None if
    nothing's ever closed for this company yet."""
    if not company:
        return None
    closed = []
    for iid in ws.list_issues_for_company(company):
        if iid == exclude_issue_id:
            continue
        issue = ws.get_issue(iid)
        if issue and issue["state"] == "done":
            closed.append(issue)
    if not closed:
        return None
    closed.sort(key=lambda i: i["updated_at"], reverse=True)
    latest = closed[0]
    days_open = max(0.0, (latest["updated_at"] - latest["opened_at"]) / 86400.0)
    return {
        "issue_id": latest["id"], "title": latest.get("display_title") or latest["title"],
        "closed_ts": latest["updated_at"], "days_to_close": round(days_open, 1),
    }


def attach_supplier_precedent(issue: dict) -> dict:
    """Mutates and returns `issue`: adds `supplier_precedent` (dict or
    None) - the most recently closed issue with the SAME real external
    supplier company, excluding this issue itself. A system-sender-only
    party (e.g. Ariba's no-reply) never has a `company` set, so it's
    naturally excluded rather than needing a separate check here."""
    company = None
    for party in ws.list_parties_for_issue(issue["id"]):
        if party.get("affiliation") == "external" and party.get("company"):
            company = party["company"]
            break
    issue["supplier_precedent"] = (
        last_closed_issue_for_company(company, exclude_issue_id=issue["id"]) if company else None
    )
    return issue


def supplier_detail(company: str) -> Optional[dict]:
    """Full issue list for one company (Settings/dashboard drill-down),
    or None if the company has no real issues at all."""
    issue_ids = ws.list_issues_for_company(company)
    if not issue_ids:
        return None
    issues = [i for i in (ws.get_issue(iid) for iid in issue_ids) if i is not None]
    if not issues:
        return None
    issues.sort(key=lambda i: i["updated_at"], reverse=True)
    workgraph_deadlines.attach_deadline_info(issues)
    for issue in issues:
        issue["value_found"] = workgraph_nba.value_amount_for_issue(issue["id"])
    return {"company": company, "issues": issues}
