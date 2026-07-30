"""Regression tests for workgraph_export.py (task #68, CSV export of a
date-ranged issue list). Pure aggregation over workgraph_store.list_issues
- zero LLM, no interpretation."""
from __future__ import annotations

import csv
import io
import time

import workgraph_export as we

DAY = 86400.0


def _set_updated_at(ws_db, issue_id, ts):
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (ts, issue_id))
    conn.close()


def _rows(csv_text):
    return list(csv.DictReader(io.StringIO(csv_text)))


def test_issues_csv_header_only_when_nothing_matches(ws_db):
    now = time.time()
    out = we.issues_csv(now - DAY, now + DAY)
    rows = _rows(out)
    assert rows == []
    assert out.strip().split(",")[0] == "id"


def test_issues_csv_includes_matching_issue(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Renewal talk", state="active", category="other")
    _set_updated_at(ws_db, iid, now)

    rows = _rows(we.issues_csv(now - DAY, now + DAY))

    assert len(rows) == 1
    assert rows[0]["id"] == iid
    assert rows[0]["title"] == "Renewal talk"
    assert rows[0]["state"] == "active"


def test_issues_csv_excludes_issue_before_range(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Too old", state="active", category="other")
    _set_updated_at(ws_db, iid, now - 10 * DAY)

    rows = _rows(we.issues_csv(now - DAY, now + DAY))

    assert rows == []


def test_issues_csv_excludes_issue_after_range(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Too new", state="active", category="other")
    _set_updated_at(ws_db, iid, now + 10 * DAY)

    rows = _rows(we.issues_csv(now - DAY, now + DAY))

    assert rows == []


def test_issues_csv_boundary_inclusive_on_both_ends(ws_db):
    now = time.time()
    start_ts, end_ts = now - DAY, now + DAY
    on_start = ws_db.create_issue_with_new_id(title="At start", state="active", category="other")
    _set_updated_at(ws_db, on_start, start_ts)
    on_end = ws_db.create_issue_with_new_id(title="At end", state="active", category="other")
    _set_updated_at(ws_db, on_end, end_ts)

    rows = _rows(we.issues_csv(start_ts, end_ts))

    assert {r["id"] for r in rows} == {on_start, on_end}


def test_issues_csv_filters_by_state(ws_db):
    now = time.time()
    active_id = ws_db.create_issue_with_new_id(title="Active one", state="active", category="other")
    _set_updated_at(ws_db, active_id, now)
    done_id = ws_db.create_issue_with_new_id(title="Done one", state="done", category="other")
    _set_updated_at(ws_db, done_id, now)

    rows = _rows(we.issues_csv(now - DAY, now + DAY, states=["active"]))

    assert [r["id"] for r in rows] == [active_id]


def test_issues_csv_no_state_filter_includes_every_state(ws_db):
    now = time.time()
    active_id = ws_db.create_issue_with_new_id(title="Active one", state="active", category="other")
    _set_updated_at(ws_db, active_id, now)
    done_id = ws_db.create_issue_with_new_id(title="Done one", state="done", category="other")
    _set_updated_at(ws_db, done_id, now)

    rows = _rows(we.issues_csv(now - DAY, now + DAY))

    assert {r["id"] for r in rows} == {active_id, done_id}


def test_issues_csv_sorted_oldest_first(ws_db):
    now = time.time()
    newer = ws_db.create_issue_with_new_id(title="Newer", state="active", category="other")
    _set_updated_at(ws_db, newer, now)
    older = ws_db.create_issue_with_new_id(title="Older", state="active", category="other")
    _set_updated_at(ws_db, older, now - 5000)

    rows = _rows(we.issues_csv(now - DAY, now + DAY))

    assert [r["id"] for r in rows] == [older, newer]


def test_issues_csv_escapes_commas_and_quotes_in_title(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title='Deal, with a "quoted" clause', state="active", category="other")
    _set_updated_at(ws_db, iid, now)

    rows = _rows(we.issues_csv(now - DAY, now + DAY))

    assert rows[0]["title"] == 'Deal, with a "quoted" clause'
