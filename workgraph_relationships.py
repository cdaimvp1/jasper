"""Relationship vs. Project separation (task #304, 2026-08-11, item #1 of
Marc's explicit build authorization: "Highest leverage - it's structural,
it's the one with the clearest concrete failure case, and getting it right
makes deduplication and grouping candidate quality better almost for free,
since 'same relationship, different project' becomes a real, representable
answer instead of a forced merge-or-don't decision.")

The concrete failure case this closes: three separate Authenticx
transactions (CMH Chatbots, Lilly Direct, Omvoh/Olumiant/Ebglyss) are the
same overall vendor relationship but genuinely different projects/
transactions. Before this, that fact existed nowhere durable - it either
got force-merged into one project (losing the real transaction split) or
sat as one of the 38 unresolved-forever work_object_relationships rows,
each one a real "these share strong signals but are different projects"
finding with no home.

Deliberately, genuinely SEPARATE from workgraph_pipeline2.py, per Marc's
own standing instruction embedded in that file's own module docstring:
"CURATOR OR ANY OTHER PREVIOUSLY BUILT MECHANISM SHOULD NOT TOUCH THIS.
BUILD NEW MECHANISMS FOR IT. KEEP IT ENTIRELY SEPARATE." This module only
ever READS pipeline2's already-written work_object_relationships output
(rows with relationship_type='rejected' - real matched-signal pairs a human/
LLM judgment already confirmed are NOT the same project) and writes to the
new, additive relationships/project_relationships tables. It never calls
into pipeline2's decision flow, and pipeline2 never calls into this.

run_relationship_sweep_daily_if_due follows the exact same
"claim_daily_run gate, then do the work" shape already established by
workgraph_claims_backfill.run_backfill_daily_if_due and workgraph_
retention.run_daily_if_due - see claim_daily_run's own docstring for the
cross-process race it closes."""
from __future__ import annotations

import json
import time

import workgraph_projects
import workgraph_signals
import workgraph_store as ws


def _shared_supplier_name(a_id: str, b_id: str) -> str | None:
    """The real matched company name behind a 'supplier' data point -
    matched_signals_json only ever records the POINT TYPE ("supplier"),
    never the value, so naming a Relationship means re-deriving the actual
    shared name from both sides' cached signatures, same normalization
    _matched_data_points itself uses (workgraph_signals.
    normalize_company_name) so "Authenticx" and "AUTHENTICX INC" resolve to
    one shared name, not two. Returns the first real (non-normalized, for
    display) spelling found in common, or None if nothing overlaps -
    callers must not fabricate a name when this returns None."""
    a_sig = workgraph_projects.get_or_compute_work_object_signature(a_id)
    b_sig = workgraph_projects.get_or_compute_work_object_signature(b_id)
    a_vocab = a_sig.get("positive_vocabulary") or {}
    b_vocab = b_sig.get("positive_vocabulary") or {}
    a_orgs = list(a_sig.get("external_orgs") or [])
    b_orgs = list(b_sig.get("external_orgs") or [])
    a_system_party = a_vocab.get("system_party")
    b_system_party = b_vocab.get("system_party")
    if a_system_party:
        a_orgs.append(a_system_party)
    if b_system_party:
        b_orgs.append(b_system_party)

    a_norm = {workgraph_signals.normalize_company_name(o) for o in a_orgs}
    b_norm = {workgraph_signals.normalize_company_name(o) for o in b_orgs}
    a_norm.discard("")
    b_norm.discard("")
    shared_norm = a_norm & b_norm
    if not shared_norm:
        return None
    for org in a_orgs + b_orgs:
        if workgraph_signals.normalize_company_name(org) in shared_norm:
            return org.strip()
    return None


