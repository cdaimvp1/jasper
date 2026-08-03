"""
workgraph_deepdive.py — Project Deep-Dive (design doc Section 10): the
deterministic scaffolding around a scoped, seeded variant of relay's own
ingestion routine. This module owns the two pure/deterministic pieces -
picking which ONE project gets a wake's deep-dive, and deriving its search
seeds - never the actual live M365 search itself, which is real judgment
work done by the headless routine (ingest/PROJECT_DEEPDIVE_ROUTINE.md),
same split as workgraph_synthesis.py vs SYNTHESIS_ROUTINE.md.

Scope call (Section 10.3): projects only, not standalone issues - matches
Marc's own seeded-retrieval example ("find everything on the Workday
renewal") and is the smaller, more conservative first cut.
"""
from __future__ import annotations

from typing import Optional

import workgraph_store as ws

_DEEPDIVE_ELIGIBLE_STATUSES = ["active", "waiting"]

DEFAULT_DEEPDIVE_LIMIT = 1


def list_deepdive_candidates(limit: int = DEFAULT_DEEPDIVE_LIMIT) -> list[dict]:
    """Every active/waiting project, ranked never-deep-dived-first then
    oldest-last_deep_dive_ts-first (identical anti-starvation shape to
    workgraph_synthesis.list_stale_entities), capped to `limit` - one
    project per wake by default (Section 10.3: "never all projects at
    once"). done/archived/dismissed projects are never candidates - no
    payoff in chasing more evidence for something already closed out."""
    projects = ws.list_projects(status=_DEEPDIVE_ELIGIBLE_STATUSES)
    ranked = sorted(
        projects,
        key=lambda p: (p.get("last_deep_dive_ts") is not None, p.get("last_deep_dive_ts") or 0.0),
    )
    return ranked[:limit]


def derive_seeds_for_project(project_id: str) -> dict:
    """The project's own name plus every real identity anchor across its
    member issues (reference numbers, company names) - the exact seed Marc
    already types by hand today (Section 10.3), derived instead of
    retyped. Returns {project_id, name, anchors: [{anchor_type,
    normalized_value}]} - deduped, real anchors only (status='active',
    the same default list_identity_anchors_for_issues already applies)."""
    project = ws.get_project(project_id)
    if project is None:
        return {"project_id": project_id, "name": None, "anchors": []}

    issues = ws.list_issues_for_project(project_id)
    issue_ids = [i["id"] for i in issues]
    anchors_by_issue = ws.list_identity_anchors_for_issues(issue_ids)

    seen = set()
    anchors = []
    for issue_id in issue_ids:
        for anchor in anchors_by_issue.get(issue_id, []):
            key = (anchor["anchor_type"], anchor["normalized_value"])
            if key in seen:
                continue
            seen.add(key)
            anchors.append({"anchor_type": anchor["anchor_type"], "normalized_value": anchor["normalized_value"]})

    return {"project_id": project_id, "name": project.get("name"), "anchors": anchors}
