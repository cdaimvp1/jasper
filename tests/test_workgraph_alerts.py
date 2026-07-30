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
