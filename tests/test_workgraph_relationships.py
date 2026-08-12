"""Regression tests for workgraph_relationships.py (task #304, item #1 of
Marc's 2026-08-11 build authorization: Relationship vs. Project separation).

Covers run_relationship_sweep()'s real decision logic: which 'rejected'
work_object_relationships rows turn into a durable, named Relationship
linking two projects, and the honest skip cases (no shared supplier name,
either side not attached to a real project, both sides already the same
project)."""
from __future__ import annotations

import workgraph_relationships as wr


def _issue(ws_db, id, title="Some issue"):
    ws_db.create_issue(id=id, title=title, state="active", category="other")
    return id


def _link_supplier(ws_db, issue_id, party_id, email, company):
    ws_db.upsert_party(id=party_id, primary_email=email, display_name=party_id,
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company=company)
    ws_db.link_party_to_issue(issue_id, party_id)


def test_sweep_links_two_projects_sharing_a_real_supplier(ws_db):
    ws_db.create_project(id="proj-cmh", name="CMH Chatbots")
    ws_db.create_project(id="proj-direct", name="Lilly Direct")
    a = _issue(ws_db, "wo-a")
    b = _issue(ws_db, "wo-b")
    _link_supplier(ws_db, a, "pa", "rep@authenticx.com", "Authenticx")
    _link_supplier(ws_db, b, "pb", "rep2@authenticx.com", "AUTHENTICX INC")
    ws_db.assign_issue_to_project(a, "proj-cmh")
    ws_db.assign_issue_to_project(b, "proj-direct")
    ws_db.upsert_work_object_relationship(
        a_id=a, b_id=b, relationship_type="rejected",
        match_count=2, matched_signals=["supplier", "stakeholder"],
    )

    result = wr.run_relationship_sweep()

    assert result["relationships_created"] == 1
    assert result["project_links_created"] == 2
    rels = ws_db.list_relationships_for_project("proj-cmh")
    assert len(rels) == 1
    assert rels[0]["name"].lower() == "authenticx"
    other_projects = {p["id"] for p in ws_db.list_projects_for_relationship(rels[0]["id"])}
    assert other_projects == {"proj-cmh", "proj-direct"}


def test_sweep_reuses_existing_relationship_for_a_third_project(ws_db):
    ws_db.create_project(id="proj-1", name="P1")
    ws_db.create_project(id="proj-2", name="P2")
    ws_db.create_project(id="proj-3", name="P3")
    a, b, c = _issue(ws_db, "wo-a"), _issue(ws_db, "wo-b"), _issue(ws_db, "wo-c")
    for iid, party, email in ((a, "pa", "x@sodalis.com"), (b, "pb", "y@sodalis.com"), (c, "pc", "z@sodalis.com")):
        _link_supplier(ws_db, iid, party, email, "Sodalis")
    ws_db.assign_issue_to_project(a, "proj-1")
    ws_db.assign_issue_to_project(b, "proj-2")
    ws_db.assign_issue_to_project(c, "proj-3")
    ws_db.upsert_work_object_relationship(a_id=a, b_id=b, relationship_type="rejected",
                                           match_count=2, matched_signals=["supplier"])
    ws_db.upsert_work_object_relationship(a_id=b, b_id=c, relationship_type="rejected",
                                           match_count=2, matched_signals=["supplier"])

    result = wr.run_relationship_sweep()

    assert result["relationships_created"] == 1  # one shared name, not two separate relationships
    rels_all = ws_db.list_relationships(status="active")
    assert len(rels_all) == 1
    projects = {p["id"] for p in ws_db.list_projects_for_relationship(rels_all[0]["id"])}
    assert projects == {"proj-1", "proj-2", "proj-3"}


def test_sweep_skips_row_with_no_supplier_signal(ws_db):
    ws_db.create_project(id="proj-1", name="P1")
    ws_db.create_project(id="proj-2", name="P2")
    a, b = _issue(ws_db, "wo-a"), _issue(ws_db, "wo-b")
    ws_db.assign_issue_to_project(a, "proj-1")
    ws_db.assign_issue_to_project(b, "proj-2")
    ws_db.upsert_work_object_relationship(a_id=a, b_id=b, relationship_type="rejected",
                                           match_count=2, matched_signals=["stakeholder", "subject_entity"])

    result = wr.run_relationship_sweep()

    assert result["relationships_created"] == 0
    assert result["skipped_no_supplier_signal"] == 1
    assert ws_db.list_relationships() == []


def test_sweep_skips_pair_with_no_matching_supplier_name(ws_db):
    ws_db.create_project(id="proj-1", name="P1")
    ws_db.create_project(id="proj-2", name="P2")
    a, b = _issue(ws_db, "wo-a"), _issue(ws_db, "wo-b")
    _link_supplier(ws_db, a, "pa", "x@vendorone.com", "Vendor One")
    _link_supplier(ws_db, b, "pb", "y@vendortwo.com", "Vendor Two")
    ws_db.assign_issue_to_project(a, "proj-1")
    ws_db.assign_issue_to_project(b, "proj-2")
    ws_db.upsert_work_object_relationship(a_id=a, b_id=b, relationship_type="rejected",
                                           match_count=2, matched_signals=["supplier", "stakeholder"])

    result = wr.run_relationship_sweep()

    assert result["relationships_created"] == 0
    assert result["skipped_no_supplier_signal"] == 1


