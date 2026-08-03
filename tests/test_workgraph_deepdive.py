"""Tests for workgraph_deepdive.py (design doc Section 10): the picker and
seed-derivation scaffolding around Project Deep-Dive. The live M365 search
itself is real judgment work in ingest/PROJECT_DEEPDIVE_ROUTINE.md, not
unit-tested here - same split as workgraph_synthesis.py vs
SYNTHESIS_ROUTINE.md."""
from __future__ import annotations

import time

import workgraph_deepdive as wdd


def _project(ws_db, name="Project", status="active"):
    return ws_db.create_project_with_new_id(name=name, category="other", status=status)


def test_never_deep_dived_project_is_a_candidate(ws_db):
    pid = _project(ws_db)
    candidates = wdd.list_deepdive_candidates(limit=10)
    assert any(p["id"] == pid for p in candidates)


def test_done_and_archived_and_dismissed_projects_excluded(ws_db):
    for status in ("done", "archived", "dismissed"):
        _project(ws_db, f"Closed {status}", status=status)

    candidates = wdd.list_deepdive_candidates(limit=10)

    assert candidates == []


def test_never_deep_dived_ranked_before_already_deep_dived(ws_db):
    pid_old = _project(ws_db, "Deep-dived once")
    ws_db.mark_project_deep_dived(pid_old, "found nothing new")
    pid_new = _project(ws_db, "Never touched")

    candidates = wdd.list_deepdive_candidates(limit=10)
    ranked_ids = [p["id"] for p in candidates]

    assert ranked_ids.index(pid_new) < ranked_ids.index(pid_old)


def test_oldest_last_deep_dive_ranked_first_among_touched(ws_db):
    pid_recent = _project(ws_db, "Recently touched")
    pid_stale = _project(ws_db, "Long overdue")
    now = time.time()
    ws_db.mark_project_deep_dived(pid_recent, "recent")
    conn = ws_db._connect()
    conn.execute("UPDATE projects SET last_deep_dive_ts = ? WHERE id = ?", (now - 999999, pid_stale))
    conn.close()

    candidates = wdd.list_deepdive_candidates(limit=10)
    ranked_ids = [p["id"] for p in candidates]

    assert ranked_ids.index(pid_stale) < ranked_ids.index(pid_recent)


def test_limit_caps_the_result(ws_db):
    for i in range(5):
        _project(ws_db, f"Project {i}")

    assert len(wdd.list_deepdive_candidates(limit=1)) == 1
    assert len(wdd.list_deepdive_candidates(limit=3)) == 3


def test_default_limit_is_one():
    assert wdd.DEFAULT_DEEPDIVE_LIMIT == 1


# --- derive_seeds_for_project ---------------------------------------------

def test_seeds_include_project_name(ws_db):
    pid = _project(ws_db, "Workday HCM Renewal")
    seeds = wdd.derive_seeds_for_project(pid)
    assert seeds["name"] == "Workday HCM Renewal"
    assert seeds["project_id"] == pid


def test_seeds_include_anchors_across_member_issues(ws_db):
    pid = _project(ws_db, "Multi-issue project")
    iid1 = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    iid2 = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.assign_issue_to_project(iid1, pid)
    ws_db.assign_issue_to_project(iid2, pid)
    ws_db.create_identity_anchor(
        anchor_type="reference", normalized_value="pr1193376", anchor_strength="exact",
        exclusive=True, issue_id=iid1, created_by="test", now=time.time(),
    )
    ws_db.create_identity_anchor(
        anchor_type="company", normalized_value="workday", anchor_strength="weak",
        exclusive=False, issue_id=iid2, created_by="test", now=time.time(),
    )

    seeds = wdd.derive_seeds_for_project(pid)

    values = {(a["anchor_type"], a["normalized_value"]) for a in seeds["anchors"]}
    assert ("reference", "pr1193376") in values
    assert ("company", "workday") in values


def test_seeds_dedupe_identical_anchors_across_issues(ws_db):
    pid = _project(ws_db, "Dup anchors")
    iid1 = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    iid2 = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.assign_issue_to_project(iid1, pid)
    ws_db.assign_issue_to_project(iid2, pid)
    for iid in (iid1, iid2):
        ws_db.create_identity_anchor(
            anchor_type="company", normalized_value="workday", anchor_strength="weak",
            exclusive=False, issue_id=iid, created_by="test", now=time.time(),
        )

    seeds = wdd.derive_seeds_for_project(pid)

    assert len(seeds["anchors"]) == 1


def test_seeds_for_unknown_project_returns_empty(ws_db):
    seeds = wdd.derive_seeds_for_project("proj-does-not-exist")
    assert seeds == {"project_id": "proj-does-not-exist", "name": None, "anchors": []}


def test_seeds_for_project_with_no_issues_has_no_anchors(ws_db):
    pid = _project(ws_db, "Empty project")
    seeds = wdd.derive_seeds_for_project(pid)
    assert seeds["anchors"] == []


# --- mark_project_deep_dived (store layer) --------------------------------

def test_mark_project_deep_dived_sets_ts_and_note(ws_db):
    pid = _project(ws_db)
    ws_db.mark_project_deep_dived(pid, "searched Teams/Calendar, found nothing new")

    project = ws_db.get_project(pid)
    assert project["last_deep_dive_ts"] is not None
    assert project["last_deep_dive_note"] == "searched Teams/Calendar, found nothing new"


def test_marked_project_drops_out_of_the_immediate_candidate_pool(ws_db):
    pid = _project(ws_db)
    ws_db.mark_project_deep_dived(pid, "done")
    other_pid = _project(ws_db, "Fresh one")

    candidates = wdd.list_deepdive_candidates(limit=1)

    assert [p["id"] for p in candidates] == [other_pid]
