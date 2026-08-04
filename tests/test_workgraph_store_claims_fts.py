"""Tests for the claims store layer's FTS helpers (Section 9.6) and the
revision/claim CRUD primitives workgraph_claims.py builds on."""
from __future__ import annotations


def test_index_and_search_evidence_fts(ws_db):
    ws_db.index_evidence_fts(1, "issue-a", "The Workday renewal PO needs a signature by Friday")
    ws_db.index_evidence_fts(2, "issue-b", "Unrelated SharePoint architecture notes")

    results = ws_db.search_evidence_fts("Workday")

    assert len(results) == 1
    assert results[0]["raw_item_id"] == 1
    assert results[0]["issue_id"] == "issue-a"


def test_search_evidence_fts_scoped_to_issue(ws_db):
    ws_db.index_evidence_fts(1, "issue-a", "renewal PO signature")
    ws_db.index_evidence_fts(2, "issue-b", "renewal contract terms")

    scoped = ws_db.search_evidence_fts("renewal", issue_id="issue-b")

    assert [r["raw_item_id"] for r in scoped] == [2]


def test_index_evidence_fts_is_idempotent_per_raw_item(ws_db):
    ws_db.index_evidence_fts(1, "issue-a", "first version of the text")
    ws_db.index_evidence_fts(1, "issue-a", "second version of the text")

    results = ws_db.search_evidence_fts("second")
    assert len(results) == 1
    stale = ws_db.search_evidence_fts("first")
    assert stale == []


def test_index_evidence_fts_skips_blank_body(ws_db):
    ws_db.index_evidence_fts(1, "issue-a", "   ")
    assert ws_db.search_evidence_fts("anything") == []


def test_get_claims_revision_defaults_to_zero(ws_db):
    iid = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    assert ws_db.get_claims_revision(iid) == 0


def test_get_project_claims_fingerprint_changes_when_a_member_gets_a_claim(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    iid1 = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    iid2 = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.assign_issue_to_project(iid1, pid)
    ws_db.assign_issue_to_project(iid2, pid)
    before = ws_db.get_project_claims_fingerprint(pid)

    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="fts1", thread_key="fts1", dedupe_key="fts1",
        occurred_ts=0.0, subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, iid1)
    ws_db.insert_claim(issue_id=iid1, raw_item_id=rid, claim_type="ask", text="x",
                        author="marc", author_basis="direction")
    ws_db.insert_claim(issue_id=iid1, raw_item_id=rid, claim_type="ask", text="y",
                        author="marc", author_basis="direction")

    after = ws_db.get_project_claims_fingerprint(pid)
    assert before != after
    assert ws_db.get_claims_revision(iid1) == 2
    assert ws_db.get_claims_revision(iid2) == 0


def test_get_project_claims_fingerprint_changes_when_membership_changes_alone(ws_db):
    """Task #175: the post-assignment synthesis-refresh Marc asked for ("once
    a new item is assigned to a project, the llm does a quick pass ... to
    synthesize it into the project's canon") needs no new code - it's this
    fingerprint plus scheduled_refresh's existing grouping-before-synthesis
    ordering. Locks in the half of get_project_claims_fingerprint's docstring
    the sibling test above doesn't cover: a brand-new member joining with
    ZERO claims of its own (the common bridge/merge shape - a new issue is
    assigned a project_id before anything about it has been re-extracted)
    still flips the fingerprint, purely from the membership SET changing."""
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    iid1 = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.assign_issue_to_project(iid1, pid)
    before = ws_db.get_project_claims_fingerprint(pid)

    iid2 = ws_db.create_issue_with_new_id(title="B (bridged in)", state="active", category="other")
    ws_db.assign_issue_to_project(iid2, pid)

    after = ws_db.get_project_claims_fingerprint(pid)
    assert before != after


def test_get_project_claims_fingerprint_stable_for_unchanged_members(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    iid1 = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.assign_issue_to_project(iid1, pid)

    first = ws_db.get_project_claims_fingerprint(pid)
    second = ws_db.get_project_claims_fingerprint(pid)
    assert first == second


def test_bump_claims_revision_increments_by_one(ws_db):
    iid = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    assert ws_db.get_claims_revision(iid) == 0
    ws_db.bump_claims_revision(iid)
    assert ws_db.get_claims_revision(iid) == 1
    ws_db.bump_claims_revision(iid)
    assert ws_db.get_claims_revision(iid) == 2
