"""Regression tests for workgraph_store.py:
- ORDER BY tie-break on list_issues (task #30 enhancement)
- create_issue_with_new_id / create_project_with_new_id / create_task retry
"""
import sqlite3
import time


def test_list_issues_tiebreak_is_stable_and_deterministic(ws_db):
    ids = [ws_db.create_issue_with_new_id(title=f"Issue {i}", state="active", category="other")
           for i in range(5)]

    same_ts = time.time()
    conn = ws_db._connect()
    for iid in ids:
        conn.execute("UPDATE issues SET priority_score = 0.5, updated_at = ? WHERE id = ?", (same_ts, iid))
    conn.commit()
    conn.close()

    runs = [[r["id"] for r in ws_db.list_issues(states=["active"], limit=100)] for _ in range(3)]
    assert runs[0] == runs[1] == runs[2], "order must be stable across repeated calls for fully-tied rows"
    assert runs[0] == sorted(runs[0]), "tied rows must sort by id ascending as the final tie-break"


def test_create_issue_with_new_id_never_collides_id(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    assert a != b