def test_sweep_skips_side_with_no_project(ws_db):
    ws_db.create_project(id="proj-1", name="P1")
    a, b = _issue(ws_db, "wo-a"), _issue(ws_db, "wo-b")
    _link_supplier(ws_db, a, "pa", "x@authenticx.com", "Authenticx")
    _link_supplier(ws_db, b, "pb", "y@authenticx.com", "Authenticx")
    ws_db.assign_issue_to_project(a, "proj-1")
    # b is never assigned to any project - still a raw cluster/issue.
    ws_db.upsert_work_object_relationship(a_id=a, b_id=b, relationship_type="rejected",
                                           match_count=2, matched_signals=["supplier"])

    result = wr.run_relationship_sweep()

    assert result["relationships_created"] == 0
    assert result["skipped_no_project"] == 1


def test_sweep_skips_pair_already_in_the_same_project(ws_db):
    ws_db.create_project(id="proj-1", name="P1")
    a, b = _issue(ws_db, "wo-a"), _issue(ws_db, "wo-b")
    _link_supplier(ws_db, a, "pa", "x@authenticx.com", "Authenticx")
    _link_supplier(ws_db, b, "pb", "y@authenticx.com", "Authenticx")
    ws_db.assign_issue_to_project(a, "proj-1")
    ws_db.assign_issue_to_project(b, "proj-1")
    ws_db.upsert_work_object_relationship(a_id=a, b_id=b, relationship_type="rejected",
                                           match_count=2, matched_signals=["supplier"])

    result = wr.run_relationship_sweep()

    assert result["relationships_created"] == 0
    assert result["skipped_same_project"] == 1


def test_sweep_ignores_non_rejected_rows(ws_db):
    ws_db.create_project(id="proj-1", name="P1")
    ws_db.create_project(id="proj-2", name="P2")
    a, b = _issue(ws_db, "wo-a"), _issue(ws_db, "wo-b")
    _link_supplier(ws_db, a, "pa", "x@authenticx.com", "Authenticx")
    _link_supplier(ws_db, b, "pb", "y@authenticx.com", "Authenticx")
    ws_db.assign_issue_to_project(a, "proj-1")
    ws_db.assign_issue_to_project(b, "proj-2")
    ws_db.upsert_work_object_relationship(a_id=a, b_id=b, relationship_type="candidate",
                                           match_count=2, matched_signals=["supplier"])

    result = wr.run_relationship_sweep()

    assert result["rejected_rows_scanned"] == 0
    assert result["relationships_created"] == 0


def test_run_relationship_sweep_daily_if_due_gates_to_once_per_day(ws_db):
    now = 1_700_000_000.0
    first = wr.run_relationship_sweep_daily_if_due(now=now)
    second = wr.run_relationship_sweep_daily_if_due(now=now + 60)
    assert first is not None
    assert second is None


# --- list_relationships_needing_review (task #304, item #2) ----------------

def test_list_relationships_needing_review_includes_multi_project_relationships(ws_db):
    ws_db.create_project(id="proj-1", name="P1")
    ws_db.create_project(id="proj-2", name="P2")
    rid = ws_db.get_or_create_relationship_by_name("Authenticx")
    ws_db.link_project_to_relationship("proj-1", rid)
    ws_db.link_project_to_relationship("proj-2", rid)

    review = wr.list_relationships_needing_review()

    assert len(review) == 1
    assert review[0]["name"] == "Authenticx"
    assert {p["id"] for p in review[0]["projects"]} == {"proj-1", "proj-2"}


def test_list_relationships_needing_review_excludes_single_project_relationships(ws_db):
    ws_db.create_project(id="proj-1", name="P1")
    rid = ws_db.get_or_create_relationship_by_name("Authenticx")
    ws_db.link_project_to_relationship("proj-1", rid)

    assert wr.list_relationships_needing_review() == []


def test_list_relationships_needing_review_sorts_by_project_count_descending(ws_db):
    for pid in ("proj-1", "proj-2", "proj-3", "proj-4"):
        ws_db.create_project(id=pid, name=pid)
    small = ws_db.get_or_create_relationship_by_name("Vendor Small")
    big = ws_db.get_or_create_relationship_by_name("Vendor Big")
    ws_db.link_project_to_relationship("proj-1", small)
    ws_db.link_project_to_relationship("proj-2", small)
    ws_db.link_project_to_relationship("proj-1", big)
    ws_db.link_project_to_relationship("proj-2", big)
    ws_db.link_project_to_relationship("proj-3", big)
    ws_db.link_project_to_relationship("proj-4", big)

    review = wr.list_relationships_needing_review()

    assert [r["name"] for r in review] == ["Vendor Big", "Vendor Small"]


