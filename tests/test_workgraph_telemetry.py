"""Regression tests for workgraph_telemetry.py (task #304, item #4) -
compute_accuracy_metrics's real arithmetic (rates, None-when-no-denominator)
against the real counter functions in workgraph_store.py."""
from __future__ import annotations

import time

import workgraph_telemetry as wt


def test_compute_accuracy_metrics_all_zero_on_a_fresh_db(ws_db):
    now = time.time()
    result = wt.compute_accuracy_metrics(window_start=now - 3600, window_end=now)

    assert result["merge_events"] == 0
    assert result["false_merge_corrections"] == 0
    assert result["false_merge_correction_rate"] is None
    assert result["false_split_catches"] == 0
    assert result["claim_corrections_in_window"] == 0
    assert result["materialized_extractions_total"] == 0
    assert result["claim_correction_rate"] is None


def test_compute_accuracy_metrics_computes_false_merge_correction_rate(ws_db):
    conn = ws_db._connect()
    now = time.time()
    for i in range(4):
        conn.execute(
            "INSERT INTO audit_log (entity_type, entity_id, field, old_value, new_value, changed_ts, reason) "
            "VALUES ('project', ?, 'issue_membership', NULL, ?, ?, 'merge')",
            (f"proj-{i}", f"proj-{i}", now),
        )
    conn.commit()
    conn.close()
    ws_db.create_identity_constraint("cannot_merge", "a", "b", "split", actor="marc")

    result = wt.compute_accuracy_metrics(window_start=now - 60, window_end=now + 60)

    assert result["merge_events"] == 4
    assert result["false_merge_corrections"] == 1
    assert result["false_merge_correction_rate"] == 0.25


def test_compute_accuracy_metrics_reports_false_split_catches_as_raw_count(ws_db):
    conn = ws_db._connect()
    now = time.time()
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, field, old_value, new_value, changed_ts, reason) "
        "VALUES ('issue', 'wo-1', 'absorbed_cluster', 'wo-1', 'marc-1', ?, 'same pr_number_base')",
        (now,),
    )
    conn.commit()
    conn.close()

    result = wt.compute_accuracy_metrics(window_start=now - 60, window_end=now + 60)

    assert result["false_split_catches"] == 1


def test_compute_accuracy_metrics_computes_claim_correction_rate(ws_db):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    iid = ws_db.create_issue_with_new_id(title="Issue", category="other", state="active")
    ws_db.create_extraction(rid, "{}")
    now = time.time()
    ws_db.reconcile_extraction_claims(
        issue_id=iid, raw_item_id=rid,
        to_insert=[{"claim_type": "ask", "text": "Do the thing.", "author": "counterparty",
                    "author_basis": "direction"}],
        to_supersede=[], new_materialized_hash="h1",
    )

    result = wt.compute_accuracy_metrics(window_start=now - 60, window_end=now + 60)

    assert result["materialized_extractions_total"] == 1
    assert result["claim_corrections_in_window"] == 1
    assert result["claim_correction_rate"] == 1.0
