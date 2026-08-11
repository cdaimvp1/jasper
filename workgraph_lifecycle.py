"""
workgraph_lifecycle.py - the 'dormant' lifecycle state (task #310 follow-up,
Fix 4, 2026-08-11, Marc's own engineering-direction doc, Section 8: "Projects
and issues should not remain permanently active merely because they once
existed").

Before this, work either sat at active/waiting/blocked forever or required a
manual done/dismissed/archived action - there was no real, evidence-driven
"gone quiet" state. 'dormant' is deliberately NOT a terminal state like those
three: it is designed to auto-revert the instant new real evidence arrives
(see revert_dormant_if_needed, called from workgraph_claims.materialize_
claims_for_raw_item), so marking something dormant is never a one-way door.

Deliberately no LLM call anywhere in this module - staleness is a pure
timestamp comparison, not a judgment call, matching Section 17's own
"structured state first" preference.

'candidate' (also named in Section 8) is deliberately NOT added as a new
value - a raw cluster (work_objects.is_raw_cluster=1) already IS the
pre-confirmation candidate state today, just expressed as a flag rather than
a status string; adding a redundant status value for the same concept would
duplicate an existing signal rather than fill a real gap."""
from __future__ import annotations

import time
from typing import Optional

import workgraph_store as ws

_DORMANT_THRESHOLD_SECONDS = 60 * 24 * 3600  # 60 days - Marc's own call to move, see this module's docstring
_DORMANT_ELIGIBLE_STATES = ("active", "waiting", "blocked")


def _last_evidence_ts(work_object_id: str) -> Optional[float]:
    """Real evidence recency for ONE work_object directly - the max of
    every linked raw_item's occurred_ts and every claim's last_seen_ts.
    Deliberately never opened_at/updated_at alone - those get touched by
    things that aren't necessarily new real-world evidence (workgraph_
    nba.recompute_all's periodic rescoring already deliberately excludes
    itself via touch_updated_at=False for exactly this staleness-signal
    reason - this module leans on the same discipline)."""
    raw_items = ws.get_raw_items_for_issue(work_object_id)
    ts_values = [item["occurred_ts"] for item in raw_items if item.get("occurred_ts")]
    claims_by_member = ws.list_claims_for_issues([work_object_id])
    for claims in claims_by_member.values():
        for claim in claims:
            if claim.get("last_seen_ts"):
                ts_values.append(claim["last_seen_ts"])
    return max(ts_values) if ts_values else None


def _last_evidence_ts_for_project(project_id: str) -> Optional[float]:
    """A project's own evidence lives on its members (clusters + real
    issues), never directly on the project row itself - same member
    resolution workgraph_pipeline2.run_project_extraction already uses."""
    member_ids = (
        [c["id"] for c in ws.list_clusters_for_project(project_id)]
        + [i["id"] for i in ws.list_issues_for_project(project_id)]
    )
    ts_values = [t for t in (_last_evidence_ts(mid) for mid in member_ids) if t is not None]
    return max(ts_values) if ts_values else None


def run_dormant_sweep(*, now: Optional[float] = None,
                       threshold_seconds: int = _DORMANT_THRESHOLD_SECONDS) -> dict:
    """Deterministic, no LLM calls. Flips active/waiting/blocked issues
    AND projects with no real evidence more recent than threshold_seconds
    to 'dormant'. A work_object with literally zero evidence ever (last_ts
    is None) is left alone - that's a different, pre-existing data gap,
    not this sweep's job to guess about."""
    if now is None:
        now = time.time()
    cutoff = now - threshold_seconds

    dormant_issues = []
    for issue in ws.list_issues(states=list(_DORMANT_ELIGIBLE_STATES), limit=10000):
        last_ts = _last_evidence_ts(issue["id"])
        if last_ts is not None and last_ts < cutoff:
            ws.update_issue(issue["id"], state="dormant", actor="workgraph_lifecycle.run_dormant_sweep")
            dormant_issues.append(issue["id"])

    dormant_projects = []
    for project in ws.list_projects(status=list(_DORMANT_ELIGIBLE_STATES)):
        last_ts = _last_evidence_ts_for_project(project["id"])
        if last_ts is not None and last_ts < cutoff:
            ws.set_project_status(project["id"], "dormant")
            dormant_projects.append(project["id"])

    return {"dormant_issues": dormant_issues, "dormant_projects": dormant_projects}


def run_dormant_sweep_daily_if_due(now: Optional[float] = None) -> Optional[dict]:
    """Gated the exact same way workgraph_relationships.run_relationship_
    sweep_daily_if_due / workgraph_noise.run_noise_sweep_daily_if_due are -
    cheap and deterministic enough to run every scheduled_refresh.py
    cycle, but daily is plenty for something measured in weeks."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not ws.claim_daily_run("dormant_sweep", today):
        return None
    return run_dormant_sweep(now=now)


def revert_dormant_if_needed(work_object_id: str) -> None:
    """Called right after new evidence materializes for a work_object
    (workgraph_claims.materialize_claims_for_raw_item) - dormant is never
    a permanent state; real new evidence always wakes it back up. Checks
    both the work_object's own state AND its parent project's, since new
    evidence landing on one issue can revive a dormant PROJECT too."""
    issue = ws.get_issue_or_cluster(work_object_id)
    if issue is None:
        return
    if issue.get("state") == "dormant":
        ws.update_issue(work_object_id, state="active", actor="workgraph_lifecycle.revert_dormant_if_needed")
    project_id = issue.get("project_id")
    if project_id:
        project = ws.get_project(project_id)
        if project and project.get("status") == "dormant":
            ws.set_project_status(project_id, "active")