def run_relationship_sweep() -> dict:
    """Reads every 'rejected' work_object_relationships row, resolves both
    sides to their parent PROJECT (parent_id), and for any pair whose
    projects genuinely differ, links both projects to a durable, named
    Relationship - creating it first if this is the first pair found for
    that shared supplier name.

    Honest, deliberate scope limits, not silently pretended-covered:
      - Only rows whose matched_signals include "supplier" get a real name
        derived (see _shared_supplier_name). A row matched only on
        "stakeholder"/"subject_entity"/etc with no shared supplier is
        skipped outright rather than inventing a name - naming those, if
        ever wanted, is curator/review work (item #2's dedup/audit sweep),
        not something this sweep should guess at.
      - Either side not yet promoted to (or no longer attached to) a real
        project (parent_id is None) is skipped - a Relationship links
        PROJECTS, not raw work_objects/clusters.
      - A pair whose two sides already resolve to the SAME project is
        skipped - that is not a cross-project relationship at all, just an
        already-grouped pair whose work_object_relationships row predates
        the grouping (or a bridge/split case) - nothing new to link.
    """
    rejected = ws.list_work_object_relationships_by_type("rejected")
    known_names = {r["name"].lower() for r in ws.list_relationships(status="active")}
    relationships_created = 0
    project_links_created = 0
    skipped_no_supplier = 0
    skipped_no_project = 0
    skipped_same_project = 0

    for row in rejected:
        matched_signals = row.get("matched_signals_json")
        if isinstance(matched_signals, str):
            try:
                matched_signals = json.loads(matched_signals)
            except (ValueError, TypeError):
                matched_signals = []
        if not matched_signals or "supplier" not in matched_signals:
            skipped_no_supplier += 1
            continue

        a_obj = ws.get_issue_or_cluster(row["from_id"])
        b_obj = ws.get_issue_or_cluster(row["to_id"])
        a_project_id = a_obj.get("project_id") if a_obj else None
        b_project_id = b_obj.get("project_id") if b_obj else None
        if not a_project_id or not b_project_id:
            skipped_no_project += 1
            continue
        if a_project_id == b_project_id:
            skipped_same_project += 1
            continue

        supplier_name = _shared_supplier_name(row["from_id"], row["to_id"])
        if not supplier_name:
            skipped_no_supplier += 1
            continue

        is_new_name = supplier_name.lower() not in known_names
        relationship_id = ws.get_or_create_relationship_by_name(supplier_name)
        if is_new_name:
            known_names.add(supplier_name.lower())
            relationships_created += 1

        reason = f"work_object_relationships #{row['id']} ({row['from_id']} / {row['to_id']})"
        before = ws.list_projects_for_relationship(relationship_id)
        before_ids = {p["id"] for p in before}
        ws.link_project_to_relationship(a_project_id, relationship_id, reason=reason)
        ws.link_project_to_relationship(b_project_id, relationship_id, reason=reason)
        after = ws.list_projects_for_relationship(relationship_id)
        project_links_created += len({p["id"] for p in after} - before_ids)

    return {
        "rejected_rows_scanned": len(rejected),
        "relationships_created": relationships_created,
        "project_links_created": project_links_created,
        "skipped_no_supplier_signal": skipped_no_supplier,
        "skipped_no_project": skipped_no_project,
        "skipped_same_project": skipped_same_project,
    }


def run_relationship_sweep_daily_if_due(now: float | None = None) -> dict | None:
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not ws.claim_daily_run("relationship_sweep", today):
        return None
    return run_relationship_sweep()


def list_relationships_needing_review() -> list[dict]:
    """Task #304, item #2 (2026-08-11, Marc's own scoping call: chat/MCP
    tool only, no cockpit UI, no confirm/reject queue - a relationship
    spanning multiple projects isn't a proposal with a clean yes/no verdict
    the way a claim suggestion is, it's a standing fact worth a look on
    demand). Every active Relationship with 2+ linked projects - exactly
    the "these share a strong relationship signal but are separate
    projects, should they be?" question the Sodalis/Authenticx case
    exposed. Read-only, computed live from relationships/
    project_relationships - nothing here is a suggestion queue with its
    own state, so there's nothing to resolve and nothing that goes stale."""
    results = []
    for rel in ws.list_relationships(status="active"):
        projects = ws.list_projects_for_relationship(rel["id"])
        if len(projects) < 2:
            continue
        results.append({
            "relationship_id": rel["id"],
            "name": rel["name"],
            "projects": [{"id": p["id"], "name": p["name"], "status": p["status"]} for p in projects],
        })
    results.sort(key=lambda r: len(r["projects"]), reverse=True)
    return results


if __name__ == "__main__":
    print(json.dumps(run_relationship_sweep(), indent=2))
