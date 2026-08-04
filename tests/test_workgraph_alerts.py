"""Regression tests for workgraph_alerts.py:
- batched evidence/state-history queries produce identical results to the
  per-issue form (task #30 enhancement)
- DEFAULT_THRESHOLDS immutability (task #30 enhancement)
- end-to-end stale-alert generation still works after the batching rewrite
"""
import time

import pytest

import workgraph_alerts as wa


def test_default_thresholds_is_immutable():
    with pytest.raises(TypeError):
        wa.DEFAULT_THRESHOLDS["stale_warn_days"] = 999


def test_batched_evidence_matches_per_issue_queries(ws_db):
    iid1 = ws_db.create_issue_with_new_id(title="Issue A", state="waiting", category="other")
    iid2 = ws_db.create_issue_with_new_id(title="Issue B", state="blocked", category="other")
    ws_db.add_evidence(issue_id=iid1, type="email", summary="ev1")
    ws_db.add_evidence(issue_id=iid1, type="email", summary="ev2")
    ws_db.add_evidence(issue_id=iid2, type="email", summary="ev3")

    batched = ws_db.list_evidence_for_issues([iid1, iid2])
    assert [e["summary"] for e in batched[iid1]] == [e["summary"] for e in ws_db.list_evidence(iid1)]
    assert [e["summary"] for e in batched[iid2]] == [e["summary"] for e in ws_db.list_evidence(iid2)]


def test_batched_evidence_handles_issue_with_none(ws_db):
    iid1 = ws_db.create_issue_with_new_id(title="Has evidence", state="waiting", category="other")
    iid3 = ws_db.create_issue_with_new_id(title="No evidence", state="waiting", category="other")
    ws_db.add_evidence(issue_id=iid1, type="email", summary="ev")
    batched = ws_db.list_evidence_for_issues([iid1, iid3])
    assert batched.get(iid3, []) == []


def test_stale_alert_generated_end_to_end(ws_db, bus_db):
    """Full run() through the batched rewrite - a genuinely stale
    waiting/blocked issue must still produce a stale alert."""
    now = time.time()
    old_ts = now - 20 * 86400
    iid = ws_db.create_issue_with_new_id(title="Stale supplier thread", state="waiting", category="other")

    conn = ws_db._connect()
    conn.execute("UPDATE issue_state_history SET changed_ts = ? WHERE issue_id = ?", (old_ts, iid))
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (old_ts, iid))
    conn.commit()
    conn.close()

    result = wa.run(now=now)
    assert result["by_kind"]["stale"] == 1
    alerts = ws_db.list_alerts(dismissed=False)
    assert any(a["issue_id"] == iid and a["kind"] == "stale" for a in alerts)


def test_unmet_prerequisite_alert_generated_end_to_end(ws_db, bus_db):
    """Full run() - an issue workgraph_nba.py already flagged with
    has_unmet_prerequisite=1 (task #55) must produce a real, visible alert,
    reusing the exact nba_reason text rather than recomputing anything."""
    iid = ws_db.create_issue_with_new_id(title="Sign this", state="active", category="other")
    ws_db.update_issue(iid, has_unmet_prerequisite=1,
                        nba_reason="No confirmation seen yet of an approved PO — verify before proceeding")

    result = wa.run(now=time.time())

    assert result["by_kind"]["unmet_prerequisite"] == 1
    alerts = ws_db.list_alerts(dismissed=False)
    match = next(a for a in alerts if a["issue_id"] == iid and a["kind"] == "unmet_prerequisite")
    assert match["summary"] == "No confirmation seen yet of an approved PO — verify before proceeding"


def test_unmet_prerequisite_alert_deduped_across_runs(ws_db, bus_db):
    iid = ws_db.create_issue_with_new_id(title="Sign this", state="active", category="other")
    ws_db.update_issue(iid, has_unmet_prerequisite=1, nba_reason="No confirmation seen yet of X")

    first = wa.run(now=time.time())
    second = wa.run(now=time.time())

    assert first["by_kind"]["unmet_prerequisite"] == 1
    assert second["by_kind"]["unmet_prerequisite"] == 0  # already alerted, not re-created


def test_unmet_prerequisite_alert_not_generated_when_flag_is_zero(ws_db, bus_db):
    ws_db.create_issue_with_new_id(title="Fine", state="active", category="other")
    result = wa.run(now=time.time())
    assert result["by_kind"]["unmet_prerequisite"] == 0


def _issue_with_reference(ws_db, title, subject, key, ref="PR9999999"):
    iid = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject=subject, from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, iid)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number = ?, pr_number_base = ? WHERE id = ?", (ref, ref, rid))
    conn.commit()
    conn.close()
    return iid


# --- enhancement idea panel #14: reference-ID cross-check worker capability

def test_reference_id_collision_alert_generated_end_to_end(ws_db, bus_db):
    a = _issue_with_reference(ws_db, "Issue A", "Approve PR9999999", "alert-coll-a")
    b = _issue_with_reference(ws_db, "Issue B", "Re: PR9999999", "alert-coll-b")

    result = wa.run(now=time.time())

    assert result["by_kind"]["reference_id_collision"] == 1
    alerts = ws_db.list_alerts(dismissed=False)
    match = next(al for al in alerts if al["kind"] == "reference_id_collision")
    assert match["issue_id"] in (a, b)
    assert "PR9999999" in match["summary"]


