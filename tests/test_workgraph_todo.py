"""Regression tests for workgraph_todo.py (task #281, "what's my to-do
list?"). Pure aggregation over attachments/claims/suppliers - no LLM, no
new extraction - so these just check the grouping/counting is right."""
from __future__ import annotations

import time

import workgraph_todo as wt


def _party(ws_db, party_id, company, email=None):
    ws_db.upsert_party(id=party_id, primary_email=email or f"{party_id}@example.com",
                        display_name=party_id, affiliation="external",
                        affiliation_confidence="H", affiliation_source="domain", company=company)


def test_build_todo_summary_empty_db(ws_db):
    summary = wt.build_todo_summary()

    assert summary["outputs_waiting"] == []
    assert summary["open_claims"] == {"total": 0, "issue_count": 0, "by_type": {}, "items": [], "truncated": False}
    assert summary["by_supplier"] == []


def test_build_todo_summary_includes_unreviewed_worker_output(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="redline", filename="redline.docx",
        stored_path="p.docx", content_type=None, size_bytes=10, sha256_hex=None, uploaded_by="bridge",
    )

    summary = wt.build_todo_summary()

    assert len(summary["outputs_waiting"]) == 1
    assert summary["outputs_waiting"][0]["filename"] == "redline.docx"


def test_build_todo_summary_counts_open_claims_by_type(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.insert_claim(issue_id=a, raw_item_id=rid, claim_type="ask", text="can we approve this?",
                        author="counterparty", author_basis="direction")
    ws_db.insert_claim(issue_id=a, raw_item_id=rid, claim_type="decision", text="pricing confirmed",
                        author="marc", author_basis="direction")

    summary = wt.build_todo_summary()

    assert summary["open_claims"]["total"] == 2
    assert summary["open_claims"]["issue_count"] == 1
    assert summary["open_claims"]["by_type"] == {"ask": 1, "decision": 1}
    titles = {item["issue_title"] for item in summary["open_claims"]["items"]}
    assert titles == {"A"}


def test_build_todo_summary_excludes_claims_on_closed_issues(ws_db):
    a = ws_db.create_issue_with_new_id(title="Closed", state="done", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.insert_claim(issue_id=a, raw_item_id=rid, claim_type="ask", text="stale ask",
                        author="counterparty", author_basis="direction")

    summary = wt.build_todo_summary()

    assert summary["open_claims"]["total"] == 0


def test_build_todo_summary_groups_active_issues_by_supplier(ws_db):
    _party(ws_db, "p1", "Acme")
    iid = ws_db.create_issue_with_new_id(title="Acme deal", state="active", category="other")
    ws_db.link_party_to_issue(iid, "p1")

    summary = wt.build_todo_summary()

    assert len(summary["by_supplier"]) == 1
    assert summary["by_supplier"][0]["company"] == "Acme"
    assert summary["by_supplier"][0]["open_issue_count"] == 1
    assert summary["by_supplier"][0]["issues"] == [{"id": iid, "title": "Acme deal"}]


def test_build_todo_summary_caps_claim_items_but_reports_true_total(ws_db, monkeypatch):
    monkeypatch.setattr(wt, "_MAX_CLAIM_ITEMS", 2)
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]")
    for i in range(3):
        ws_db.insert_claim(issue_id=a, raw_item_id=rid, claim_type="ask", text=f"ask {i}",
                            author="counterparty", author_basis="direction")

    summary = wt.build_todo_summary()

    assert summary["open_claims"]["total"] == 3
    assert len(summary["open_claims"]["items"]) == 2
    assert summary["open_claims"]["truncated"] is True
