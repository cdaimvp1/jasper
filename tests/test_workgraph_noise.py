"""Regression tests for workgraph_noise.py (task #310 follow-up,
2026-08-11, Marc's own direct request after reviewing the Workload Status
Update Report's first real output - it included a literal youth-soccer-
team forward as a "project").

Covers classify_project_noise()'s real decision logic: only-personal-
email-domain participants, no PR/PO reference, no dollar figure - and the
honest non-matches (a real dollar figure, a real PR/PO reference, a real
external business domain, or an internal-only Lilly thread)."""
from __future__ import annotations

import workgraph_noise as wn


def _cluster_with_raw_item(ws_db, cluster_id, *, from_actor, pr_number=None):
    ws_db.create_cluster(id=cluster_id, title="Some thread")
    row_id = ws_db.insert_raw_item(
        source="mail", stable_key=f"sk-{cluster_id}", thread_key=f"tk-{cluster_id}",
        dedupe_key=f"dk-{cluster_id}", occurred_ts=1_700_000_000.0,
        subject="Some subject", from_actor=from_actor, participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(row_id, cluster_id)
    if pr_number:
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET pr_number = ? WHERE id = ?", (pr_number, row_id))
        conn.commit()
        conn.close()
    return cluster_id, row_id


def _claim(ws_db, cluster_id, row_id, *, text, claim_type="ask"):
    ws_db.insert_claim(issue_id=cluster_id, raw_item_id=row_id, claim_type=claim_type,
                        text=text, author="unknown", author_basis="unresolved")


def test_personal_domain_only_no_reference_no_dollar_is_noise(ws_db):
    ws_db.create_project(id="proj-soccer", name="Soccer")
    cid, rid = _cluster_with_raw_item(ws_db, "wo-soccer", from_actor="coach@gmail.com")
    ws_db.assign_issue_to_project(cid, "proj-soccer")
    _claim(ws_db, cid, rid, text="Confirm roster spot by Friday", claim_type="ask")

    reason = wn.classify_project_noise("proj-soccer")

    assert reason is not None
    assert "gmail.com" in reason


def test_real_dollar_figure_is_not_noise(ws_db):
    ws_db.create_project(id="proj-personal-deal", name="Personal but has $")
    cid, rid = _cluster_with_raw_item(ws_db, "wo-pd", from_actor="someone@yahoo.com")
    ws_db.assign_issue_to_project(cid, "proj-personal-deal")
    _claim(ws_db, cid, rid, text="Approved for $50,000 payment", claim_type="commitment")

    assert wn.classify_project_noise("proj-personal-deal") is None


def test_real_pr_reference_is_not_noise(ws_db):
    ws_db.create_project(id="proj-pr", name="Has a PR number")
    cid, rid = _cluster_with_raw_item(ws_db, "wo-pr", from_actor="someone@hotmail.com",
                                       pr_number="PR1234567")
    ws_db.assign_issue_to_project(cid, "proj-pr")
    _claim(ws_db, cid, rid, text="Approve the requisition", claim_type="ask")

    assert wn.classify_project_noise("proj-pr") is None


def test_real_external_business_domain_is_not_noise(ws_db):
    ws_db.create_project(id="proj-sap", name="SAP deal")
    cid, rid = _cluster_with_raw_item(ws_db, "wo-sap", from_actor="rep@sap.com")
    ws_db.assign_issue_to_project(cid, "proj-sap")
    _claim(ws_db, cid, rid, text="Please sign the order form", claim_type="ask")

    assert wn.classify_project_noise("proj-sap") is None


def test_internal_only_lilly_thread_is_not_noise(ws_db):
    ws_db.create_project(id="proj-internal", name="Internal approval")
    cid, rid = _cluster_with_raw_item(ws_db, "wo-internal", from_actor="brian.laughlin@lilly.com")
    ws_db.assign_issue_to_project(cid, "proj-internal")
    _claim(ws_db, cid, rid, text="Approve the requisition internally", claim_type="ask")

    assert wn.classify_project_noise("proj-internal") is None


def test_run_noise_sweep_archives_only_the_real_noise_project(ws_db):
    ws_db.create_project(id="proj-soccer", name="Soccer")
    cid1, rid1 = _cluster_with_raw_item(ws_db, "wo-soccer", from_actor="coach@gmail.com")
    ws_db.assign_issue_to_project(cid1, "proj-soccer")
    _claim(ws_db, cid1, rid1, text="Confirm roster spot", claim_type="ask")

    ws_db.create_project(id="proj-sap", name="SAP deal")
    cid2, rid2 = _cluster_with_raw_item(ws_db, "wo-sap", from_actor="rep@sap.com")
    ws_db.assign_issue_to_project(cid2, "proj-sap")
    _claim(ws_db, cid2, rid2, text="Please sign the order form", claim_type="ask")

    result = wn.run_noise_sweep()

    assert result["reclassified_count"] == 1
    assert result["reclassified"][0]["project_id"] == "proj-soccer"
    assert ws_db.get_project("proj-soccer")["status"] == "noise-archived"
    assert ws_db.get_project("proj-sap")["status"] == "active"
