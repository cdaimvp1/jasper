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
import workgraph_lessons
import workgraph_nba
import workgraph_store as ws

_OPEN_STATES = ("active", "waiting", "blocked")


def _gated_open_issue_count(open_issues: list[dict]) -> int:
    """Enhancement #3 (Aristotle join): count of this company's open issues
    currently flagged has_unmet_prerequisite=1 - a column workgraph_nba.
    recompute_all() already maintains on every scoring pass. Deliberately
    NOT re-running workgraph_aristotle.check_prerequisites() here - that
    would answer a different question ('which RULE is gating right now')
    than what the Supplier Dashboard needs ('how many of this supplier's
    open issues are gated, per the last real recompute')."""
    return sum(1 for i in open_issues if i.get("has_unmet_prerequisite"))


def _total_recall_precedent_for_company(company: str, categories: set) -> Optional[dict]:
    """Enhancement #3 (Total Recall join): a supplier's open issues can span
    more than one category, and workgraph_lessons.situation_key is keyed on
    (category, company) together - there's no single situation_key for a
    company alone. So this checks every distinct real category this
    supplier currently has open issues in, and surfaces the highest-trust
    validated lesson found across them (None if none of them have one)."""
    best = None
    for category in categories:
        key = workgraph_lessons.situation_key(category, company)
        if key is None:
            continue
        lesson = workgraph_lessons.best_lesson_for_key(key)
        if lesson and (best is None or lesson["trust_score"] > best["trust_score"]):
            best = lesson
    if best is None:
        return None
    return {"statement": best["statement"], "confidence": workgraph_lessons.confidence_band(best["trust_score"])}


def _issue_dicts_for_company(company: str) -> list[dict]:
    """Shared batched fetch: every real issue row for this company, via one
    id-list query plus one batched get_issues_by_ids call - fixed 2026-07-30
    (hardening pass #3, minor nit #6) from an O(n) get_issue-per-id loop."""
    if not company:
        return []
    issue_ids = ws.list_issues_for_company(company)
    if not issue_ids:
        return []
    issues_by_id = ws.get_issues_by_ids(issue_ids)
    return [issues_by_id[iid] for iid in issue_ids if iid in issues_by_id]


def list_suppliers() -> list[dict]:
    """One entry per distinct external company with at least one issue,
    sorted by open-issue count descending (ties broken by most-recent
    activity). A company with 0 currently-open issues still appears (real
    history, not hidden), just at the bottom.

    Fixed 2026-07-30 (hardening pass #3, HIGH): this used to call
    ws.get_issue() once per issue and workgraph_nba.value_amount_for_issue()
    (-> ws.get_raw_items_for_issue()) once per open issue, across every
    company - measured live at 375 individual sqlite connections and
    3-4.5s wall-clock, freezing the single uvicorn worker for that whole
    span on every call. Both are now single batched queries across every
    company's issues at once."""
    parties = ws.list_parties(affiliation="external")
    companies = sorted({p["company"] for p in parties if p.get("company")})

    company_issue_ids = {company: ws.list_issues_for_company(company) for company in companies}
    all_issue_ids = [iid for ids in company_issue_ids.values() for iid in ids]
    issues_by_id = ws.get_issues_by_ids(all_issue_ids)
    all_open_issue_ids = [iid for iid in all_issue_ids if issues_by_id.get(iid, {}).get("state") in _OPEN_STATES]
    value_by_issue = workgraph_nba.value_amounts_for_issues(all_open_issue_ids)

    out = []
    for company in companies:
        issues = [issues_by_id[iid] for iid in company_issue_ids[company] if iid in issues_by_id]
        if not issues:
            continue
        open_issues = [i for i in issues if i["state"] in _OPEN_STATES]

        value_found = sum(value_by_issue.get(i["id"], 0.0) for i in open_issues)
        workgraph_deadlines.attach_deadline_info(open_issues)
        has_hard_deadline = any(i.get("has_hard_deadline") for i in open_issues)
        last_activity_ts = max((i["updated_at"] for i in issues), default=None)
        categories = {i["category"] for i in open_issues if i.get("category")}

        out.append({
            "company": company,
            "open_issue_count": len(open_issues),
            "total_issue_count": len(issues),
            "value_found": value_found,
            "has_hard_deadline": has_hard_deadline,
            "last_activity_ts": last_activity_ts,
            "gated_open_issue_count": _gated_open_issue_count(open_issues),
            "precedent": _total_recall_precedent_for_company(company, categories),
        })

    out.sort(key=lambda s: (s["open_issue_count"], s["last_activity_ts"] or 0), reverse=True)
    return out