def test_reference_id_collision_alert_deduped_across_runs(ws_db, bus_db):
    _issue_with_reference(ws_db, "Issue A", "Approve PR8888800", "alert-dedup-a", ref="PR8888800")
    _issue_with_reference(ws_db, "Issue B", "Re: PR8888800", "alert-dedup-b", ref="PR8888800")

    first = wa.run(now=time.time())
    second = wa.run(now=time.time())

    assert first["by_kind"]["reference_id_collision"] == 1
    assert second["by_kind"]["reference_id_collision"] == 0  # already alerted, not re-created


def test_reference_id_collision_alert_not_generated_without_collision(ws_db, bus_db):
    ws_db.create_issue_with_new_id(title="Fine", state="active", category="other")
    result = wa.run(now=time.time())
    assert result["by_kind"]["reference_id_collision"] == 0


# --- enhancement idea panel #16: conflicting dollar-figure flag ------------

def _issue_with_two_disagreeing_figures(ws_db):
    iid = ws_db.create_issue_with_new_id(title="Disputed deal", state="active", category="other")
    r1 = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="conflict-a", thread_key="conflict-a", dedupe_key="conflict-a",
        occurred_ts=time.time(), subject="Total contract value is $50,000",
        from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(r1, iid)
    r2 = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="conflict-b", thread_key="conflict-b", dedupe_key="conflict-b",
        occurred_ts=time.time(), subject="Updated: total contract value is $75,000",
        from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(r2, iid)
    return iid


def test_conflicting_value_figures_alert_generated_end_to_end(ws_db, bus_db):
    iid = _issue_with_two_disagreeing_figures(ws_db)

    result = wa.run(now=time.time())

    assert result["by_kind"]["conflicting_value_figures"] == 1
    alerts = ws_db.list_alerts(dismissed=False)
    match = next(al for al in alerts if al["kind"] == "conflicting_value_figures")
    assert match["issue_id"] == iid
    assert "$75,000" in match["summary"] and "$50,000" in match["summary"]


def test_conflicting_value_figures_alert_deduped_across_runs(ws_db, bus_db):
    _issue_with_two_disagreeing_figures(ws_db)

    first = wa.run(now=time.time())
    second = wa.run(now=time.time())

    assert first["by_kind"]["conflicting_value_figures"] == 1
    assert second["by_kind"]["conflicting_value_figures"] == 0  # already alerted, not re-created


def test_conflicting_value_figures_alert_not_generated_for_single_figure(ws_db, bus_db):
    iid = ws_db.create_issue_with_new_id(title="Fine", state="active", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="single-fig", thread_key="single-fig", dedupe_key="single-fig",
        occurred_ts=time.time(), subject="Total contract value is $50,000",
        from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, iid)

    result = wa.run(now=time.time())

    assert result["by_kind"]["conflicting_value_figures"] == 0


# --- enhancement idea panel #17: duplicate/conflicting ask across project --

def _conflicting_ask_across_project(ws_db):
    import json
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    a = ws_db.create_issue_with_new_id(title="Issue A", state="active", category="other")
    ws_db.assign_issue_to_project(a, pid)
    b = ws_db.create_issue_with_new_id(title="Issue B", state="active", category="other")
    ws_db.assign_issue_to_project(b, pid)

    import workgraph_claims as wc
    for iid, key, amount in ((a, "dup-alert-a", "$3,876,200.00"), (b, "dup-alert-b", "$1,938,100.00")):
        rid = ws_db.insert_raw_item(
            source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
            occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
        )
        ws_db.link_raw_item_to_issue(rid, iid)
        ws_db.create_extraction(rid, json.dumps({"asks": [f"Approve requisition PR9990001 for {amount}"]}))
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET direction = 'inbound', pr_number_base = 'PR9990001' WHERE id = ?", (rid,))
        conn.close()
        wc.materialize_claims_for_raw_item(rid)
    return a, b


def test_duplicate_ask_across_project_alert_generated_end_to_end(ws_db, bus_db):
    a, b = _conflicting_ask_across_project(ws_db)

    result = wa.run(now=time.time())

    assert result["by_kind"]["duplicate_ask_across_project"] == 1
    alerts = ws_db.list_alerts(dismissed=False)
    match = next(al for al in alerts if al["kind"] == "duplicate_ask_across_project")
    assert match["severity"] == "warn"  # conflicting, not identical text
    assert match["issue_id"] in (a, b)


def test_duplicate_ask_across_project_alert_deduped_across_runs(ws_db, bus_db):
    _conflicting_ask_across_project(ws_db)

    first = wa.run(now=time.time())
    second = wa.run(now=time.time())

    assert first["by_kind"]["duplicate_ask_across_project"] == 1
    assert second["by_kind"]["duplicate_ask_across_project"] == 0


def test_duplicate_ask_across_project_alert_not_generated_without_a_group(ws_db, bus_db):
    ws_db.create_issue_with_new_id(title="Fine", state="active", category="other")
    result = wa.run(now=time.time())
    assert result["by_kind"]["duplicate_ask_across_project"] == 0
