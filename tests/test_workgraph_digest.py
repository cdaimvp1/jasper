"""Regression tests for workgraph_digest.py (task #76, Weekly Digest).
Pure rollup over already-real numbers (workgraph_nba's scoring/value
extraction, workgraph_deadlines' hard/soft classification) - zero LLM,
zero new extraction. Fixed reference `now` throughout to avoid
week-boundary flakiness."""
from __future__ import annotations

import time

import workgraph_digest as wd

DAY = 86400.0
_NOW = time.time()


def _set_opened_at(ws_db, issue_id, ts):
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET opened_at = ? WHERE id = ?", (ts, issue_id))
    conn.close()


def _set_updated_at(ws_db, issue_id, ts):
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (ts, issue_id))
    conn.close()


def test_build_digest_all_zero_when_no_issues(ws_db):
    d = wd.build_digest(_NOW)
    assert d["opened_count"] == 0
    assert d["closed_count"] == 0
    assert d["open_count"] == 0
    assert d["hard_deadline_count"] == 0
    assert d["value_found_total"] == 0.0
    assert d["top_priority"] == []
    assert d["closed_this_week"] == []
    assert d["hard_deadline_issues"] == []


def test_opened_count_includes_recent_excludes_old(ws_db):
    recent = ws_db.create_issue_with_new_id(title="Recent", state="active", category="other")
    _set_opened_at(ws_db, recent, _NOW - 2 * DAY)
    old = ws_db.create_issue_with_new_id(title="Old", state="active", category="other")
    _set_opened_at(ws_db, old, _NOW - 30 * DAY)

    d = wd.build_digest(_NOW)

    assert d["opened_count"] == 1


def test_closed_count_requires_both_done_state_and_recent_update(ws_db):
    closed_recent = ws_db.create_issue_with_new_id(title="Closed recently", state="done", category="other")
    _set_updated_at(ws_db, closed_recent, _NOW - DAY)
    closed_old = ws_db.create_issue_with_new_id(title="Closed long ago", state="done", category="other")
    _set_updated_at(ws_db, closed_old, _NOW - 30 * DAY)
    still_open = ws_db.create_issue_with_new_id(title="Still open", state="active", category="other")
    _set_updated_at(ws_db, still_open, _NOW - DAY)

    d = wd.build_digest(_NOW)

    assert d["closed_count"] == 1
    assert [i["id"] for i in d["closed_this_week"]] == [closed_recent]


def test_open_count_covers_active_waiting_blocked_only(ws_db):
    ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.create_issue_with_new_id(title="W", state="waiting", category="other")
    ws_db.create_issue_with_new_id(title="B", state="blocked", category="other")
    ws_db.create_issue_with_new_id(title="D", state="done", category="other")
    ws_db.create_issue_with_new_id(title="N", state="noise-archived", category="other")

    d = wd.build_digest(_NOW)

    assert d["open_count"] == 3


def test_top_priority_sorted_descending_and_capped_at_five(ws_db):
    ids = []
    for i in range(7):
        iid = ws_db.create_issue_with_new_id(title=f"Issue {i}", state="active", category="other")
        ws_db.update_issue(iid, priority_score=float(i))
        ids.append(iid)

    d = wd.build_digest(_NOW)

    assert len(d["top_priority"]) == 5
    assert [t["id"] for t in d["top_priority"]] == list(reversed(ids))[:5]


def test_hard_deadline_issues_reflect_attach_deadline_info(ws_db):
    now = _NOW
    iid = ws_db.create_issue_with_new_id(title="Has a real deadline", state="active", category="other")
    ws_db.update_issue(iid, due=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 3 * DAY)))
    ws_db.create_issue_with_new_id(title="No deadline", state="active", category="other")

    d = wd.build_digest(now)

    assert d["hard_deadline_count"] == 1
    assert d["hard_deadline_issues"][0]["id"] == iid


def test_value_found_total_matches_value_at_risk_rollup(ws_db):
    import workgraph_nba as nba
    iid = ws_db.create_issue_with_new_id(title="Big deal", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="d1", thread_key="d1", dedupe_key="d1",
                                 occurred_ts=_NOW, subject="Worth $4 million", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)

    d = wd.build_digest(_NOW)

    assert d["value_found_total"] == nba.value_at_risk_rollup()["total"] == 4_000_000.0