# --- run_supplier_entity_sweep (task #342, Marc's own direct review,
# 2026-08-11): a second, additive relationship-discovery producer. The
# sweep above only ever links projects that FIRST became pipeline2
# candidates (2+ matched points) and were then LLM-judged related_
# different_project - a pair sharing exactly ONE point (a bare company
# name, nothing else) never becomes a candidate at all, so it can never
# produce a work_object_relationships row for that sweep to read. This
# sweep groups the corpus's own already-indexed "supplier" data points by
# normalized company name across ALL real projects, independent of
# whether the pair was ever compared pairwise or judged by an LLM at all.

import workgraph_projects as wp


def _project_with_supplier(ws_db, project_id, project_name, issue_id, party_id, email, company):
    """Builds a real project with one issue carrying a real supplier
    signal, then forces the fasttrack data_point_values index to populate
    (mirroring what get_or_compute_work_object_signature already does on
    every real cache-miss in live operation - e.g. whenever find_
    candidates runs for any other item) - the sweep reads that index
    directly, never re-deriving party data itself."""
    ws_db.create_project(id=project_id, name=project_name)
    _issue(ws_db, issue_id)
    _link_supplier(ws_db, issue_id, party_id, email, company)
    ws_db.assign_issue_to_project(issue_id, project_id)
    wp.get_or_compute_work_object_signature(issue_id)


def test_supplier_entity_sweep_links_projects_that_never_became_pipeline2_candidates(ws_db):
    """The real gap this closes: two projects sharing nothing but a bare
    company name - Marc's own example, an EA Renewal and a later Copilot
    Pilot under the same vendor - with NO work_object_relationships row of
    any kind (since they'd never clear the 2+-point candidate gate in real
    operation). The existing rejected-candidate sweep has nothing to read
    here; this one still finds and links them."""
    _project_with_supplier(ws_db, "proj-ea", "Microsoft EA Renewal", "wo-ea", "p1", "rep@microsoft.com", "Microsoft")
    _project_with_supplier(ws_db, "proj-copilot", "Microsoft Copilot Pilot", "wo-copilot", "p2",
                            "rep2@microsoft.com", "MICROSOFT CORP")

    result = wr.run_supplier_entity_sweep()

    assert result["relationships_created"] == 1
    assert result["project_links_created"] == 2
    rels = ws_db.list_relationships_for_project("proj-ea")
    assert len(rels) == 1
    assert rels[0]["name"].lower() == "microsoft"
    linked_projects = {p["id"] for p in ws_db.list_projects_for_relationship(rels[0]["id"])}
    assert linked_projects == {"proj-ea", "proj-copilot"}


def test_supplier_entity_sweep_skips_a_company_appearing_in_only_one_project(ws_db):
    _project_with_supplier(ws_db, "proj-solo", "Solo Project", "wo-solo", "p1", "rep@onlyone.com", "OnlyOne")

    result = wr.run_supplier_entity_sweep()

    assert result["relationships_created"] == 0
    assert result["project_links_created"] == 0
    assert ws_db.list_relationships_for_project("proj-solo") == []


def test_supplier_entity_sweep_reuses_an_existing_relationship_for_a_third_project(ws_db):
    """Idempotent and cumulative: a company already linked to two projects
    by an earlier run picks up a third project on a later run without
    creating a second, duplicate Relationship."""
    _project_with_supplier(ws_db, "proj-1", "P1", "wo-1", "p1", "a@sodalis.com", "Sodalis")
    _project_with_supplier(ws_db, "proj-2", "P2", "wo-2", "p2", "b@sodalis.com", "Sodalis")
    wr.run_supplier_entity_sweep()

    _project_with_supplier(ws_db, "proj-3", "P3", "wo-3", "p3", "c@sodalis.com", "Sodalis")
    result = wr.run_supplier_entity_sweep()

    assert result["relationships_created"] == 0  # already exists from the first run
    assert result["project_links_created"] == 1  # only the new third project
    rels = ws_db.list_relationships_for_project("proj-1")
    assert len(rels) == 1
    all_projects = {p["id"] for p in ws_db.list_projects_for_relationship(rels[0]["id"])}
    assert all_projects == {"proj-1", "proj-2", "proj-3"}


def test_supplier_entity_sweep_daily_gate_runs_once_per_day(ws_db, monkeypatch):
    calls = []
    monkeypatch.setattr(wr, "run_supplier_entity_sweep", lambda: calls.append(1) or {"ok": True})

    first = wr.run_supplier_entity_sweep_daily_if_due(now=1000.0)
    second = wr.run_supplier_entity_sweep_daily_if_due(now=1000.0 + 3600)

    assert first == {"ok": True}
    assert second is None
    assert len(calls) == 1
