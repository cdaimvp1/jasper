"""
workgraph_todo.py - task #281: the real aggregation behind "what's my to-do
list?" - three sections mirroring the locked drawer-mockup's own structure
("Outputs waiting on you" / "Open claims" / "By supplier"), built entirely
from data Jasper already tracks. Zero LLM, zero new extraction - same
"group and sum, don't invent" discipline as workgraph_suppliers.py.

Deliberately returns raw structured data, not pre-formatted prose - the
assistant (workgraph_assistant.py's system prompt) renders it conversationally,
same division of labor as jasper_list_review_queue/jasper_worker_status.
"""
from __future__ import annotations

from collections import Counter

import workgraph_store as ws
import workgraph_suppliers

_OPEN_STATES = ("active", "waiting", "blocked")

# Open-claims corpus can run into the thousands at Marc's current DB scale
# (see task #207's issue-count growth) - capped so this stays a single fast
# tool call inside the assistant's turn budget, with the true total always
# reported alongside so a cap never silently reads as "that's everything."
_MAX_CLAIM_ITEMS = 40


def build_todo_summary() -> dict:
    outputs = ws.list_unreviewed_worker_outputs()

    open_issues = ws.list_issues(states=list(_OPEN_STATES), limit=5000)
    issues_by_id = {i["id"]: i for i in open_issues}
    claims_by_issue = ws.list_open_claims_for_issues(list(issues_by_id.keys()))

    all_claims = []
    for issue_id, claims in claims_by_issue.items():
        issue = issues_by_id.get(issue_id, {})
        for c in claims:
            c = dict(c)
            c["issue_title"] = issue.get("display_title") or issue.get("title")
            c["external_companies"] = issue.get("external_companies")
            all_claims.append(c)

    claim_type_counts = Counter(c["claim_type"] for c in all_claims)
    issues_with_open_claims = {c["issue_id"] for c in all_claims}
    # last_seen_ts DESC (list_open_claims_for_issues' own order) already
    # surfaces the most recently-active claims first within each issue's
    # bucket - re-sort across the flattened set so the cap keeps the overall
    # most-recently-touched claims, not just the first few issues' worth.
    all_claims.sort(key=lambda c: c.get("last_seen_ts") or 0, reverse=True)

    suppliers = [s for s in workgraph_suppliers.list_suppliers() if s["open_issue_count"] > 0]
    by_supplier = []
    for s in suppliers:
        company_issue_ids = ws.list_issues_for_company(s["company"])
        company_open_issues = [
            issues_by_id[iid] for iid in company_issue_ids
            if iid in issues_by_id
        ]
        by_supplier.append({
            "company": s["company"],
            "open_issue_count": s["open_issue_count"],
            "value_found": s["value_found"],
            "issues": [
                {"id": i["id"], "title": i.get("display_title") or i.get("title")}
                for i in company_open_issues
            ],
        })
    by_supplier.sort(key=lambda s: s["open_issue_count"], reverse=True)

    return {
        "outputs_waiting": outputs,
        "open_claims": {
            "total": len(all_claims),
            "issue_count": len(issues_with_open_claims),
            "by_type": dict(claim_type_counts),
            "items": all_claims[:_MAX_CLAIM_ITEMS],
            "truncated": len(all_claims) > _MAX_CLAIM_ITEMS,
        },
        "by_supplier": by_supplier,
    }
