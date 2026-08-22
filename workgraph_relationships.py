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

import workgraph_discovery
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
    if not ws.due_for_daily_run("relationship_sweep", now):
        return None
    return run_relationship_sweep()


# --- second producer: shared-supplier entity sweep (2026-08-11, Marc's own
# direct review) ------------------------------------------------------------
# Real, confirmed gap in the sweep above: it only ever links two projects
# that FIRST cleared workgraph_pipeline2.find_candidates' 2+-point gate AND
# were then LLM-judged related_different_project. A pair sharing exactly
# ONE data point (a company name, and nothing else) never even becomes a
# candidate - it never reaches judge_candidate at all - so it can never
# produce a work_object_relationships row for the sweep above to read.
# Concretely: "Microsoft EA Renewal" and, six months later, "Microsoft
# Copilot Pilot" may share nothing but "Microsoft" - never 2+ points, never
# a candidate, never a Relationship, even though they obviously belong to
# the same durable vendor relationship.
#
# Marc's own framing of the fix: "Relationship = rejected same-supplier
# project candidate" should evolve toward "Relationship = canonical durable
# business entity/context to which independently discovered projects can
# attach" - with the rejected-candidate mechanism becoming ONE way of
# discovering the link, not the only one. This is that second producer -
# additive, not a replacement: run_relationship_sweep above is untouched,
# and this reuses the exact same relationships/project_relationships
# tables and the exact same get_or_create_relationship_by_name/
# link_project_to_relationship primitives, so a Relationship discovered
# either way is indistinguishable once it exists.
#
# Deterministic, no LLM, no score: reuses the data_point_values index
# workgraph_projects._sync_fasttrack_data_point_index already maintains for
# the "supplier" point (task #331) - the SAME normalized company name
# (workgraph_signals.normalize_company_name) _matched_data_points itself
# already trusts, just grouped across the WHOLE corpus instead of compared
# pairwise. No new extraction, no new normalization rule, no O(n^2) scan -
# one grouped pass over an already-indexed table.

def _display_name_for_normalized_supplier(normalized_value: str, sample_work_object_ids: list) -> str | None:
    """A real, non-fabricated spelling for a normalized company name -
    same "never invent a name, only ever re-derive one already extracted"
    discipline _shared_supplier_name above uses, just sampling a single
    normalized value's own indexed work objects instead of comparing two
    sides' sets. Returns None (never a guess) if, implausibly, none of the
    sampled work objects' own signatures still carry a matching raw
    spelling (e.g. the index is stale relative to a since-changed
    signature) - callers must not fabricate a name in that case either."""
    for work_object_id in sample_work_object_ids:
        sig = workgraph_projects.get_or_compute_work_object_signature(work_object_id)
        vocab = sig.get("positive_vocabulary") or {}
        candidates = list(sig.get("external_orgs") or [])
        system_party = vocab.get("system_party")
        if system_party:
            candidates.append(system_party)
        for org in candidates:
            if workgraph_signals.normalize_company_name(org) == normalized_value:
                return org.strip()
    return None


def run_supplier_entity_sweep() -> dict:
    """Groups the entire corpus's own already-indexed 'supplier' data
    points by normalized company name, resolves each indexed work_object
    to its owning PROJECT (a Relationship links projects, never raw
    issues/clusters - same rule run_relationship_sweep's own docstring
    states), and for any normalized company name spanning 2+ DISTINCT real
    projects, links them all to a durable Relationship - creating it first
    if this is the first time that name has produced one.

    Same honest scope limits as run_relationship_sweep:
      - A work object with no project yet (still an unpromoted cluster) is
        skipped - nothing to link.
      - Never fabricates a display name (see _display_name_for_normalized_
        supplier) - a normalized value with no recoverable real spelling
        contributes nothing rather than showing a mangled lowercase name
        to a human.
    Idempotent and safe to re-run: get_or_create_relationship_by_name/
    link_project_to_relationship are both already dedupe-then-insert."""
    rows = ws.list_data_point_values_for_definition(workgraph_discovery.FASTTRACK_SUPPLIER_ID)
    projects_by_value: dict[str, set] = {}
    sample_work_objects_by_value: dict[str, list] = {}
    for row in rows:
        obj = ws.get_issue_or_cluster(row["work_object_id"])
        project_id = (obj or {}).get("project_id")
        if not project_id:
            continue
        projects_by_value.setdefault(row["value"], set()).add(project_id)
        sample_work_objects_by_value.setdefault(row["value"], []).append(row["work_object_id"])

    known_names = {r["name"].lower() for r in ws.list_relationships(status="active")}
    relationships_created = 0
    project_links_created = 0
    skipped_no_display_name = 0
    entity_groups_found = 0

    for normalized_value, project_ids in projects_by_value.items():
        if len(project_ids) < 2:
            continue
        entity_groups_found += 1
        display_name = _display_name_for_normalized_supplier(
            normalized_value, sample_work_objects_by_value[normalized_value])
        if not display_name:
            skipped_no_display_name += 1
            continue

        is_new_name = display_name.lower() not in known_names
        relationship_id = ws.get_or_create_relationship_by_name(display_name)
        if is_new_name:
            known_names.add(display_name.lower())
            relationships_created += 1

        reason = f"supplier_entity_sweep: shared company '{display_name}' across {len(project_ids)} projects"
        before = ws.list_projects_for_relationship(relationship_id)
        before_ids = {p["id"] for p in before}
        for project_id in project_ids:
            ws.link_project_to_relationship(project_id, relationship_id, reason=reason)
        after = ws.list_projects_for_relationship(relationship_id)
        project_links_created += len({p["id"] for p in after} - before_ids)

    return {
        "supplier_values_scanned": len(projects_by_value),
        "entity_groups_found": entity_groups_found,
        "relationships_created": relationships_created,
        "project_links_created": project_links_created,
        "skipped_no_display_name": skipped_no_display_name,
    }


def run_supplier_entity_sweep_daily_if_due(now: float | None = None) -> dict | None:
    if not ws.due_for_daily_run("supplier_entity_sweep", now):
        return None
    return run_supplier_entity_sweep()


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
