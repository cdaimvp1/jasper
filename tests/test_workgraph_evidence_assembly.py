"""Regression tests for workgraph_evidence_assembly.py's Evidence Assembly
(Section 8.1, 2026-08-03)."""
from __future__ import annotations

import time

import workgraph_evidence_assembly as ea


def test_no_evidence_returns_empty_selection(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    result = ea.assemble_evidence(iid, token_budget=1000)
    assert result == {"selected": [], "excluded_count": 0, "conflicts": [], "tokens_used": 0}


def test_all_evidence_selected_when_budget_is_generous(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.add_evidence(issue_id=iid, type="email", summary="short note one")
    ws_db.add_evidence(issue_id=iid, type="email", summary="short note two")

    result = ea.assemble_evidence(iid, token_budget=10_000)

    assert len(result["selected"]) == 2
    assert result["excluded_count"] == 0


def test_budget_excludes_lower_ranked_rows(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    now = time.time()
    for i in range(5):
        ws_db.add_evidence(issue_id=iid, type="email", summary="x" * 400)  # ~100 tokens each

    result = ea.assemble_evidence(iid, token_budget=250, now=now)

    assert len(result["selected"]) < 5
    assert result["excluded_count"] > 0
    assert result["tokens_used"] <= 250 or len(result["selected"]) == 1  # always includes >=1 row


def test_always_includes_at_least_one_row_even_over_budget(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.add_evidence(issue_id=iid, type="email", summary="y" * 40_000)  # way over any small budget

    result = ea.assemble_evidence(iid, token_budget=10)

    assert len(result["selected"]) == 1
    assert result["excluded_count"] == 0


def test_fresher_evidence_ranks_above_stale_evidence(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    now = time.time()
    conn = ws_db._connect()
    conn.execute("INSERT INTO evidence (issue_id, raw_item_id, type, summary, ts) VALUES (?, NULL, 'email', 'stale one', ?)",
                 (iid, now - 200 * 86400))
    conn.execute("INSERT INTO evidence (issue_id, raw_item_id, type, summary, ts) VALUES (?, NULL, 'email', 'fresh one', ?)",
                 (iid, now - 1 * 86400))
    conn.commit()
    conn.close()

    result = ea.assemble_evidence(iid, token_budget=10_000, now=now)

    scores_by_summary = {r["summary"]: r["assembly_score"] for r in result["selected"]}
    assert scores_by_summary["fresh one"] > scores_by_summary["stale one"]


def test_issue_with_real_reference_anchor_ranks_higher_than_without(ws_db):
    with_ref = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    without_ref = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.create_identity_anchor(anchor_type="reference", normalized_value="PR1", anchor_strength="strong",
                                  exclusive=True, issue_id=with_ref)
    ws_db.add_evidence(issue_id=with_ref, type="email", summary="note")
    ws_db.add_evidence(issue_id=without_ref, type="email", summary="note")

    result_with = ea.assemble_evidence(with_ref, token_budget=10_000)
    result_without = ea.assemble_evidence(without_ref, token_budget=10_000)

    assert result_with["selected"][0]["assembly_score"] > result_without["selected"][0]["assembly_score"]


def test_system_record_with_no_raw_item_gets_full_referential_resolution(ws_db):
    """A merge note (raw_item_id=NULL) is Jasper's own direct record, not
    a floating/ambiguous one - it should score at least as well on the
    referential axis as a real-reference row, even with no anchor at all."""
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.add_evidence(issue_id=iid, type="worker_action", summary="Merged X into this issue: reason")

    result = ea.assemble_evidence(iid, token_budget=10_000)

    assert len(result["selected"]) == 1
    assert result["selected"][0]["assembly_score"] > 0.5


def test_conflicts_always_empty_in_v0(ws_db):
    """Documented gap, not silently claimed coverage - see module docstring."""
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.add_evidence(issue_id=iid, type="email", summary="note")
    result = ea.assemble_evidence(iid, token_budget=10_000)
    assert result["conflicts"] == []


def test_deterministic_for_identical_inputs(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.add_evidence(issue_id=iid, type="email", summary="note one")
    ws_db.add_evidence(issue_id=iid, type="email", summary="note two")
    now = time.time()

    a = ea.assemble_evidence(iid, token_budget=10_000, now=now)
    b = ea.assemble_evidence(iid, token_budget=10_000, now=now)

    assert a == b
