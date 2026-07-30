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


def test_upsert_response_pattern_increments_hit_count_on_repeat(ws_db):
    ws_db.upsert_response_pattern("app_chat", "ariba", "first mention", 100.0)
    ws_db.upsert_response_pattern("app_chat", "ariba", "second mention", 200.0)
    rows = ws_db.list_response_patterns("app_chat")
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 2
    assert rows[0]["example_text"] == "second mention"
    assert rows[0]["last_seen_ts"] == 200.0
    assert rows[0]["first_seen_ts"] == 100.0  # set once on insert, never overwritten


def test_upsert_response_pattern_separate_surfaces_dont_collide(ws_db):
    ws_db.upsert_response_pattern("app_chat", "ariba", "x", 1.0)
    ws_db.upsert_response_pattern("sent_mail", "ariba", "y", 1.0)
    assert len(ws_db.list_response_patterns()) == 2
    assert len(ws_db.list_response_patterns("app_chat")) == 1


def test_clear_response_patterns_removes_everything(ws_db):
    ws_db.upsert_response_pattern("app_chat", "ariba", "x", 1.0)
    ws_db.upsert_response_pattern("app_chat", "sap", "y", 1.0)
    cleared = ws_db.clear_response_patterns()
    assert cleared == 2
    assert ws_db.list_response_patterns() == []


def test_get_socrates_log_since_dedupes_multi_tier_rows(ws_db):
    """append_socrates_log logs one row PER TIER for a single real question -
    get_socrates_log_since must collapse that back to one row per question."""
    ws_db.append_socrates_log(asked_ts=100.0, asker="marc", question="q1", signature="s1",
                               tier="recall", band="high", contributed=True, outcome="answered")
    ws_db.append_socrates_log(asked_ts=100.0, asker="marc", question="q1", signature="s1",
                               tier="materialized", band="high", contributed=False, outcome="answered")
    rows = ws_db.get_socrates_log_since(0)
    assert len(rows) == 1
    assert rows[0]["question"] == "q1"


def test_list_prerequisite_rules_active_only_filter(ws_db):
    r1 = ws_db.create_prerequisite_rule(trigger_signal_type="a", requires_signal_type="b",
                                         match_on="project", reason="x", created_by="marc")
    r2 = ws_db.create_prerequisite_rule(trigger_signal_type="c", requires_signal_type="d",
                                         match_on="supplier", reason="y", created_by="marc")
    ws_db.set_prerequisite_rule_active(r2, False)

    all_rules = ws_db.list_prerequisite_rules()
    active_rules = ws_db.list_prerequisite_rules(active_only=True)

    assert {r["id"] for r in all_rules} == {r1, r2}
    assert {r["id"] for r in active_rules} == {r1}


def test_delete_prerequisite_rule(ws_db):
    rule_id = ws_db.create_prerequisite_rule(trigger_signal_type="a", requires_signal_type="b",
                                              match_on="project", reason="x", created_by="marc")
    ws_db.delete_prerequisite_rule(rule_id)
    assert ws_db.list_prerequisite_rules() == []


def test_get_active_prerequisite_rules_for_trigger_empty_for_unknown_type(ws_db):
    assert ws_db.get_active_prerequisite_rules_for_trigger("nonexistent_signal") == []


def test_get_teams_messages_from_actor_since_matches_case_insensitively(ws_db):
    ws_db.insert_raw_item(source="teams_chat", stable_key="c1:m1", thread_key="c1",
                           dedupe_key="d1", occurred_ts=100.0, subject=None,
                           from_actor="Marc Lane", participants_json="[]", body_preview="hi team")
    ws_db.insert_raw_item(source="teams_chat", stable_key="c1:m2", thread_key="c1",
                           dedupe_key="d2", occurred_ts=200.0, subject=None,
                           from_actor="marc lane", participants_json="[]", body_preview="ping")
    ws_db.insert_raw_item(source="teams_chat", stable_key="c1:m3", thread_key="c1",
                           dedupe_key="d3", occurred_ts=300.0, subject=None,
                           from_actor="Someone Else", participants_json="[]", body_preview="pong")

    rows = ws_db.get_teams_messages_from_actor_since("Marc Lane", 0)

    assert [r["body_preview"] for r in rows] == ["hi team", "ping"]


def test_get_teams_messages_from_actor_since_excludes_at_or_before_cutoff(ws_db):
    ws_db.insert_raw_item(source="teams_chat", stable_key="c1:m1", thread_key="c1",
                           dedupe_key="d1", occurred_ts=100.0, subject=None,
                           from_actor="Marc Lane", participants_json="[]", body_preview="old")
    ws_db.insert_raw_item(source="teams_chat", stable_key="c1:m2", thread_key="c1",
                           dedupe_key="d2", occurred_ts=200.0, subject=None,
                           from_actor="Marc Lane", participants_json="[]", body_preview="new")

    rows = ws_db.get_teams_messages_from_actor_since("Marc Lane", 100.0)

    assert [r["body_preview"] for r in rows] == ["new"]


def test_get_teams_messages_from_actor_since_excludes_other_sources(ws_db):
    ws_db.insert_raw_item(source="outlook_mail", stable_key="m1", thread_key="m1",
                           dedupe_key="d1", occurred_ts=100.0, subject=None,
                           from_actor="Marc Lane", participants_json="[]", body_preview="an email")
    rows = ws_db.get_teams_messages_from_actor_since("Marc Lane", 0)
    assert rows == []


def test_get_socrates_log_since_excludes_at_or_before_cutoff(ws_db):
    ws_db.append_socrates_log(asked_ts=100.0, asker="marc", question="old", signature="s",
                               tier="recall", band="high", contributed=True, outcome="answered")
    ws_db.append_socrates_log(asked_ts=200.0, asker="marc", question="new", signature="s",
                               tier="recall", band="high", contributed=True, outcome="answered")
    rows = ws_db.get_socrates_log_since(100.0)
    assert [r["question"] for r in rows] == ["new"]