def last_closed_issue_for_company(company: str, exclude_issue_id: Optional[str] = None) -> Optional[dict]:
    """Task #77 (supplier precedent comparison): the most recently closed
    (state='done') issue for this company - "last time with this
    supplier," a real reference point rather than a guess. None if
    nothing's ever closed for this company yet."""
    closed = [
        i for i in _issue_dicts_for_company(company)
        if i["id"] != exclude_issue_id and i["state"] == "done"
    ]
    if not closed:
        return None
    closed.sort(key=lambda i: i["updated_at"], reverse=True)
    latest = closed[0]
    days_open = max(0.0, (latest["updated_at"] - latest["opened_at"]) / 86400.0)
    return {
        "issue_id": latest["id"], "title": latest.get("display_title") or latest["title"],
        "closed_ts": latest["updated_at"], "days_to_close": round(days_open, 1),
    }


def _resolved_company_for_issue(issue_id: str) -> Optional[str]:
    """The one real external supplier company for this issue, or None. A
    system-sender-only party (e.g. Ariba's no-reply) never has a `company`
    set, so it's naturally excluded rather than needing a separate check.

    Fixed 2026-07-30 (hardening pass #2): workgraph_store.
    list_parties_for_issue has no ORDER BY, so taking the first match
    from an unordered JOIN result was non-deterministic whenever an issue
    has more than one identifiable external company - two otherwise-
    identical requests could resolve to a different supplier. first_seen_ts
    ascending (the earliest-known contact on this issue) is a real, stable
    tie-break, not an arbitrary one."""
    candidates = [
        p for p in ws.list_parties_for_issue(issue_id)
        if p.get("affiliation") == "external" and p.get("company")
    ]
    candidates.sort(key=lambda p: p.get("first_seen_ts") or 0)
    return candidates[0]["company"] if candidates else None


def other_gated_open_issue_count_for_company(company: str, exclude_issue_id: str) -> int:
    """Enhancement #89 (issue detail panel): how many of this SAME
    supplier's OTHER open issues are currently gated - real signal Marc
    previously only saw by visiting the Handover panel's supplier list.
    0 if there's no real company or nothing else is gated."""
    return sum(
        1 for i in _issue_dicts_for_company(company)
        if i["id"] != exclude_issue_id and i["state"] in _OPEN_STATES and i.get("has_unmet_prerequisite")
    )


def attach_supplier_precedent(issue: dict) -> dict:
    """Mutates and returns `issue`: adds `supplier_precedent` (dict or
    None) - the most recently closed issue with the SAME real external
    supplier company, excluding this issue itself - and
    `supplier_other_gated_count` - how many of this supplier's OTHER open
    issues are currently gated (enhancement #89)."""
    company = _resolved_company_for_issue(issue["id"])
    issue["supplier_precedent"] = (
        last_closed_issue_for_company(company, exclude_issue_id=issue["id"]) if company else None
    )
    issue["supplier_other_gated_count"] = (
        other_gated_open_issue_count_for_company(company, exclude_issue_id=issue["id"]) if company else 0
    )
    return issue


def supplier_detail(company: str) -> Optional[dict]:
    """Full issue list for one company (Settings/dashboard drill-down),
    or None if the company has no real issues at all."""
    issues = _issue_dicts_for_company(company)
    if not issues:
        return None
    issues.sort(key=lambda i: i["updated_at"], reverse=True)
    workgraph_deadlines.attach_deadline_info(issues)
    value_by_issue = workgraph_nba.value_amounts_for_issues([i["id"] for i in issues])
    for issue in issues:
        issue["value_found"] = value_by_issue.get(issue["id"], 0.0)
    open_issues = [i for i in issues if i["state"] in _OPEN_STATES]
    categories = {i["category"] for i in open_issues if i.get("category")}
    return {
        "company": company,
        "issues": issues,
        "gated_open_issue_count": _gated_open_issue_count(open_issues),
        "precedent": _total_recall_precedent_for_company(company, categories),
    }
