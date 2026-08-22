"""
workgraph_noise.py - deterministic, zero-LLM project-level noise gate
(task #310 follow-up, 2026-08-11, Marc's own direct request after
reviewing the Workload Status Update Report's first real output).

Real motivating example: proj-001, "[EXTERNAL] Fwd: Welcome to the
Grizzlies! Fall 2026 U10 Soccer" - Marc forwarding his own kid's team
sign-up email from his personal gmail account to himself. It has real,
structurally valid ask/commitment claims ("confirm roster spot", "get
practice gear") - claim-type substance filtering alone (workgraph_status_
report.py's _has_real_substance) cannot tell this apart from a real
business ask, because it isn't a claims-quality problem: it's a "this
isn't Jasper's job to track at all" problem. A separate, automated-
notification-only kind of noise (Yammer/Viva Engage group-add pings,
Artifactory onboarding emails) is already handled - those never produce
a real ask/decision/commitment claim in the first place, so the claims-
substance filter already excludes them without this module's help.

The gate here is specifically for content that DOES look substantive by
claim-type, but has no real external-business counterpart: every
external (non-lilly.com) participant domain across every raw_item under
the project is a known personal/consumer webmail provider, AND there is
no PR/PO reference number anywhere, AND no claim mentions a real dollar
figure. All three have to hold - a real vendor thread that happens to cc
someone's personal gmail, or an internal-only Lilly approval thread with
a real PR number, must never get caught by this.

Deliberately separate from workgraph_relationships.py's mechanism (same
"keep it entirely separate" discipline Marc's own words established for
that module) - this reads raw_items/claims, never work_object_
relationships, and writes only projects.status via the pre-existing
noise-archived value (task #205's whitelist already permits it) - never
touches grouping/merge decisions."""
from __future__ import annotations

import re
import time
from typing import Optional

import workgraph_store as ws

_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com",
    "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
}
_EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+\.[\w.-]+)")
_HAS_DOLLAR_RE = re.compile(r"\$\s?\d")
_LILLY_DOMAIN = "lilly.com"


def _domains_in_text(text: Optional[str]) -> set[str]:
    return {m.group(1).lower() for m in _EMAIL_RE.finditer(text or "")}


def classify_project_noise(project_id: str) -> Optional[str]:
    """Returns a real, human-readable reason string if this project
    matches the noise gate, else None (never a guess either way - a
    project with zero raw_items, or with any non-personal external
    domain, or with a PR/PO reference, or with any dollar figure
    anywhere in its claims, is left alone no matter how thin its
    content otherwise looks)."""
    member_ids = (
        [c["id"] for c in ws.list_clusters_for_project(project_id)]
        + [i["id"] for i in ws.list_issues_for_project(project_id)]
    )
    if not member_ids:
        return None

    raw_items = []
    for member_id in member_ids:
        raw_items.extend(ws.get_raw_items_for_issue(member_id))
    if not raw_items:
        return None

    domains: set[str] = set()
    has_reference = False
    for item in raw_items:
        domains |= _domains_in_text(item.get("from_actor"))
        domains |= _domains_in_text(item.get("participants"))
        if item.get("pr_number"):
            has_reference = True

    domains.discard(_LILLY_DOMAIN)
    if not domains:
        return None  # internal-only thread - not this gate's concern, could easily be real work
    if not domains <= _PERSONAL_EMAIL_DOMAINS:
        return None  # at least one real external business domain - not noise
    if has_reference:
        return None

    claims_by_member = ws.list_claims_for_issues(member_ids)
    for claims in claims_by_member.values():
        for claim in claims:
            if _HAS_DOLLAR_RE.search(claim.get("text") or ""):
                return None

    return (f"only personal/consumer email domain(s) present ({', '.join(sorted(domains))}), "
            f"no PR/PO reference, no dollar figure in any claim")


_NOISE_SWEEP_STATUSES = ["active", "waiting", "blocked"]


def run_noise_sweep() -> dict:
    """Deterministic, no LLM calls. Checks every currently open project,
    flips a real match to status='noise-archived' via the existing,
    already-whitelisted set_project_status - reversible (a human can
    revert via the Projects panel's existing 'Revert' action, task #206),
    never a hard delete."""
    checked = 0
    reclassified = []
    for project in ws.list_projects(status=_NOISE_SWEEP_STATUSES):
        checked += 1
        reason = classify_project_noise(project["id"])
        if reason:
            ws.set_project_status(project["id"], "noise-archived")
            reclassified.append({"project_id": project["id"], "reason": reason})
    return {"checked": checked, "reclassified_count": len(reclassified), "reclassified": reclassified}


def run_noise_sweep_daily_if_due(now: float | None = None) -> Optional[dict]:
    """Gated the exact same way workgraph_relationships.run_relationship_
    sweep_daily_if_due is - cheap and deterministic enough to run every
    scheduled_refresh.py cycle, but a daily cadence is plenty for
    something this low-urgency, and keeps this mechanism's own cursor
    independent of any other sweep's."""
    if not ws.due_for_daily_run("noise_sweep", now):
        return None
    return run_noise_sweep()
