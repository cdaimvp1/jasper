"""Tests for workgraph_lifecycle.py (task #310 follow-up, Fix 4, 2026-08-11):
the 'dormant' lifecycle state - evidence-driven staleness detection and
auto-revert on new evidence."""
from __future__ import annotations

import time

import workgraph_lifecycle as wl

_DAY = 24 * 3600


def _issue(ws_db, title="Issue", state="active"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category="other")


def _raw_item_with_ts(ws_db, issue_id, key, occurred_ts):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=occurred_ts, subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    return rid


def test_dormant_sweep_flips_issue_with_only_old_evidence(ws_db):
    now = time.time()
    a = _issue(ws_db)
    _raw_item_with_ts(ws_db, a, "k1", now - 90 * _DAY)

    result = wl.run_dormant_sweep(now=now, threshold_seconds=60 * _DAY)

    assert a in result["dormant_issues"]
    assert ws_db.get_issue(a)["state"] == "dormant"


def test_dormant_sweep_leaves_recently_touched_issue_alone(ws_db):
    now = time.time()
    a = _issue(ws_db)
    _raw_item_with_ts(ws_db, a, "k2", now - 5 * _DAY)

    result = wl.run_dormant_sweep(now=now, threshold_seconds=60 * _DAY)

    assert a not in result["dormant_issues"]
    assert ws_db.get_issue(a)["state"] == "active"


def test_dormant_sweep_ignores_issue_with_zero_evidence(ws_db):
    """No raw_items, no claims at all - a different, pre-existing gap,
    never this sweep's job to guess about."""
    now = time.time()
    a = _issue(ws_db)

    result = wl.run_dormant_sweep(now=now, threshold_seconds=60 * _DAY)

    assert a not in result["dormant_issues"]
    assert ws_db.get_issue(a)["state"] == "active"


def test_dormant_sweep_flips_project_via_stale_member_evidence(ws_db):
    now = time.time()
    project_id = ws_db.create_project_with_new_id(name="Stale project", category="other")
    a = _issue(ws_db)
    ws_db.assign_issue_to_project(a, project_id)
    _raw_item_with_ts(ws_db, a, "k3", now - 90 * _DAY)

    result = wl.run_dormant_sweep(now=now, threshold_seconds=60 * _DAY)

    assert project_id in result["dormant_projects"]
    assert ws_db.get_project(project_id)["status"] == "dormant"


def test_dormant_sweep_leaves_project_alone_with_recent_member_evidence(ws_db):
    now = time.time()
    project_id = ws_db.create_project_with_new_id(name="Fresh project", category="other")
    a = _issue(ws_db)
    ws_db.assign_issue_to_project(a, project_id)
    _raw_item_with_ts(ws_db, a, "k4", now - 5 * _DAY)

    result = wl.run_dormant_sweep(now=now, threshold_seconds=60 * _DAY)

    assert project_id not in result["dormant_projects"]
    assert ws_db.get_project(project_id)["status"] == "active"


def test_revert_dormant_if_needed_wakes_dormant_issue(ws_db):
    a = _issue(ws_db, state="dormant")

    wl.revert_dormant_if_needed(a)

    assert ws_db.get_issue(a)["state"] == "active"


def test_revert_dormant_if_needed_wakes_dormant_project(ws_db):
    project_id = ws_db.create_project_with_new_id(name="Dormant project", category="other")
    ws_db.set_project_status(project_id, "dormant")
    a = _issue(ws_db)
    ws_db.assign_issue_to_project(a, project_id)

    wl.revert_dormant_if_needed(a)

    assert ws_db.get_project(project_id)["status"] == "active"


def test_revert_dormant_if_needed_is_a_noop_when_nothing_is_dormant(ws_db):
    a = _issue(ws_db, state="active")

    wl.revert_dormant_if_needed(a)  # must not raise or change anything

    assert ws_db.get_issue(a)["state"] == "active"
