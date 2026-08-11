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
