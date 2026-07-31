"""Regression tests for workgraph_store.py:
- ORDER BY tie-break on list_issues (task #30 enhancement)
- create_issue_with_new_id / create_project_with_new_id / create_task retry
"""
import sqlite3
import time

import pytest


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


def test_get_issues_by_ids_batches_correctly(ws_db):
    """Hardening pass #3: workgraph_suppliers.list_suppliers() was calling
    get_issue() once per issue - same N+1 fix already applied to raw_items/
    extractions/state_history this session."""
    ids = [ws_db.create_issue_with_new_id(title=f"Issue {i}", state="active", category="other")
           for i in range(3)]
    result = ws_db.get_issues_by_ids(ids)
    assert set(result.keys()) == set(ids)
    assert all(result[i]["id"] == i for i in ids)


def test_get_issues_by_ids_empty_list_is_safe(ws_db):
    assert ws_db.get_issues_by_ids([]) == {}


def test_get_issues_by_ids_missing_id_omitted(ws_db):
    assert ws_db.get_issues_by_ids(["does-not-exist"]) == {}


def test_get_raw_items_for_issues_batches_correctly(ws_db):
    """Hardening pass #3: workgraph_nba.value_amount_for_issue() (via
    get_raw_items_for_issue) was called once per open issue inside
    workgraph_suppliers.list_suppliers()'s per-company loop."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    r1 = ws_db.insert_raw_item(source="outlook_mail", stable_key="ri1", thread_key="ri1", dedupe_key="ri1",
                                occurred_ts=100.0, subject="s1", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(r1, a)
    r2 = ws_db.insert_raw_item(source="outlook_mail", stable_key="ri2", thread_key="ri2", dedupe_key="ri2",
                                occurred_ts=100.0, subject="s2", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(r2, b)

    result = ws_db.get_raw_items_for_issues([a, b])

    assert set(result.keys()) == {a, b}
    assert [r["id"] for r in result[a]] == [r1]
    assert [r["id"] for r in result[b]] == [r2]


def test_get_raw_items_for_issues_empty_list_is_safe(ws_db):
    assert ws_db.get_raw_items_for_issues([]) == {}


def test_get_raw_items_for_issues_issue_with_none_omitted(ws_db):
    iid = ws_db.create_issue_with_new_id(title="No items", state="active", category="other")
    assert ws_db.get_raw_items_for_issues([iid]) == {}


def test_list_open_issue_ids_for_reference_finds_matching_issue(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="pr1", thread_key="pr1", dedupe_key="pr1",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number = ? WHERE id = ?", ("PR999999", rid))
    conn.close()

    assert ws_db.list_open_issue_ids_for_reference("PR999999") == [iid]


def test_list_open_issue_ids_for_reference_excludes_closed_issues(ws_db):
    iid = ws_db.create_issue_with_new_id(title="Closed", state="done", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="pr2", thread_key="pr2", dedupe_key="pr2",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number = ? WHERE id = ?", ("PR888888", rid))
    conn.close()

    assert ws_db.list_open_issue_ids_for_reference("PR888888") == []


def test_list_open_issue_ids_for_reference_empty_for_unknown_reference(ws_db):
    assert ws_db.list_open_issue_ids_for_reference("PR000000") == []


def test_list_open_issue_ids_for_reference_empty_string_is_safe(ws_db):
    assert ws_db.list_open_issue_ids_for_reference("") == []


def test_list_open_issue_ids_for_reference_orders_most_recently_updated_first(ws_db):
    older = ws_db.create_issue_with_new_id(title="Older", state="active", category="other")
    newer = ws_db.create_issue_with_new_id(title="Newer", state="active", category="other")
    for iid, key in ((older, "pr3a"), (newer, "pr3b")):
        rid = ws_db.insert_raw_item(source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
                                     occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                                     participants_json="[]")
        ws_db.link_raw_item_to_issue(rid, iid)
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET pr_number = ? WHERE id = ?", ("PR777000", rid))
        conn.close()
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (100.0, older))
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (200.0, newer))
    conn.close()

    assert ws_db.list_open_issue_ids_for_reference("PR777000") == [newer, older]


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


def test_list_response_patterns_tiebreak_is_stable_and_deterministic(ws_db):
    """Fixed 2026-07-30 (adversarial review round #2): ORDER BY hit_count
    DESC alone has no tie-break for two patterns with equal hit_count -
    the same non-determinism class already fixed 3x elsewhere this session.
    first_seen_ts ascending must win ties, deterministically, every call."""
    ws_db.upsert_response_pattern("app_chat", "sap", "x", 200.0)
    ws_db.upsert_response_pattern("app_chat", "docusign", "y", 100.0)
    ws_db.upsert_response_pattern("app_chat", "ariba", "z", 150.0)

    runs = [[r["pattern_key"] for r in ws_db.list_response_patterns("app_chat")] for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    assert runs[0] == ["docusign", "ariba", "sap"], "all tied at hit_count=1, earliest first_seen_ts must sort first"


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


def test_list_issues_with_unmet_prerequisite(ws_db):
    issue_a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    issue_b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    issue_c = ws_db.create_issue_with_new_id(title="C", state="done", category="other")
    ws_db.update_issue(issue_a, has_unmet_prerequisite=1)
    ws_db.update_issue(issue_c, has_unmet_prerequisite=1)

    results = {i["id"] for i in ws_db.list_issues_with_unmet_prerequisite()}

    assert results == {issue_a}  # issue_b never flagged, issue_c excluded (done)


def test_alerts_table_accepts_unmet_prerequisite_kind(ws_db):
    """Confirms the CHECK constraint migration in init_workgraph() actually
    took effect - not just that CREATE TABLE IF NOT EXISTS silently no-opped
    against an old constraint."""
    alert_id = ws_db.create_alert(issue_id=None, kind="unmet_prerequisite", severity="warn", summary="x")
    assert alert_id is not None
    alerts = ws_db.list_alerts(dismissed=False)
    assert any(a["kind"] == "unmet_prerequisite" for a in alerts)


def test_alerts_migration_preserves_existing_rows_from_old_schema(ws_db):
    """Simulates a real pre-task-#55 database: an alerts table with the OLD,
    narrower CHECK constraint and a real row already in it. Calling
    init_workgraph() again must migrate the constraint AND keep that row."""
    conn = ws_db._connect()
    conn.execute("DROP TABLE alerts")
    conn.execute("""
        CREATE TABLE alerts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id     TEXT,
            kind         TEXT NOT NULL CHECK (kind IN ('stale','high_priority_ask','anomaly','stuck_action')),
            severity     TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
            summary      TEXT NOT NULL,
            source_ref   TEXT,
            created_ts   REAL NOT NULL,
            dismissed    INTEGER NOT NULL DEFAULT 0,
            dismissed_ts REAL
        )
    """)
    conn.execute(
        "INSERT INTO alerts (issue_id, kind, severity, summary, created_ts) VALUES (NULL, 'stale', 'warn', 'pre-existing alert', 1.0)"
    )

    ws_db.init_workgraph()  # re-run the migration

    alerts = ws_db.list_alerts(dismissed=False)
    assert any(a["summary"] == "pre-existing alert" for a in alerts)
    new_id = ws_db.create_alert(issue_id=None, kind="unmet_prerequisite", severity="warn", summary="new one")
    assert new_id is not None


def test_alerts_migration_is_crash_safe(ws_db, monkeypatch):
    """Fixed (adversarial review, task #61): the migration used to run as 4
    independent autocommit statements - a crash between RENAME and DROP
    permanently orphaned the real data under alerts_pre_task55 behind a
    fresh, EMPTY alerts table. Wrapped in an explicit transaction, SQLite's
    DDL is fully atomic: this simulates a genuine crash (an exception that
    isn't the caught sqlite3.OperationalError) firing right after the RENAME
    step, and proves the ORIGINAL table+row survives untouched - nothing was
    ever committed, so nothing was ever lost."""
    conn = ws_db._connect()
    conn.execute("DROP TABLE alerts")
    conn.execute("""
        CREATE TABLE alerts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id     TEXT,
            kind         TEXT NOT NULL CHECK (kind IN ('stale','high_priority_ask','anomaly','stuck_action')),
            severity     TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
            summary      TEXT NOT NULL,
            source_ref   TEXT,
            created_ts   REAL NOT NULL,
            dismissed    INTEGER NOT NULL DEFAULT 0,
            dismissed_ts REAL
        )
    """)
    conn.execute(
        "INSERT INTO alerts (issue_id, kind, severity, summary, created_ts) VALUES (NULL, 'stale', 'warn', 'must survive a crash', 1.0)"
    )
    conn.close()

    real_connect = sqlite3.connect
    call_count = {"renames": 0}

    class _CrashingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "RENAME TO alerts_pre_task55" in sql:
                call_count["renames"] += 1
                result = super().execute(sql, *args, **kwargs)
                raise RuntimeError("simulated crash right after the rename")
            return super().execute(sql, *args, **kwargs)

    def fake_sqlite_connect(*args, **kwargs):
        kwargs["factory"] = _CrashingConnection
        return real_connect(*args, **kwargs)

    sqlite3.connect = fake_sqlite_connect
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            ws_db.init_workgraph()
    finally:
        # Restore ONLY sqlite3.connect directly - monkeypatch.undo() would
        # also revert the ws_db fixture's OWN WORKGRAPH_DB redirection
        # (same monkeypatch instance), pointing the "verify nothing was
        # lost" check below at a completely different database.
        sqlite3.connect = real_connect
    assert call_count["renames"] == 1  # confirms the crash point was actually hit

    # Reconnect for real and verify nothing was lost: the original table,
    # under its original name, with its original row, exactly as if the
    # migration had never been attempted.
    conn2 = ws_db._connect()
    row = conn2.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='alerts'").fetchone()
    assert "unmet_prerequisite" not in (row["sql"] or "")  # still the OLD schema - migration never committed
    surviving = conn2.execute("SELECT summary FROM alerts WHERE summary = 'must survive a crash'").fetchone()
    assert surviving is not None
    orphan_check = conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts_pre_task55'"
    ).fetchone()
    assert orphan_check is None  # no orphaned table left behind either


def test_list_distinct_signal_types_in_use(ws_db):
    ws_db.insert_raw_item(source="outlook_mail", stable_key="a", thread_key="a", dedupe_key="a",
                          occurred_ts=1.0, subject="s", from_actor="x@example.com", participants_json="[]")
    ws_db.insert_raw_item(source="outlook_mail", stable_key="b", thread_key="b", dedupe_key="b",
                          occurred_ts=2.0, subject="s", from_actor="x@example.com", participants_json="[]")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = 'ariba_pr_fully_approved' WHERE stable_key = 'a'")
    conn.execute("UPDATE raw_items SET signal_type = 'signature_requested_docusign' WHERE stable_key = 'b'")
    types = ws_db.list_distinct_signal_types_in_use()
    assert set(types) == {"ariba_pr_fully_approved", "signature_requested_docusign"}


def test_get_raw_items_by_signal_type(ws_db):
    row_id = ws_db.insert_raw_item(source="outlook_mail", stable_key="a", thread_key="a", dedupe_key="a",
                                    occurred_ts=5.0, subject="s", from_actor="x@example.com", participants_json="[]")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = 'ariba_pr_fully_approved' WHERE id = ?", (row_id,))
    rows = ws_db.get_raw_items_by_signal_type("ariba_pr_fully_approved")
    assert len(rows) == 1
    assert rows[0]["id"] == row_id
    assert rows[0]["occurred_ts"] == 5.0
    assert ws_db.get_raw_items_by_signal_type("nonexistent_type") == []


def test_create_and_list_prerequisite_suggestion(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="detected", trigger_signal_type="a", requires_signal_type="b", match_on="project",
        reason="r", evidence="e", raw_explanation=None, proposed_by="system",
    )
    pending = ws_db.list_prerequisite_suggestions("pending")
    assert len(pending) == 1
    assert pending[0]["id"] == sid
    assert pending[0]["origin"] == "detected"
    assert ws_db.get_prerequisite_suggestion(sid)["reason"] == "r"


def test_list_prerequisite_suggestions_status_none_returns_all(ws_db):
    sid1 = ws_db.create_prerequisite_suggestion(origin="detected", trigger_signal_type="a",
        requires_signal_type="b", match_on="project", reason="r", evidence="e",
        raw_explanation=None, proposed_by="system")
    ws_db.resolve_prerequisite_suggestion(sid1, "rejected")
    sid2 = ws_db.create_prerequisite_suggestion(origin="detected", trigger_signal_type="c",
        requires_signal_type="d", match_on="project", reason="r", evidence="e",
        raw_explanation=None, proposed_by="system")

    assert len(ws_db.list_prerequisite_suggestions("pending")) == 1
    assert len(ws_db.list_prerequisite_suggestions(None)) == 2
    assert ws_db.get_prerequisite_suggestion(sid1)["status"] == "rejected"
    assert ws_db.get_prerequisite_suggestion(sid2)["status"] == "pending"


def test_resolve_prerequisite_suggestion_invalid_status_raises(ws_db):
    sid = ws_db.create_prerequisite_suggestion(origin="detected", trigger_signal_type="a",
        requires_signal_type="b", match_on="project", reason="r", evidence="e",
        raw_explanation=None, proposed_by="system")
    with pytest.raises(ValueError):
        ws_db.resolve_prerequisite_suggestion(sid, "pending")


def test_get_most_recent_pending_suggestion_by_asker(ws_db):
    ws_db.create_prerequisite_suggestion(origin="taught_via_chat", trigger_signal_type=None,
        requires_signal_type=None, match_on=None, reason=None, evidence=None,
        raw_explanation="old one", proposed_by="marc")
    recent_id = ws_db.create_prerequisite_suggestion(origin="taught_via_chat", trigger_signal_type=None,
        requires_signal_type=None, match_on=None, reason=None, evidence=None,
        raw_explanation="new one", proposed_by="marc")

    result = ws_db.get_most_recent_pending_suggestion_by_asker("marc", since_ts=0)
    assert result["id"] == recent_id
    assert result["raw_explanation"] == "new one"


def test_get_most_recent_pending_suggestion_excludes_other_askers(ws_db):
    ws_db.create_prerequisite_suggestion(origin="taught_via_chat", trigger_signal_type=None,
        requires_signal_type=None, match_on=None, reason=None, evidence=None,
        raw_explanation="x", proposed_by="someone_else")
    assert ws_db.get_most_recent_pending_suggestion_by_asker("marc", since_ts=0) is None


def test_get_most_recent_pending_suggestion_excludes_resolved(ws_db):
    sid = ws_db.create_prerequisite_suggestion(origin="taught_via_chat", trigger_signal_type=None,
        requires_signal_type=None, match_on=None, reason=None, evidence=None,
        raw_explanation="x", proposed_by="marc")
    ws_db.resolve_prerequisite_suggestion(sid, "confirmed")
    assert ws_db.get_most_recent_pending_suggestion_by_asker("marc", since_ts=0) is None


def test_get_most_recent_pending_suggestion_respects_since_ts(ws_db):
    ws_db.create_prerequisite_suggestion(origin="taught_via_chat", trigger_signal_type=None,
        requires_signal_type=None, match_on=None, reason=None, evidence=None,
        raw_explanation="x", proposed_by="marc")
    result = ws_db.get_most_recent_pending_suggestion_by_asker("marc", since_ts=time.time() + 100)
    assert result is None


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


def test_claim_daily_run_first_caller_wins(ws_db):
    assert ws_db.claim_daily_run("retention", "2026-07-30") is True


def test_claim_daily_run_second_caller_same_day_loses(ws_db):
    """The exact race this fixes: two overlapping scheduled_refresh.py
    processes both reach the gate for the same day. Only one may proceed -
    a second claim call for the same (source, day) must observe False, not
    silently re-run the (sometimes destructive, e.g. backup-writing) work."""
    assert ws_db.claim_daily_run("retention", "2026-07-30") is True
    assert ws_db.claim_daily_run("retention", "2026-07-30") is False
    assert ws_db.claim_daily_run("retention", "2026-07-30") is False


def test_claim_daily_run_new_day_wins_again(ws_db):
    assert ws_db.claim_daily_run("retention", "2026-07-30") is True
    assert ws_db.claim_daily_run("retention", "2026-07-31") is True


def test_claim_daily_run_is_scoped_per_source(ws_db):
    """Two different daily-gated jobs (e.g. retention and health_check) must
    not block each other - each source claims its own row."""
    assert ws_db.claim_daily_run("retention", "2026-07-30") is True
    assert ws_db.claim_daily_run("health_check", "2026-07-30") is True


def test_claim_daily_run_concurrent_threads_only_one_winner(ws_db):
    """Simulates the real failure mode with actual OS threads racing the
    same claim, each against its own sqlite3 connection (claim_daily_run
    calls _connect() internally) - proves the win is decided by the
    database's own statement atomicity, not by this process's in-memory
    lock alone."""
    import threading

    results = []
    results_lock = threading.Lock()

    def attempt():
        won = ws_db.claim_daily_run("retention", "2026-07-30")
        with results_lock:
            results.append(won)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert results.count(False) == 11


def test_set_project_status_updates_status(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    ws_db.set_project_status(pid, "archived")
    assert ws_db.get_project(pid)["status"] == "archived"


def test_set_project_status_rejects_invalid_status(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    with pytest.raises(ValueError):
        ws_db.set_project_status(pid, "bogus")
    assert ws_db.get_project(pid)["status"] != "bogus"


# --- nba_choice_log (Part E2, grouping/NBA redesign) ----------------------

def test_create_nba_choice_log_defaults_to_offered_status(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    log_id = ws_db.create_nba_choice_log(issue_id=iid, offered_json="[]", scoring_inputs_json="{}")
    log = ws_db.get_most_recent_open_choice_log(iid)
    assert log["id"] == log_id
    assert log["status"] == "offered"
    assert log["chosen_action_kind"] is None


def test_get_most_recent_open_choice_log_none_when_no_rows(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    assert ws_db.get_most_recent_open_choice_log(iid) is None


def test_get_most_recent_open_choice_log_ignores_already_chosen_rows(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    log_id = ws_db.create_nba_choice_log(issue_id=iid, offered_json="[]", scoring_inputs_json="{}")
    ws_db.mark_choice_log_chosen(log_id, chosen_action_kind="draft_reply")
    assert ws_db.get_most_recent_open_choice_log(iid) is None


def test_get_most_recent_open_choice_log_returns_the_latest_when_several(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    first = ws_db.create_nba_choice_log(issue_id=iid, offered_json="[]", scoring_inputs_json="{}")
    ws_db.mark_choice_log_chosen(first, chosen_action_kind="snooze")
    second = ws_db.create_nba_choice_log(issue_id=iid, offered_json="[]", scoring_inputs_json="{}")
    log = ws_db.get_most_recent_open_choice_log(iid)
    assert log["id"] == second


def test_mark_choice_log_chosen_sets_all_real_fields(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    log_id = ws_db.create_nba_choice_log(issue_id=iid, offered_json='[{"kind":"draft_reply"}]', scoring_inputs_json="{}")
    ws_db.mark_choice_log_chosen(log_id, chosen_action_kind="draft_reply",
                                  resulting_pending_action_id=42, chosen_note="matched top candidate")
    conn = ws_db._connect()
    row = dict(conn.execute("SELECT * FROM nba_choice_log WHERE id = ?", (log_id,)).fetchone())
    conn.close()
    assert row["status"] == "chosen"
    assert row["chosen_action_kind"] == "draft_reply"
    assert row["resulting_pending_action_id"] == 42
    assert row["chosen_note"] == "matched top candidate"
    assert row["chosen_ts"] is not None


def test_log_shadow_grouping_decision_persists_all_fields(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    log_id = ws_db.log_shadow_grouping_decision(
        issue_id=iid, live_action="suggested", live_signal="company", live_sibling_id="marc-002",
        scored_verdict="auto_merge", scored_score=0.7, scored_sibling_id="marc-002",
        scored_signals_json='["company","topic"]',
    )
    rows = ws_db.list_shadow_grouping_log()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == log_id
    assert row["issue_id"] == iid
    assert row["live_action"] == "suggested"
    assert row["live_signal"] == "company"
    assert row["scored_verdict"] == "auto_merge"
    assert row["scored_score"] == 0.7
    assert row["scored_signals_json"] == '["company","topic"]'


def test_list_shadow_grouping_log_disagreements_only_filters_to_mismatches(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    # live agrees with scored (both auto_merge) - not a disagreement
    ws_db.log_shadow_grouping_decision(
        issue_id=iid, live_action="auto_merged", live_signal="reference", live_sibling_id="marc-001",
        scored_verdict="auto_merge", scored_score=1.0, scored_sibling_id="marc-001", scored_signals_json="[]",
    )
    # live only suggests, but scored would have auto-merged - a real disagreement
    ws_db.log_shadow_grouping_decision(
        issue_id=iid, live_action="suggested", live_signal="company", live_sibling_id="marc-002",
        scored_verdict="auto_merge", scored_score=0.7, scored_sibling_id="marc-002", scored_signals_json="[]",
    )

    all_rows = ws_db.list_shadow_grouping_log()
    disagreements = ws_db.list_shadow_grouping_log(disagreements_only=True)
    assert len(all_rows) == 2
    assert len(disagreements) == 1
    assert disagreements[0]["live_action"] == "suggested"
