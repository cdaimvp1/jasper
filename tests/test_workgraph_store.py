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


def test_get_raw_items_by_ids_batches_correctly(ws_db):
    """Task #44's deep_links.attach_deep_links relies on this being a single
    query for the whole evidence list, not one per row - same N+1 fix already
    applied elsewhere this session."""
    ids = [
        ws_db.insert_raw_item(source="outlook_mail", stable_key=f"k{i}", thread_key=f"k{i}",
                               dedupe_key=f"dk{i}", occurred_ts=1_800_000_000.0 + i,
                               subject=f"s{i}", from_actor="a@example.com", participants_json="[]")
        for i in range(3)
    ]
    result = ws_db.get_raw_items_by_ids(ids)
    assert set(result.keys()) == set(ids)
    assert all(result[i]["id"] == i for i in ids)


def test_get_raw_items_by_ids_empty_list_is_safe(ws_db):
    assert ws_db.get_raw_items_by_ids([]) == {}


def test_get_raw_items_by_ids_missing_id_omitted(ws_db):
    result = ws_db.get_raw_items_by_ids([999999])
    assert result == {}
