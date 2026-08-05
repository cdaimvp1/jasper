"""Regression tests for workgraph_store.py:
- ORDER BY tie-break on list_issues (task #30 enhancement)
- create_issue_with_new_id / create_project_with_new_id / create_task retry
"""
import json
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


def test_create_cluster_is_invisible_through_issues_view(ws_db):
    """Corrected-ordering redesign (2026-08-05): a cluster is a real
    work_objects row (object_type='request') but must never appear through
    get_issue/list_issues - it's not a real, individually-tracked issue
    until something promotes it. This is the whole point of is_raw_cluster."""
    cid = ws_db.create_cluster_with_new_id(title="Authenticx Pricing Discussion", category="other")
    assert ws_db.get_issue(cid) is None
    assert cid not in [i["id"] for i in ws_db.list_issues(states=None, limit=1000)]


def test_get_cluster_returns_issue_shaped_dict(ws_db):
    """get_cluster must return the same column shape get_issue does (state/
    project_id aliases included) so pass-1/pass-2 matching code can treat a
    cluster and a real issue identically without a special case."""
    cid = ws_db.create_cluster_with_new_id(title="Authenticx Pricing Discussion", category="other")
    cluster = ws_db.get_cluster(cid)
    assert cluster is not None
    assert cluster["title"] == "Authenticx Pricing Discussion"
    assert cluster["state"] == "active"
    assert cluster["project_id"] is None


def test_get_cluster_returns_none_for_a_real_issue(ws_db):
    """The inverse guarantee - get_cluster must not accidentally surface a
    real issue as if it were a cluster."""
    iid = ws_db.create_issue_with_new_id(title="Real issue", state="active", category="other")
    assert ws_db.get_cluster(iid) is None


def test_list_clusters_only_returns_clusters(ws_db):
    cid = ws_db.create_cluster_with_new_id(title="Cluster A", category="other")
    iid = ws_db.create_issue_with_new_id(title="Real issue", state="active", category="other")
    clusters = ws_db.list_clusters()
    ids = [c["id"] for c in clusters]
    assert cid in ids
    assert iid not in ids


def test_create_issue_with_new_id_never_collides_with_cluster_ids(ws_db):
    """Clusters and issues share the same id namespace (all work_objects) -
    the allocation race guard must hold across both creation paths."""
    a = ws_db.create_cluster_with_new_id(title="A", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    assert a != b


def test_get_issue_or_cluster_reads_either_kind(ws_db):
    iid = ws_db.create_issue_with_new_id(title="Real issue", state="active", category="other")
    cid = ws_db.create_cluster_with_new_id(title="Cluster", category="other")
    assert ws_db.get_issue_or_cluster(iid)["title"] == "Real issue"
    assert ws_db.get_issue_or_cluster(cid)["title"] == "Cluster"


def test_get_issue_or_cluster_none_for_a_project(ws_db):
    """A project id is not object_type='request' at all - must not leak
    through this reader, which is scoped to issue/cluster kinds only."""
    pid = ws_db.create_project_with_new_id(name="A project", category="other")
    assert ws_db.get_issue_or_cluster(pid) is None


def test_list_open_work_objects_for_reference_finds_clusters_and_issues(ws_db):
    """The whole point of this function - cluster_and_link's exact-
    reference auto-attach needs to find a matching CLUSTER (not yet
    promoted), not just an already-promoted issue."""
    cid = ws_db.create_cluster_with_new_id(title="Cluster with a PR", category="other")
    ws_db.insert_raw_item(source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="dk1",
                           occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                           participants_json="[]")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET issue_id = ?, pr_number_base = 'PR900001' WHERE stable_key = 'k1'", (cid,))
    conn.commit()
    conn.close()

    matches = ws_db.list_open_work_objects_for_reference("PR900001")
    assert cid in matches


def test_list_open_work_objects_for_reference_excludes_closed(ws_db):
    cid = ws_db.create_cluster_with_new_id(title="Closed cluster", category="other")
    conn = ws_db._connect()
    conn.execute("UPDATE work_objects SET status = 'done' WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    ws_db.insert_raw_item(source="outlook_mail", stable_key="k2", thread_key="k2", dedupe_key="dk2",
                           occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                           participants_json="[]")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET issue_id = ?, pr_number_base = 'PR900002' WHERE stable_key = 'k2'", (cid,))
    conn.commit()
    conn.close()

    assert ws_db.list_open_work_objects_for_reference("PR900002") == []


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


# --- reply-latency / ping-pong count (enhancement idea panel #1) ----------

def _raw_item_with_direction(ws_db, issue_id, key, occurred_ts, direction):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=occurred_ts, subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET direction = ? WHERE id = ?", (direction, rid))
    conn.commit()
    conn.close()
    return rid


def test_reply_latency_counts_real_alternations_only(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    _raw_item_with_direction(ws_db, a, "r1", 100.0, "inbound")
    _raw_item_with_direction(ws_db, a, "r2", 200.0, "outbound")   # alternation (+100)
    _raw_item_with_direction(ws_db, a, "r3", 250.0, "outbound")   # same side, not an alternation
    _raw_item_with_direction(ws_db, a, "r4", 400.0, "inbound")    # alternation (+150)

    result = ws_db.compute_reply_latency_for_issue(a)

    assert result["ping_pong_count"] == 2
    assert result["avg_reply_latency_seconds"] == 125.0  # (100 + 150) / 2


def test_reply_latency_excludes_internal_and_unknown_direction(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    _raw_item_with_direction(ws_db, a, "r1", 100.0, "inbound")
    _raw_item_with_direction(ws_db, a, "r2", 150.0, "internal")   # excluded - neither real side
    _raw_item_with_direction(ws_db, a, "r3", 200.0, "outbound")   # still a real alternation vs r1

    result = ws_db.compute_reply_latency_for_issue(a)

    assert result["ping_pong_count"] == 1
    assert result["avg_reply_latency_seconds"] == 100.0


def test_reply_latency_none_for_one_sided_thread(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    _raw_item_with_direction(ws_db, a, "r1", 100.0, "outbound")
    _raw_item_with_direction(ws_db, a, "r2", 200.0, "outbound")

    result = ws_db.compute_reply_latency_for_issue(a)

    assert result["ping_pong_count"] == 0
    assert result["avg_reply_latency_seconds"] is None


def test_reply_latency_zero_for_issue_with_no_raw_items(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")

    result = ws_db.compute_reply_latency_for_issue(a)

    assert result == {"ping_pong_count": 0, "avg_reply_latency_seconds": None}


# --- active signal-treatment overrides (enhancement idea panel #4) --------

def _raw_item_with_signal_type(ws_db, issue_id, key, signal_type):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", (signal_type, rid))
    conn.commit()
    conn.close()
    return rid


def test_find_active_signal_overrides_returns_the_real_override(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    _raw_item_with_signal_type(ws_db, a, "s1", "ariba_notification")
    ws_db.set_signal_treatment("ariba_notification", "actionable", reason="always needs a reply", set_by="marc")

    overrides = ws_db.find_active_signal_overrides_for_issue(a)

    assert len(overrides) == 1
    assert overrides[0]["signal_type"] == "ariba_notification"
    assert overrides[0]["treatment"] == "actionable"
    assert overrides[0]["reason"] == "always needs a reply"


def test_find_active_signal_overrides_empty_when_no_override_set(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    _raw_item_with_signal_type(ws_db, a, "s2", "docusign_notification")

    assert ws_db.find_active_signal_overrides_for_issue(a) == []


def test_find_active_signal_overrides_empty_when_no_signal_type(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")

    assert ws_db.find_active_signal_overrides_for_issue(a) == []


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
    conn.execute("UPDATE raw_items SET pr_number = ?, pr_number_base = ? WHERE id = ?", ("PR999999", "PR999999", rid))
    conn.close()

    assert ws_db.list_open_issue_ids_for_reference("PR999999") == [iid]


def test_list_open_issue_ids_for_reference_excludes_closed_issues(ws_db):
    iid = ws_db.create_issue_with_new_id(title="Closed", state="done", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="pr2", thread_key="pr2", dedupe_key="pr2",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number = ?, pr_number_base = ? WHERE id = ?", ("PR888888", "PR888888", rid))
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
        conn.execute("UPDATE raw_items SET pr_number = ?, pr_number_base = ? WHERE id = ?", ("PR777000", "PR777000", rid))
        conn.close()
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (100.0, older))
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (200.0, newer))
    conn.close()

    assert ws_db.list_open_issue_ids_for_reference("PR777000") == [newer, older]


def test_list_open_issue_ids_for_reference_matches_on_base_not_full_string(ws_db):
    """The real bug this fixes: querying by base "PR1140347" must find an
    issue whose raw_item's full pr_number is a DIFFERENT version
    ("PR1140347-V3") of the same real requisition."""
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="pr4", thread_key="pr4", dedupe_key="pr4",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number = ?, pr_number_base = ? WHERE id = ?",
                 ("PR1140347-V3", "PR1140347", rid))
    conn.close()

    assert ws_db.list_open_issue_ids_for_reference("PR1140347") == [iid]


def test_insert_raw_item_persists_thread_key_source_and_is_organizer(ws_db):
    rid = ws_db.insert_raw_item(
        source="calendar", stable_key="evt-1", thread_key="synth:x|y|19:00", dedupe_key="dk1",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
        thread_key_source="synthetic_calendar_series", is_organizer=1,
    )
    row = ws_db.get_raw_item(rid)
    assert row["thread_key_source"] == "synthetic_calendar_series"
    assert row["is_organizer"] == 1


def test_insert_raw_item_defaults_thread_key_source_and_is_organizer_to_none(ws_db):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="m-1", thread_key="m-1", dedupe_key="dk2",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    row = ws_db.get_raw_item(rid)
    assert row["thread_key_source"] is None
    assert row["is_organizer"] is None


def test_classify_raw_item_persists_pr_number_base(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="pr5", thread_key="pr5", dedupe_key="pr5",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)
    ws_db.classify_raw_item(
        rid, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
        topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
        anomaly_flag=False, pr_number="PR416079-V33", pr_number_base="PR416079",
    )
    row = ws_db.get_raw_items_for_issue(iid)[0]
    assert row["pr_number"] == "PR416079-V33"
    assert row["pr_number_base"] == "PR416079"


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


# --- issue_state_history actor tracking (2026-08-01, real-incident follow-up) ---

def test_update_issue_state_change_records_actor(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.update_issue(iid, state="done", actor="marc")

    history = ws_db.list_issue_state_history(iid)
    last = history[-1]
    assert last["from_state"] == "active"
    assert last["to_state"] == "done"
    assert last["actor"] == "marc"


def test_update_issue_no_actor_records_null(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.update_issue(iid, state="waiting")  # no actor passed - honest unknown, not a guess

    last = ws_db.list_issue_state_history(iid)[-1]
    assert last["to_state"] == "waiting"
    assert last["actor"] is None


def test_update_issue_non_state_field_does_not_add_history_row(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    before = len(ws_db.list_issue_state_history(iid))
    ws_db.update_issue(iid, priority="high", actor="marc")

    assert len(ws_db.list_issue_state_history(iid)) == before


# --- get_items_pending_link ordering (2026-08-01, real-incident follow-up) ---

def test_get_items_pending_link_never_checked_sorts_before_already_skipped(ws_db):
    """The exact bug: plain oldest-first let an ever-growing pool of already-
    examined-and-skipped rows crowd out rows that have never been looked at,
    no matter how much older the skipped ones are."""
    old_skipped = ws_db.insert_raw_item(source="outlook_mail", stable_key="p1", thread_key="p1", dedupe_key="p1",
                                         occurred_ts=1.0, subject="old", from_actor="a@example.com", participants_json="[]")
    ws_db.classify_raw_item(old_skipped, item_class="NOISE", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True, anomaly_flag=False)
    ws_db.mark_link_checked(old_skipped, 500.0)

    new_unchecked = ws_db.insert_raw_item(source="outlook_mail", stable_key="p2", thread_key="p2", dedupe_key="p2",
                                           occurred_ts=1000.0, subject="new", from_actor="a@example.com", participants_json="[]")
    ws_db.classify_raw_item(new_unchecked, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True, anomaly_flag=False)

    pending = ws_db.get_items_pending_link(limit=1)

    assert [p["id"] for p in pending] == [new_unchecked]


def test_get_items_pending_link_orders_oldest_first_within_each_group(ws_db):
    """Ordering within the never-checked group (and within the already-
    checked group) is still oldest-occurred_ts-first, unchanged from before
    this fix - only the CROSS-group priority changed."""
    older = ws_db.insert_raw_item(source="outlook_mail", stable_key="p3", thread_key="p3", dedupe_key="p3",
                                   occurred_ts=1.0, subject="older", from_actor="a@example.com", participants_json="[]")
    ws_db.classify_raw_item(older, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True, anomaly_flag=False)
    newer = ws_db.insert_raw_item(source="outlook_mail", stable_key="p4", thread_key="p4", dedupe_key="p4",
                                   occurred_ts=2.0, subject="newer", from_actor="a@example.com", participants_json="[]")
    ws_db.classify_raw_item(newer, item_class="ACTIONABLE-ASK", direction="inbound", direction_inferred=False,
                             topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True, anomaly_flag=False)

    pending = ws_db.get_items_pending_link(limit=10)

    assert [p["id"] for p in pending] == [older, newer]


def test_mark_link_checked_only_affects_the_named_row(ws_db):
    a = ws_db.insert_raw_item(source="outlook_mail", stable_key="p5", thread_key="p5", dedupe_key="p5",
                               occurred_ts=1.0, subject="a", from_actor="x@example.com", participants_json="[]")
    b = ws_db.insert_raw_item(source="outlook_mail", stable_key="p6", thread_key="p6", dedupe_key="p6",
                               occurred_ts=2.0, subject="b", from_actor="x@example.com", participants_json="[]")
    ws_db.mark_link_checked(a, 100.0)

    assert ws_db.get_raw_item(a)["last_link_check_ts"] == 100.0
    assert ws_db.get_raw_item(b)["last_link_check_ts"] is None


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


# --- task #44: real 'dismissed' state + checklist-item dismissal ----------

def test_issues_table_accepts_dismissed_state(ws_db):
    """Confirms the CHECK constraint migration in init_workgraph() actually
    took effect for issues.state, same shape as the alerts.kind migration."""
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    ws_db.update_issue(issue_id, state="dismissed")
    assert ws_db.get_issue(issue_id)["state"] == "dismissed"


def test_issues_migration_preserves_existing_rows_from_old_schema(ws_db):
    """Simulates a real pre-task-#44 database: an issues table with the OLD,
    narrower CHECK constraint and a real row already in it. Calling
    init_workgraph() again must migrate the constraint AND keep that row.

    Deliberately includes project_id/lesson_id_cited/has_unmet_prerequisite -
    three columns added to the REAL table by later ALTER TABLEs, well after
    the base CREATE TABLE this migration rebuilds from. A real bug caught
    live against production (not by this test, the first time it was
    written without these three columns - fixed after): the rebuilt CREATE
    TABLE in the migration omitted them, so the INSERT...SELECT failed with
    "no such column", silently caught and rolled back every single time,
    meaning the constraint never actually widened in production despite the
    migration looking correct and every other test passing.

    2026-08-03 (design doc Section 12.1): the ws_db fixture's own
    init_workgraph() call already migrated issues/projects into
    work_objects, so `issues`/`projects` are VIEWS by the time this test
    body runs - simulating a genuinely pre-migration database means
    tearing all of that back down first (triggers, views, and
    work_objects itself), not just the issues table."""
    conn = ws_db._connect()
    conn.execute("DROP TRIGGER IF EXISTS trg_issues_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_issues_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_issues_delete")
    conn.execute("DROP TRIGGER IF EXISTS trg_projects_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_projects_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_projects_delete")
    conn.execute("DROP VIEW IF EXISTS issues")
    conn.execute("DROP VIEW IF EXISTS projects")
    conn.execute("DROP TABLE IF EXISTS work_objects")
    # The ws_db fixture's own init_workgraph() call already ran this
    # migration once, leaving these backup tables behind (by design, never
    # dropped in real production - see workgraph_store.py's own comment).
    # Simulating a genuinely pre-migration database means clearing them
    # too, or the real migration's rename step collides with its own prior
    # run's leftovers - a real, if narrow, case worth naming rather than
    # silently working around: this is not something this specific test
    # needs to also cover, so it clears it rather than asserting on it.
    conn.execute("DROP TABLE IF EXISTS issues_pre_workobjects")
    conn.execute("DROP TABLE IF EXISTS projects_pre_workobjects")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            category   TEXT,
            status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','waiting','done','archived','dismissed')),
            opened_at  REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE issues (
            id               TEXT PRIMARY KEY,
            title            TEXT NOT NULL,
            category         TEXT,
            state            TEXT NOT NULL CHECK (state IN ('active','waiting','blocked','done','noise-archived')),
            priority         TEXT NOT NULL DEFAULT 'med' CHECK (priority IN ('high','med','low')),
            priority_score   REAL,
            nba_action_kind  TEXT CHECK (nba_action_kind IN ('draft','review','approve','chase','wait','read','none')),
            nba_reason       TEXT,
            owner            TEXT NOT NULL DEFAULT 'marc',
            due              TEXT,
            opened_at        REAL NOT NULL,
            updated_at       REAL NOT NULL,
            confidence_tier  TEXT CHECK (confidence_tier IN ('H','M','L')),
            project_id       TEXT REFERENCES projects(id),
            lesson_id_cited  INTEGER REFERENCES lessons(id),
            has_unmet_prerequisite INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "INSERT INTO issues (id, title, category, state, priority, owner, opened_at, updated_at, has_unmet_prerequisite) "
        "VALUES ('pre-existing-1', 'pre-existing issue', 'other', 'active', 'med', 'marc', 1.0, 1.0, 0)"
    )
    conn.close()

    ws_db.init_workgraph()  # re-run the migration

    assert ws_db.get_issue("pre-existing-1")["title"] == "pre-existing issue"
    ws_db.update_issue("pre-existing-1", state="dismissed")
    assert ws_db.get_issue("pre-existing-1")["state"] == "dismissed"


def test_checklist_item_key_is_deterministic_and_kind_sensitive(ws_db):
    k1 = ws_db.checklist_item_key("ask", 42, "Please approve the PO")
    k2 = ws_db.checklist_item_key("ask", 42, "please approve the po")  # case/whitespace-insensitive
    k3 = ws_db.checklist_item_key("decision", 42, "Please approve the PO")  # different kind
    k4 = ws_db.checklist_item_key("ask", 43, "Please approve the PO")  # different raw_item_id
    assert k1 == k2
    assert k1 != k3
    assert k1 != k4


def test_dismiss_checklist_item_round_trip(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    item_key = ws_db.dismiss_checklist_item(
        issue_id=issue_id, kind="ask", raw_item_id=7, text="Approve the requisition", actor="marc",
    )
    assert item_key == ws_db.checklist_item_key("ask", 7, "Approve the requisition")
    dismissed = ws_db.list_dismissed_checklist_keys(issue_id)
    assert item_key in dismissed
    # a different issue's set must not see this issue's dismissal
    other_issue_id = ws_db.create_issue_with_new_id(title="Y", state="active", category="other")
    assert ws_db.list_dismissed_checklist_keys(other_issue_id) == set()


def test_dismiss_checklist_item_is_idempotent(ws_db):
    """Re-dismissing the same item_key must not raise (INSERT OR REPLACE),
    and must not create a second row for the same (issue_id, item_key)."""
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    ws_db.dismiss_checklist_item(issue_id=issue_id, kind="ask", raw_item_id=7, text="Same ask", actor="marc")
    ws_db.dismiss_checklist_item(issue_id=issue_id, kind="ask", raw_item_id=7, text="Same ask", actor="marc")
    conn = ws_db._connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM checklist_dismissals WHERE issue_id = ?", (issue_id,)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert count == 1


def test_mark_checklist_item_done_round_trip(ws_db):
    """Task #59: same mechanics as dismiss, distinct recorded status."""
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    item_key = ws_db.mark_checklist_item_done(
        issue_id=issue_id, kind="ask", raw_item_id=9, text="Send the signed order form", actor="marc",
    )
    assert item_key == ws_db.checklist_item_key("ask", 9, "Send the signed order form")
    # list_dismissed_checklist_keys covers ANY resolved status (dismissed or
    # done) - a done item must stop reappearing exactly like a dismissed one.
    resolved = ws_db.list_dismissed_checklist_keys(issue_id)
    assert item_key in resolved
    conn = ws_db._connect()
    try:
        row = conn.execute(
            "SELECT status FROM checklist_dismissals WHERE issue_id = ? AND item_key = ?",
            (issue_id, item_key),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "done"


def test_dismiss_and_done_are_recorded_as_distinct_outcomes(ws_db):
    """The whole point of task #44/#59: dismissing one item and marking a
    different item done on the same issue must not collide or overwrite -
    they're different item_keys, and each keeps its own recorded status."""
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    dismissed_key = ws_db.dismiss_checklist_item(issue_id=issue_id, kind="ask", raw_item_id=1, text="Ask A")
    done_key = ws_db.mark_checklist_item_done(issue_id=issue_id, kind="ask", raw_item_id=2, text="Ask B")
    assert dismissed_key != done_key
    conn = ws_db._connect()
    try:
        rows = {r["item_key"]: r["status"] for r in conn.execute(
            "SELECT item_key, status FROM checklist_dismissals WHERE issue_id = ?", (issue_id,)
        ).fetchall()}
    finally:
        conn.close()
    assert rows[dismissed_key] == "dismissed"
    assert rows[done_key] == "done"


# --- merge_issues_txn (meeting-grouping/related-project identity pass) ----

def test_merge_issues_txn_creates_new_project_when_neither_has_one(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    result = ws_db.merge_issues_txn(a, b, reason_label="test", new_project_name="New", new_project_category="other")
    assert result["status"] == "merged"
    project_id = result["project_id"]
    assert ws_db.get_issue(a)["project_id"] == project_id
    assert ws_db.get_issue(b)["project_id"] == project_id
    assert ws_db.get_project(project_id)["name"] == "New"


def test_merge_issues_txn_singleton_loser_still_auto_merges(ws_db):
    """2026-07-31 (step 5): a loser project whose ONLY member is the issue
    being merged itself (no other real members) is low-risk - nothing else
    gets uprooted - and still auto-merges, unlike an established (2+
    member) loser below."""
    proj_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.assign_issue_to_project(a, proj_a)

    proj_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.assign_issue_to_project(b, proj_b)

    result = ws_db.merge_issues_txn(a, b, reason_label="test singleton", new_project_name="unused", new_project_category="other")

    assert result == {"status": "merged", "project_id": proj_a}
    assert ws_db.get_issue(b)["project_id"] == proj_a
    assert ws_db.get_project(proj_b)["status"] == "archived"


def test_merge_issues_txn_established_loser_defers_to_reconciliation(ws_db):
    """2026-07-31 (step 5, mandatory reconciliation): a loser project with
    ANY real member beyond the issue being merged is an established
    project with its own history - refuses to auto-collapse it, creates a
    'merge_projects' pending suggestion instead of merging."""
    proj_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.assign_issue_to_project(a, proj_a)

    proj_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.assign_issue_to_project(b, proj_b)
    other = ws_db.create_issue_with_new_id(title="Other member of B", state="active", category="other")
    ws_db.assign_issue_to_project(other, proj_b)

    result = ws_db.merge_issues_txn(a, b, reason_label="test collision", new_project_name="unused", new_project_category="other")

    assert result["status"] == "deferred"
    assert result["winner_project_id"] == proj_a
    assert result["loser_project_id"] == proj_b
    # NOTHING actually merged - other/b/proj_b all untouched.
    assert ws_db.get_issue(other)["project_id"] == proj_b
    assert ws_db.get_issue(b)["project_id"] == proj_b
    assert ws_db.get_project(proj_b)["status"] != "archived"
    sugg = ws_db.get_project_suggestion(result["suggestion_id"])
    assert sugg["suggestion_kind"] == "merge_projects"
    assert sugg["status"] == "pending"


def test_would_collide_established_projects_none_when_no_collision(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    assert ws_db.would_collide_established_projects(a, b) is None


def test_would_collide_established_projects_none_for_singleton_loser(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.assign_issue_to_project(a, proj_a)
    proj_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.assign_issue_to_project(b, proj_b)
    assert ws_db.would_collide_established_projects(a, b) is None


def test_would_collide_established_projects_fires_for_established_loser(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.assign_issue_to_project(a, proj_a)
    proj_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.assign_issue_to_project(b, proj_b)
    other = ws_db.create_issue_with_new_id(title="Other", state="active", category="other")
    ws_db.assign_issue_to_project(other, proj_b)

    result = ws_db.would_collide_established_projects(a, b)

    assert result == {"winner_project_id": proj_a, "loser_project_id": proj_b, "loser_members": [other]}


def test_force_merge_projects_moves_members_and_archives_loser(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    proj_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.assign_issue_to_project(b, proj_b)
    other = ws_db.create_issue_with_new_id(title="Other", state="active", category="other")
    ws_db.assign_issue_to_project(other, proj_b)

    winner = ws_db.force_merge_projects(proj_a, proj_b, reason_label="confirmed reconciliation")

    assert winner == proj_a
    assert ws_db.get_issue(b)["project_id"] == proj_a
    assert ws_db.get_issue(other)["project_id"] == proj_a
    assert ws_db.get_project(proj_b)["status"] == "archived"


def test_merge_issues_txn_is_crash_safe(ws_db):
    """The real bug this fixes: merge_issues() used to run as several
    independent autocommit connections - a crash partway through left the
    DB partially merged (e.g. one issue reassigned to the winner but not
    the other) with no recovery path. Uses a singleton-loser pair (2026-
    07-31: an established, 2+-member loser now defers to reconciliation
    BEFORE ever opening a transaction - see would_collide_established_
    projects - so it can no longer exercise this specific crash point;
    the singleton case still reaches the real merge transaction). Crashes
    right after b is reassigned to the winner, and proves NOTHING was
    committed - not even that one UPDATE - because the whole thing is one
    transaction."""
    proj_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.assign_issue_to_project(a, proj_a)

    proj_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.assign_issue_to_project(b, proj_b)

    real_connect = sqlite3.connect
    call_count = {"reassigns": 0}

    class _CrashingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("UPDATE issues SET project_id") and args and args[0] and args[0][0] == proj_a and args[0][2] == b:
                call_count["reassigns"] += 1
                result = super().execute(sql, *args, **kwargs)
                raise RuntimeError("simulated crash right after reassigning b to the winner")
            return super().execute(sql, *args, **kwargs)

    def fake_sqlite_connect(*args, **kwargs):
        kwargs["factory"] = _CrashingConnection
        return real_connect(*args, **kwargs)

    sqlite3.connect = fake_sqlite_connect
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            ws_db.merge_issues_txn(a, b, reason_label="test crash", new_project_name="unused", new_project_category="other")
    finally:
        sqlite3.connect = real_connect
    assert call_count["reassigns"] == 1  # confirms the crash point was actually hit

    # Nothing committed: proj_b still active, b/a untouched - exactly as if
    # the merge had never been attempted.
    assert ws_db.get_project(proj_b)["status"] != "archived"
    assert ws_db.get_issue(b)["project_id"] == proj_b
    assert ws_db.get_issue(a)["project_id"] == proj_a


def test_merge_issues_txn_retries_on_lock_then_succeeds(ws_db, monkeypatch):
    """Bounded retry on BEGIN IMMEDIATE - _connect() sets no busy_timeout, so
    a concurrent writer holding the lock raises immediately rather than
    waiting. Simulates that happening twice, then succeeding on the third
    attempt, and confirms the retry sleep is bounded (not a real sleep in
    tests) and the merge still completes correctly."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")

    real_connect = sqlite3.connect
    call_count = {"begins": 0}

    class _LockedThenOkConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "BEGIN IMMEDIATE":
                call_count["begins"] += 1
                if call_count["begins"] < 3:
                    raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args, **kwargs)

    def fake_sqlite_connect(*args, **kwargs):
        kwargs["factory"] = _LockedThenOkConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("time.sleep", lambda *a: None)
    sqlite3.connect = fake_sqlite_connect
    try:
        result = ws_db.merge_issues_txn(a, b, reason_label="test retry", new_project_name="New", new_project_category="other")
    finally:
        sqlite3.connect = real_connect
    assert call_count["begins"] == 3
    project_id = result["project_id"]
    assert ws_db.get_issue(a)["project_id"] == project_id
    assert ws_db.get_issue(b)["project_id"] == project_id


# --- merge_issue_into (2026-08-03, real issue-level merge) ---------------

def test_merge_issue_into_moves_raw_items_and_evidence(ws_db):
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, loser)
    ws_db.add_evidence(issue_id=loser, type="email", summary="original evidence")

    result = ws_db.merge_issue_into(loser, winner, reason="test merge", actor="marc")

    assert result["status"] == "merged"
    assert result["raw_items_moved"] == 1
    assert result["evidence_moved"] == 1
    conn = ws_db._connect()
    assert conn.execute("SELECT issue_id FROM raw_items WHERE id = ?", (rid,)).fetchone()[0] == winner
    conn.close()


def test_merge_issue_into_dismisses_loser_and_leaves_it_intact(ws_db):
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")

    ws_db.merge_issue_into(loser, winner, reason="test merge", actor="marc")

    loser_after = ws_db.get_issue(loser)
    assert loser_after is not None  # never deleted
    assert loser_after["state"] == "dismissed"
    history = ws_db.list_issue_state_history(loser)
    assert history[-1]["to_state"] == "dismissed"
    assert history[-1]["actor"] == "marc"


def test_merge_issue_into_adds_visible_evidence_note_on_both_sides(ws_db):
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")

    ws_db.merge_issue_into(loser, winner, reason="same PR854779", actor="marc")

    winner_evidence = ws_db.list_evidence(winner)
    loser_evidence = ws_db.list_evidence(loser)
    assert any("same PR854779" in e["summary"] and loser in e["summary"] for e in winner_evidence)
    assert any("same PR854779" in e["summary"] and winner in e["summary"] for e in loser_evidence)


def test_merge_issue_into_moves_parties_without_duplicate_key_error(ws_db):
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    ws_db.upsert_party(id="shared", primary_email="rep@acme.com", display_name="Rep",
                        affiliation="external", affiliation_confidence="H", affiliation_source="domain", company="Acme")
    ws_db.link_party_to_issue(loser, "shared")
    ws_db.link_party_to_issue(winner, "shared")  # already on both - must not raise on the move

    result = ws_db.merge_issue_into(loser, winner, reason="test", actor="marc")

    assert result["status"] == "merged"
    parties = ws_db.list_parties_for_issue(winner)
    assert [p["id"] for p in parties] == ["shared"]


def test_merge_issue_into_moves_exclusive_anchor_when_winner_has_none(ws_db):
    """idx_identity_anchor_exclusive already guarantees at most one active
    exclusive anchor per (type, value) can ever exist DB-wide - so the
    realistic shape (confirmed against the live backfill's own 17 real
    conflicts: create_identity_anchor rejects the second issue's copy at
    creation time, so only one side ever holds an active row) is a clean
    move, not a collision to resolve at merge time."""
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    ws_db.create_identity_anchor(anchor_type="reference", normalized_value="PR1", anchor_strength="strong",
                                  exclusive=True, issue_id=loser)

    result = ws_db.merge_issue_into(loser, winner, reason="test", actor="marc")

    assert result["status"] == "merged"
    winner_anchors = ws_db.list_identity_anchors(issue_id=winner)
    assert len(winner_anchors) == 1
    assert winner_anchors[0]["normalized_value"] == "PR1"
    assert ws_db.list_identity_anchors(issue_id=loser) == []


    # Note: the merge's own conflict-avoidance branch (superseding a
    # loser-side exclusive anchor instead of moving it) is defensive code
    # for a DB state idx_identity_anchor_exclusive makes impossible to
    # construct through any INSERT, direct or otherwise - confirmed by
    # this same test file's earlier attempt to build that state, which the
    # UNIQUE index itself rejected. Not independently testable, and that's
    # the point: the schema already guarantees the danger can't occur.


def test_merge_issue_into_moves_non_conflicting_anchor_normally(ws_db):
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    ws_db.create_identity_anchor(anchor_type="party", normalized_value="p1", anchor_strength="weak",
                                  exclusive=False, issue_id=loser)

    ws_db.merge_issue_into(loser, winner, reason="test", actor="marc")

    assert len(ws_db.list_identity_anchors(issue_id=winner)) == 1
    assert ws_db.list_identity_anchors(issue_id=loser) == []


def test_merge_issue_into_moves_containers_and_checklist_dismissals(ws_db):
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    ws_db.upsert_source_container(id="sc1", source="outlook_mail", container_type="email_conversation",
                                   exact_key="conv1", key_quality="exact", issue_id=loser)
    ws_db.dismiss_checklist_item(issue_id=loser, kind="ask", raw_item_id=None, text="do the thing", actor="marc")

    ws_db.merge_issue_into(loser, winner, reason="test", actor="marc")

    assert ws_db.list_source_containers(issue_id=winner)[0]["id"] == "sc1"
    assert ws_db.list_source_containers(issue_id=loser) == []


def test_merge_issue_into_rejects_self_merge(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    with pytest.raises(ValueError):
        ws_db.merge_issue_into(a, a, reason="test", actor="marc")


def test_merge_issue_into_missing_issue_returns_not_found(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    result = ws_db.merge_issue_into("bogus-id", a, reason="test", actor="marc")
    assert result["status"] == "not_found"


def test_merge_issue_into_is_crash_safe(ws_db):
    """Same all-or-nothing discipline as merge_issues_txn - a crash partway
    through the multi-table move must leave NOTHING committed, not a
    partially-merged issue."""
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, loser)

    real_connect = sqlite3.connect

    class _CrashingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("UPDATE issues SET state = 'dismissed'"):
                result = super().execute(sql, *args, **kwargs)
                raise RuntimeError("simulated crash right after dismissing the loser")
            return super().execute(sql, *args, **kwargs)

    def fake_sqlite_connect(*args, **kwargs):
        kwargs["factory"] = _CrashingConnection
        return real_connect(*args, **kwargs)

    sqlite3.connect = fake_sqlite_connect
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            ws_db.merge_issue_into(loser, winner, reason="test crash", actor="marc")
    finally:
        sqlite3.connect = real_connect

    conn = ws_db._connect()
    assert conn.execute("SELECT issue_id FROM raw_items WHERE id = ?", (rid,)).fetchone()[0] == loser  # NOT moved
    conn.close()
    assert ws_db.get_issue(loser)["state"] == "active"  # NOT dismissed


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


def test_expire_stale_nba_choice_logs_expires_old_offered(ws_db):
    """Phase 0 fix (D12): 'expired' was a valid state from the start but
    nothing ever wrote it - an old open offer must not sit 'offered'
    forever once resolved by this sweep."""
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    log_id = ws_db.create_nba_choice_log(issue_id=iid, offered_json="[]", scoring_inputs_json="{}")
    conn = ws_db._connect()
    conn.execute("UPDATE nba_choice_log SET offered_ts = ? WHERE id = ?", (time.time() - 30 * 86400, log_id))
    conn.close()

    expired = ws_db.expire_stale_nba_choice_logs(14)

    assert expired == 1
    assert ws_db.get_most_recent_open_choice_log(iid) is None
    conn = ws_db._connect()
    status = conn.execute("SELECT status FROM nba_choice_log WHERE id = ?", (log_id,)).fetchone()[0]
    conn.close()
    assert status == "expired"


def test_expire_stale_nba_choice_logs_leaves_recent_offered(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    log_id = ws_db.create_nba_choice_log(issue_id=iid, offered_json="[]", scoring_inputs_json="{}")

    expired = ws_db.expire_stale_nba_choice_logs(14)

    assert expired == 0
    assert ws_db.get_most_recent_open_choice_log(iid)["id"] == log_id


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


# --- suggestion_kind / project_links (related-vs-same-project, 2026-07-31) -

def test_create_project_suggestion_defaults_to_merge_kind(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test")
    sugg = ws_db.get_project_suggestion(sid)
    assert sugg["suggestion_kind"] == "merge"


def test_create_project_suggestion_link_kind(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="link")
    sugg = ws_db.get_project_suggestion(sid)
    assert sugg["suggestion_kind"] == "link"


def test_create_project_suggestion_rejects_invalid_kind(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    with pytest.raises(ValueError):
        ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="bogus")


def test_create_project_suggestion_dedup_is_scoped_to_same_kind(ws_db):
    """A pending 'merge' suggestion for a pair must NOT be reused for a
    'link' suggestion on the same pair - they're different questions."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    merge_id = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="merge reason", suggestion_kind="merge")
    link_id = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="link reason", suggestion_kind="link")
    assert merge_id != link_id
    assert len(ws_db.list_project_suggestions(status="pending")) == 2


def test_create_project_suggestion_same_kind_dedups(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    first = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="r1", suggestion_kind="link")
    second = ws_db.create_project_suggestion(issue_id_a=b, issue_id_b=a, reason="r2", suggestion_kind="link")
    assert first == second
    assert len(ws_db.list_project_suggestions(status="pending")) == 1


def test_create_and_find_identity_constraint_either_ordering(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")

    cid = ws_db.create_identity_constraint("cannot_merge", a, b, "confirmed separate", actor="marc")
    assert cid is not None

    assert ws_db.find_identity_constraint("cannot_merge", a, b) is not None
    assert ws_db.find_identity_constraint("cannot_merge", b, a) is not None  # order-independent
    assert ws_db.find_identity_constraint("cannot_link", a, b) is None  # different type, no match


def test_find_identity_constraint_no_subject_b_matches_single_subject_types(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.create_identity_constraint("confirm_anchor", a, None, "test", actor="marc")

    assert ws_db.find_identity_constraint("confirm_anchor", a) is not None


def test_create_identity_constraint_rejects_invalid_type(ws_db):
    import sqlite3
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    with pytest.raises(sqlite3.IntegrityError):
        ws_db.create_identity_constraint("not_a_real_type", a, None, "test", actor="marc")


def test_list_identity_constraints_for_subject_either_side(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    c = ws_db.create_issue_with_new_id(title="C", state="active", category="other")
    ws_db.create_identity_constraint("cannot_merge", a, b, "r1", actor="marc")
    ws_db.create_identity_constraint("cannot_link", c, a, "r2", actor="marc")

    found = ws_db.list_identity_constraints_for_subject(a)

    assert len(found) == 2
    assert {c["constraint_type"] for c in found} == {"cannot_merge", "cannot_link"}


# --- work_object_signatures (Section 12.7) ---------------------------------

def test_upsert_and_get_work_object_signature(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.upsert_work_object_signature(
        a, definitive_ids_json='["PR1"]', accepted_lineages_json="[]", containers_json="[]",
        external_orgs_json='["acme"]', participant_roles_json="[]", active_period_start=100.0,
        active_period_end=200.0, positive_vocabulary_json=None, negative_vocabulary_json=None,
        cannot_link_ids_json="[]",
    )

    row = ws_db.get_work_object_signature(a)

    assert row["definitive_ids"] == '["PR1"]'
    assert row["external_orgs"] == '["acme"]'
    assert row["active_period_start"] == 100.0


def test_upsert_work_object_signature_replaces_existing_row(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    kwargs = dict(definitive_ids_json="[]", accepted_lineages_json="[]", containers_json="[]",
                  external_orgs_json="[]", participant_roles_json="[]", active_period_start=None,
                  active_period_end=None, positive_vocabulary_json=None, negative_vocabulary_json=None,
                  cannot_link_ids_json="[]")
    ws_db.upsert_work_object_signature(a, **kwargs)
    ws_db.upsert_work_object_signature(a, **{**kwargs, "external_orgs_json": '["updated"]'})

    row = ws_db.get_work_object_signature(a)
    assert row["external_orgs"] == '["updated"]'


def test_invalidate_work_object_signature_deletes_the_row(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.upsert_work_object_signature(
        a, definitive_ids_json="[]", accepted_lineages_json="[]", containers_json="[]",
        external_orgs_json="[]", participant_roles_json="[]", active_period_start=None,
        active_period_end=None, positive_vocabulary_json=None, negative_vocabulary_json=None,
        cannot_link_ids_json="[]",
    )

    ws_db.invalidate_work_object_signature(a)

    assert ws_db.get_work_object_signature(a) is None


def test_invalidate_work_object_signature_is_a_silent_noop_for_unknown_id(ws_db):
    ws_db.invalidate_work_object_signature("no-such-issue")  # must not raise


def test_link_raw_item_to_issue_invalidates_cached_signature(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.upsert_work_object_signature(
        a, definitive_ids_json="[]", accepted_lineages_json="[]", containers_json="[]",
        external_orgs_json="[]", participant_roles_json="[]", active_period_start=None,
        active_period_end=None, positive_vocabulary_json=None, negative_vocabulary_json=None,
        cannot_link_ids_json="[]",
    )
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )

    ws_db.link_raw_item_to_issue(rid, a)

    assert ws_db.get_work_object_signature(a) is None


def test_add_evidence_invalidates_cached_signature(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.upsert_work_object_signature(
        a, definitive_ids_json="[]", accepted_lineages_json="[]", containers_json="[]",
        external_orgs_json="[]", participant_roles_json="[]", active_period_start=None,
        active_period_end=None, positive_vocabulary_json=None, negative_vocabulary_json=None,
        cannot_link_ids_json="[]",
    )

    ws_db.add_evidence(issue_id=a, type="email", summary="test")

    assert ws_db.get_work_object_signature(a) is None


# --- artifact_lineages/artifact_versions (Section 12.5) --------------------

def test_create_attachment_unique_hash_creates_no_lineage(ws_db):
    """A genuinely unique hash never gets a speculative lineage - no real
    producer could connect it to a later version by content alone."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    aid = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="f.pdf",
        stored_path="f.pdf", content_type="application/pdf", size_bytes=10,
        sha256_hex="hash1", uploaded_by="marc",
    )

    assert ws_db.find_artifact_version_by_attachment(aid) is None


def test_create_attachment_matching_hash_creates_a_shared_lineage(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    aid1 = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="order_form.xlsx",
        stored_path="p1.xlsx", content_type=None, size_bytes=10,
        sha256_hex="sharedhash", uploaded_by="marc",
    )
    aid2 = ws_db.create_attachment(
        entity_type="issue", entity_id=b, kind="upload", filename="order_form_copy.xlsx",
        stored_path="p2.xlsx", content_type=None, size_bytes=10,
        sha256_hex="sharedhash", uploaded_by="marc",
    )

    v1 = ws_db.find_artifact_version_by_attachment(aid1)
    v2 = ws_db.find_artifact_version_by_attachment(aid2)
    assert v1 is not None and v2 is not None
    assert v1["lineage_id"] == v2["lineage_id"]
    lineage = ws_db.get_artifact_lineage(v1["lineage_id"])
    assert lineage["work_object_id"] == a  # anchored on the earliest (first-created) attachment
    assert lineage["title"] == "order_form.xlsx"


def test_create_attachment_new_lineage_invalidates_the_owning_signature(ws_db):
    """Section 12.5/12.7 coordination: accepted_lineages is now real,
    populated data on the cached signature - a lineage created here must
    invalidate the owning work_object's cache the same way link_party_to_
    issue/add_evidence/etc already do for their own fields."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.upsert_work_object_signature(
        a, definitive_ids_json="[]", accepted_lineages_json="[]", containers_json="[]",
        external_orgs_json="[]", participant_roles_json="[]", active_period_start=None,
        active_period_end=None, positive_vocabulary_json=None, negative_vocabulary_json=None,
        cannot_link_ids_json="[]",
    )

    ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="f1.pdf",
        stored_path="p1.pdf", content_type=None, size_bytes=10, sha256_hex="hinv", uploaded_by="marc",
    )
    ws_db.create_attachment(
        entity_type="issue", entity_id=b, kind="upload", filename="f2.pdf",
        stored_path="p2.pdf", content_type=None, size_bytes=10, sha256_hex="hinv", uploaded_by="marc",
    )

    assert ws_db.get_work_object_signature(a) is None  # invalidated, not left stale


def test_create_attachment_third_matching_hash_joins_the_same_lineage(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    aid1 = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="f1.pdf",
        stored_path="p1.pdf", content_type=None, size_bytes=10, sha256_hex="h3", uploaded_by="marc",
    )
    aid2 = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="f2.pdf",
        stored_path="p2.pdf", content_type=None, size_bytes=10, sha256_hex="h3", uploaded_by="marc",
    )
    aid3 = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="f3.pdf",
        stored_path="p3.pdf", content_type=None, size_bytes=10, sha256_hex="h3", uploaded_by="marc",
    )

    lineage_id = ws_db.find_artifact_version_by_attachment(aid1)["lineage_id"]
    assert ws_db.find_artifact_version_by_attachment(aid2)["lineage_id"] == lineage_id
    assert ws_db.find_artifact_version_by_attachment(aid3)["lineage_id"] == lineage_id
    assert len(ws_db.list_artifact_versions_for_lineage(lineage_id)) == 3


# --- attachment-hash consumer surfacing (v2.9) -----------------------------

def test_list_other_occurrences_for_attachment_empty_for_a_unique_hash(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    aid = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="f.pdf",
        stored_path="f.pdf", content_type=None, size_bytes=10, sha256_hex="unique1", uploaded_by="marc",
    )

    assert ws_db.list_other_occurrences_for_attachment(aid) == []


def test_list_other_occurrences_for_attachment_surfaces_the_other_issue(ws_db):
    a = ws_db.create_issue_with_new_id(title="Contract A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="Contract B", state="active", category="other")
    aid1 = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="order_form.xlsx",
        stored_path="p1.xlsx", content_type=None, size_bytes=10, sha256_hex="sharedh", uploaded_by="marc",
    )
    aid2 = ws_db.create_attachment(
        entity_type="issue", entity_id=b, kind="upload", filename="order_form_copy.xlsx",
        stored_path="p2.xlsx", content_type=None, size_bytes=10, sha256_hex="sharedh", uploaded_by="marc",
    )

    occurrences_from_1 = ws_db.list_other_occurrences_for_attachment(aid1)
    assert len(occurrences_from_1) == 1
    assert occurrences_from_1[0]["attachment_id"] == aid2
    assert occurrences_from_1[0]["work_object_id"] == b
    assert occurrences_from_1[0]["work_object_title"] == "Contract B"

    occurrences_from_2 = ws_db.list_other_occurrences_for_attachment(aid2)
    assert len(occurrences_from_2) == 1
    assert occurrences_from_2[0]["attachment_id"] == aid1
    assert occurrences_from_2[0]["work_object_title"] == "Contract A"


def test_list_other_occurrences_for_attachment_none_title_for_unresolved_work_object(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )  # never linked to an issue - entity resolves to no real work_object
    aid1 = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="f1.pdf",
        stored_path="p1.pdf", content_type=None, size_bytes=10, sha256_hex="sh2", uploaded_by="marc",
    )
    aid2 = ws_db.create_attachment(
        entity_type="raw_item", entity_id=str(rid), kind="reference", filename="f2.pdf",
        stored_path="p2.pdf", content_type=None, size_bytes=10, sha256_hex="sh2", uploaded_by="outlook_ingest",
    )

    occurrences = ws_db.list_other_occurrences_for_attachment(aid1)

    assert len(occurrences) == 1
    assert occurrences[0]["attachment_id"] == aid2
    assert occurrences[0]["work_object_id"] is None
    assert occurrences[0]["work_object_title"] is None


# --- attachment extracted_text backfill support (E6) -----------------------

def test_list_attachments_missing_extracted_text_matches_extension_case_insensitively(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    aid = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="Notice.DOCX",
        stored_path="p.docx", content_type=None, size_bytes=10, sha256_hex=None, uploaded_by="marc",
    )

    found = ws_db.list_attachments_missing_extracted_text((".docx",))

    assert [f["id"] for f in found] == [aid]


def test_list_attachments_missing_extracted_text_excludes_already_extracted(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="notice.docx",
        stored_path="p.docx", content_type=None, size_bytes=10, sha256_hex=None, uploaded_by="marc",
        extracted_text="already has real text",
    )

    assert ws_db.list_attachments_missing_extracted_text((".docx",)) == []


def test_list_attachments_missing_extracted_text_excludes_other_extensions(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="notice.pdf",
        stored_path="p.pdf", content_type=None, size_bytes=10, sha256_hex=None, uploaded_by="marc",
    )

    assert ws_db.list_attachments_missing_extracted_text((".docx",)) == []


def test_update_attachment_extracted_text_persists(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    aid = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="notice.docx",
        stored_path="p.docx", content_type=None, size_bytes=10, sha256_hex=None, uploaded_by="marc",
    )

    ws_db.update_attachment_extracted_text(aid, "real extracted text")

    assert ws_db.get_attachment(aid)["extracted_text"] == "real extracted text"


def test_work_object_id_for_attachment_resolves_via_raw_item(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, a)
    attachment = {"entity_type": "raw_item", "entity_id": str(rid)}

    assert ws_db._work_object_id_for_attachment(attachment) == a


def test_work_object_id_for_attachment_none_for_project_scoped(ws_db):
    p = ws_db.create_project_with_new_id(name="P", category="other")
    attachment = {"entity_type": "project", "entity_id": p}

    assert ws_db._work_object_id_for_attachment(attachment) is None


def test_backfill_artifact_lineages_is_idempotent(ws_db):
    """Simulates duplicate attachments that predate the feature (inserted
    directly, bypassing create_attachment's live hook) - the real
    scenario backfill_artifact_lineages exists for."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    conn = ws_db._connect()
    conn.execute(
        """INSERT INTO attachments (entity_type, entity_id, kind, filename, stored_path,
           content_type, size_bytes, sha256, uploaded_by, uploaded_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("issue", a, "upload", "old1.pdf", "old1.pdf", None, 10, "prehash", "marc", 100.0),
    )
    conn.execute(
        """INSERT INTO attachments (entity_type, entity_id, kind, filename, stored_path,
           content_type, size_bytes, sha256, uploaded_by, uploaded_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("issue", a, "upload", "old2.pdf", "old2.pdf", None, 10, "prehash", "marc", 200.0),
    )
    conn.commit()
    conn.close()

    first = ws_db.backfill_artifact_lineages()
    assert first["duplicate_groups_found"] == 1
    assert first["lineages_created"] == 1

    second = ws_db.backfill_artifact_lineages()
    assert second["lineages_created"] == 0  # already linked - nothing new to create


def test_backfill_artifact_lineages_invalidates_a_preexisting_cached_signature(ws_db):
    """The real bug this regression-tests: a signature computed and
    cached (e.g. by backtest_scored_model, run for shadow-comparison
    purposes on every real issue) BEFORE a historical duplicate group was
    backfilled must not keep reporting a stale, empty accepted_lineages
    forever - only _ensure_artifact_versions' own invalidation (shared by
    both create_attachment's live hook and this backfill) can catch it,
    since nothing else would ever re-touch this work_object afterward."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.upsert_work_object_signature(
        a, definitive_ids_json="[]", accepted_lineages_json="[]", containers_json="[]",
        external_orgs_json="[]", participant_roles_json="[]", active_period_start=None,
        active_period_end=None, positive_vocabulary_json=None, negative_vocabulary_json=None,
        cannot_link_ids_json="[]",
    )
    conn = ws_db._connect()
    conn.execute(
        """INSERT INTO attachments (entity_type, entity_id, kind, filename, stored_path,
           content_type, size_bytes, sha256, uploaded_by, uploaded_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("issue", a, "upload", "old1.pdf", "old1.pdf", None, 10, "prehash2", "marc", 100.0),
    )
    conn.execute(
        """INSERT INTO attachments (entity_type, entity_id, kind, filename, stored_path,
           content_type, size_bytes, sha256, uploaded_by, uploaded_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("issue", a, "upload", "old2.pdf", "old2.pdf", None, 10, "prehash2", "marc", 200.0),
    )
    conn.commit()
    conn.close()
    assert ws_db.get_work_object_signature(a) is not None  # the stale cache exists before backfill

    ws_db.backfill_artifact_lineages()

    assert ws_db.get_work_object_signature(a) is None  # invalidated, not left stale


# --- prepared_actions (Section 12.4) ---------------------------------------

def test_create_and_get_prepared_action(ws_db):
    pid = ws_db.create_prepared_action(
        claim_id=None, action_type="draft_reply", proposed_parameters_json="{}",
        evidence_refs_json="[]", rationale="test", risk_class="low", idempotency_key="key1",
    )

    row = ws_db.get_prepared_action(pid)
    assert row["action_type"] == "draft_reply"
    assert row["state"] == "proposed"  # default
    assert row["required_approval"] == 1  # default
    assert row["resolved_ts"] is None


def test_create_prepared_action_rejects_invalid_risk_class(ws_db):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        ws_db.create_prepared_action(
            claim_id=None, action_type="draft_reply", proposed_parameters_json="{}",
            evidence_refs_json="[]", rationale="test", risk_class="not_a_real_class", idempotency_key="key2",
        )


def test_create_prepared_action_rejects_duplicate_idempotency_key(ws_db):
    import sqlite3
    ws_db.create_prepared_action(
        claim_id=None, action_type="draft_reply", proposed_parameters_json="{}",
        evidence_refs_json="[]", rationale="test", risk_class="low", idempotency_key="dup-key",
    )
    with pytest.raises(sqlite3.IntegrityError):
        ws_db.create_prepared_action(
            claim_id=None, action_type="draft_reply", proposed_parameters_json="{}",
            evidence_refs_json="[]", rationale="test again", risk_class="low", idempotency_key="dup-key",
        )


def test_find_prepared_action_by_idempotency_key(ws_db):
    pid = ws_db.create_prepared_action(
        claim_id=None, action_type="draft_reply", proposed_parameters_json="{}",
        evidence_refs_json="[]", rationale="test", risk_class="low", idempotency_key="findme",
    )

    found = ws_db.find_prepared_action_by_idempotency_key("findme")
    assert found["id"] == pid
    assert ws_db.find_prepared_action_by_idempotency_key("no-such-key") is None


def test_update_prepared_action_state_stamps_resolved_ts_only_on_terminal_state(ws_db):
    pid = ws_db.create_prepared_action(
        claim_id=None, action_type="draft_reply", proposed_parameters_json="{}",
        evidence_refs_json="[]", rationale="test", risk_class="low", idempotency_key="k3",
    )

    ws_db.update_prepared_action_state(pid, "executing")
    mid = ws_db.get_prepared_action(pid)
    assert mid["state"] == "executing"
    assert mid["resolved_ts"] is None  # not a terminal state

    ws_db.update_prepared_action_state(pid, "succeeded", policy_result="ok")
    done = ws_db.get_prepared_action(pid)
    assert done["state"] == "succeeded"
    assert done["policy_result"] == "ok"
    assert done["resolved_ts"] is not None


def test_list_prepared_actions_for_claim(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, a)
    claim_id = ws_db.insert_claim(
        issue_id=a, raw_item_id=rid, claim_type="ask", text="approve this",
        author="counterparty", author_basis="direction", owner="marc", ts=time.time(),
    )
    ws_db.create_prepared_action(
        claim_id=claim_id, action_type="draft_reply", proposed_parameters_json="{}",
        evidence_refs_json="[]", rationale="test", risk_class="low", idempotency_key="k4",
    )

    found = ws_db.list_prepared_actions_for_claim(claim_id)
    assert len(found) == 1
    assert found[0]["claim_id"] == claim_id


def test_expire_stale_prepared_actions_only_touches_non_terminal_past_cutoff(ws_db):
    now = time.time()
    stale_id = ws_db.create_prepared_action(
        claim_id=None, action_type="draft_reply", proposed_parameters_json="{}",
        evidence_refs_json="[]", rationale="test", risk_class="low", idempotency_key="stale",
    )
    fresh_id = ws_db.create_prepared_action(
        claim_id=None, action_type="draft_reply", proposed_parameters_json="{}",
        evidence_refs_json="[]", rationale="test", risk_class="low", idempotency_key="fresh",
    )
    terminal_id = ws_db.create_prepared_action(
        claim_id=None, action_type="draft_reply", proposed_parameters_json="{}",
        evidence_refs_json="[]", rationale="test", risk_class="low", idempotency_key="terminal",
    )
    conn = ws_db._connect()
    conn.execute("UPDATE prepared_actions SET created_ts = ? WHERE id = ?", (now - 7200, stale_id))
    conn.execute("UPDATE prepared_actions SET created_ts = ? WHERE id = ?", (now - 7200, terminal_id))
    conn.commit()
    conn.close()
    ws_db.update_prepared_action_state(terminal_id, "succeeded")

    expired = ws_db.expire_stale_prepared_actions(3600, now=now)

    assert expired == 1
    assert ws_db.get_prepared_action(stale_id)["state"] == "expired"
    assert ws_db.get_prepared_action(fresh_id)["state"] == "proposed"  # too recent, untouched
    assert ws_db.get_prepared_action(terminal_id)["state"] == "succeeded"  # already terminal, untouched


def test_run_prepared_action_expiry_daily_if_due_only_runs_once_a_day(ws_db):
    now = time.time()
    stale_id = ws_db.create_prepared_action(
        claim_id=None, action_type="draft_reply", proposed_parameters_json="{}",
        evidence_refs_json="[]", rationale="test", risk_class="low", idempotency_key="daily1",
    )
    conn = ws_db._connect()
    conn.execute("UPDATE prepared_actions SET created_ts = ? WHERE id = ?", (now - 7200, stale_id))
    conn.commit()
    conn.close()

    first = ws_db.run_prepared_action_expiry_daily_if_due(now=now)
    assert first == 1
    assert ws_db.get_prepared_action(stale_id)["state"] == "expired"

    second = ws_db.run_prepared_action_expiry_daily_if_due(now=now)
    assert second is None  # already claimed today


# --- membership_state / exposure_state (Section 12.8) ----------------------

def test_new_work_object_defaults_provisional_and_not_exposed(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")

    state = ws_db.get_work_object_membership_exposure(a)

    assert state["membership_state"] == "provisional"
    assert state["exposure_state"] == "not_exposed"


def test_confirm_work_object_membership_sets_confirmed(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")

    ws_db.confirm_work_object_membership(a)

    assert ws_db.get_work_object_membership_exposure(a)["membership_state"] == "confirmed"


def test_advance_work_object_exposure_state_moves_forward(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")

    ws_db.advance_work_object_exposure_state(a, "shown_in_project")
    assert ws_db.get_work_object_membership_exposure(a)["exposure_state"] == "shown_in_project"

    ws_db.advance_work_object_exposure_state(a, "used_for_action")
    assert ws_db.get_work_object_membership_exposure(a)["exposure_state"] == "used_for_action"


def test_advance_work_object_exposure_state_never_regresses(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.advance_work_object_exposure_state(a, "used_for_action")

    ws_db.advance_work_object_exposure_state(a, "shown_in_project")  # lower rank - must be a no-op

    assert ws_db.get_work_object_membership_exposure(a)["exposure_state"] == "used_for_action"


def test_upsert_synthesis_advances_exposure_state_to_used_in_summary(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")

    ws_db.upsert_synthesis(
        entity_type="issue", entity_id=a, summary="test summary",
        next_steps_json="[]", suggested_actions_json="[]", synthesized_from_marker="rev:1",
    )

    assert ws_db.get_work_object_membership_exposure(a)["exposure_state"] == "used_in_summary"


# --- three-tier timeline (Section 12.9) ------------------------------------

def test_list_complete_timeline_includes_evidence_and_claim_events(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    ws_db.add_evidence(issue_id=a, type="email", summary="an email arrived")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, a)
    claim_id = ws_db.insert_claim(
        issue_id=a, raw_item_id=rid, claim_type="ask", text="approve this",
        author="counterparty", author_basis="direction", owner="marc", ts=time.time(),
    )
    ws_db.log_claim_event(claim_id, "create", actor="curator")

    timeline = ws_db.list_complete_timeline_for_issue(a)

    kinds = [(e["tier"], e["kind"]) for e in timeline]
    assert ("evidence", "email") in kinds
    assert ("claim_event", "create") in kinds
    assert timeline == sorted(timeline, key=lambda e: e["ts"])  # chronological


def test_list_milestone_timeline_includes_artifact_versions_and_state_transitions(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    aid = ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="f1.pdf",
        stored_path="p1.pdf", content_type=None, size_bytes=10, sha256_hex="hmilestone", uploaded_by="marc",
    )
    ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="f2.pdf",
        stored_path="p2.pdf", content_type=None, size_bytes=10, sha256_hex="hmilestone", uploaded_by="marc",
    )
    conn = ws_db._connect()
    conn.execute(
        "INSERT INTO issue_state_history (issue_id, from_state, to_state, changed_ts, actor) VALUES (?, 'active', 'blocked', ?, 'marc')",
        (a, time.time()),
    )
    conn.commit()
    conn.close()

    milestones = ws_db.list_milestone_timeline_for_issue(a)

    kinds = [e["kind"] for e in milestones]
    assert "artifact_version_produced" in kinds
    assert "issue_blocked" in kinds


def test_list_activity_stream_excludes_evidence_behind_a_milestone_claim(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid_ask = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid_ask, a)
    claim_id = ws_db.insert_claim(
        issue_id=a, raw_item_id=rid_ask, claim_type="ask", text="approve this",
        author="counterparty", author_basis="direction", owner="marc", ts=time.time(),
    )
    ws_db.log_claim_event(claim_id, "create", actor="curator")
    ws_db.add_evidence(issue_id=a, type="email", summary="the ask email", raw_item_id=rid_ask)
    ws_db.add_evidence(issue_id=a, type="email", summary="an unrelated FYI email", raw_item_id=None)

    activity = ws_db.list_activity_stream_for_issue(a)

    summaries = [e["summary"] for e in activity]
    assert "an unrelated FYI email" in summaries
    assert "the ask email" not in summaries  # behind a milestone claim's raw_item - excluded


def test_expire_stale_project_suggestions_expires_old_pending_merge(ws_db):
    """Phase 0 fix (D2): the structural backstop against the pending queue
    accumulating forever, independent of the generation flag's setting."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="merge")
    conn = ws_db._connect()
    conn.execute("UPDATE pending_project_suggestions SET created_ts = ? WHERE id = ?",
                 (time.time() - 30 * 86400, sid))
    conn.close()

    expired = ws_db.expire_stale_project_suggestions(21)

    assert expired == 1
    assert ws_db.get_project_suggestion(sid)["status"] == "expired"


def test_expire_stale_project_suggestions_leaves_recent_pending(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="merge")

    expired = ws_db.expire_stale_project_suggestions(21)

    assert expired == 0
    assert ws_db.get_project_suggestion(sid)["status"] == "pending"


def test_expire_stale_project_suggestions_exempts_link_kind_by_default(ws_db):
    """A 'related' suggestion doesn't go stale the way a same-project merge
    guess does - Marc may still want to confirm it long after it surfaced."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="link")
    conn = ws_db._connect()
    conn.execute("UPDATE pending_project_suggestions SET created_ts = ? WHERE id = ?",
                 (time.time() - 30 * 86400, sid))
    conn.close()

    expired = ws_db.expire_stale_project_suggestions(21)

    assert expired == 0
    assert ws_db.get_project_suggestion(sid)["status"] == "pending"


def test_create_project_link_persists_and_is_idempotent(ws_db):
    p1 = ws_db.create_project_with_new_id(name="P1", category="other")
    p2 = ws_db.create_project_with_new_id(name="P2", category="other")
    first = ws_db.create_project_link(from_project_id=p1, to_project_id=p2, link_type="related",
                                       reason="same vendor, adjacent topics", created_by="marc")
    second = ws_db.create_project_link(from_project_id=p1, to_project_id=p2, link_type="related",
                                        reason="duplicate call", created_by="marc")
    assert first == second
    links = ws_db.list_project_links_for_project(p1)
    assert len(links) == 1
    assert links[0]["link_type"] == "related"
    assert links[0]["reason"] == "same vendor, adjacent topics"


def test_create_project_link_different_type_is_not_deduped(ws_db):
    p1 = ws_db.create_project_with_new_id(name="P1", category="other")
    p2 = ws_db.create_project_with_new_id(name="P2", category="other")
    ws_db.create_project_link(from_project_id=p1, to_project_id=p2, link_type="related", reason="r1")
    ws_db.create_project_link(from_project_id=p1, to_project_id=p2, link_type="enables", reason="r2")
    assert len(ws_db.list_project_links_for_project(p1)) == 2


def test_list_project_links_for_project_finds_links_from_either_direction(ws_db):
    p1 = ws_db.create_project_with_new_id(name="P1", category="other")
    p2 = ws_db.create_project_with_new_id(name="P2", category="other")
    ws_db.create_project_link(from_project_id=p1, to_project_id=p2, link_type="enables", reason="r")
    assert len(ws_db.list_project_links_for_project(p1)) == 1
    assert len(ws_db.list_project_links_for_project(p2)) == 1


def test_create_project_link_rejects_invalid_link_type(ws_db):
    p1 = ws_db.create_project_with_new_id(name="P1", category="other")
    p2 = ws_db.create_project_with_new_id(name="P2", category="other")
    with pytest.raises(sqlite3.IntegrityError):
        ws_db.create_project_link(from_project_id=p1, to_project_id=p2, link_type="bogus", reason="r")


# --- remediate_merge_issue_identity (step 7, calendar-series remediation) -

def test_remediate_merge_issue_identity_reassigns_raw_items_and_archives_loser(ws_db):
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    loser = ws_db.create_issue_with_new_id(title="Loser occurrence", state="active", category="other")
    rid = ws_db.insert_raw_item(source="calendar", stable_key="evt-1", thread_key="evt-1", dedupe_key="dk1",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, loser)

    ws_db.remediate_merge_issue_identity(winner, loser, reason_label="test remediation")

    assert ws_db.get_raw_item(rid)["issue_id"] == winner
    assert ws_db.get_issue(loser)["state"] == "noise-archived"


def test_remediate_merge_issue_identity_moves_all_fk_tables(ws_db):
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    ws_db.create_task(issue_id=loser, label="a task", owner=None)

    ws_db.remediate_merge_issue_identity(winner, loser, reason_label="test")

    tasks = ws_db.list_tasks_for_issue(winner) if hasattr(ws_db, "list_tasks_for_issue") else None
    conn = ws_db._connect()
    row = conn.execute("SELECT issue_id FROM work_tasks WHERE label = ?", ("a task",)).fetchone()
    conn.close()
    assert row["issue_id"] == winner


def test_remediate_merge_issue_identity_dedupes_shared_party(ws_db):
    """The real shape: the SAME organizer/party is linked to both the
    winner and the loser (every occurrence of a recurring series shares
    the organizer) - must not violate issue_parties' (issue_id, party_id)
    primary key."""
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    ws_db.upsert_party(id="shared-organizer", primary_email="org@example.com", display_name="Organizer",
                        affiliation="internal", affiliation_confidence="H", affiliation_source="domain", company=None)
    ws_db.link_party_to_issue(winner, "shared-organizer")
    ws_db.link_party_to_issue(loser, "shared-organizer")

    ws_db.remediate_merge_issue_identity(winner, loser, reason_label="test")

    parties = ws_db.list_parties_for_issue(winner)
    assert len([p for p in parties if p["id"] == "shared-organizer"]) == 1


def test_remediate_merge_issue_identity_moves_a_party_only_the_loser_had(ws_db):
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    ws_db.upsert_party(id="loser-only", primary_email="attendee@example.com", display_name="Attendee",
                        affiliation="internal", affiliation_confidence="H", affiliation_source="domain", company=None)
    ws_db.link_party_to_issue(loser, "loser-only")

    ws_db.remediate_merge_issue_identity(winner, loser, reason_label="test")

    parties = ws_db.list_parties_for_issue(winner)
    assert any(p["id"] == "loser-only" for p in parties)


def test_list_parties_for_issues_batched_matches_per_issue_calls(ws_db):
    """N+1 fix (2026-08-02): list_parties_for_issues must return the exact
    same result as calling list_parties_for_issue once per issue, but via a
    single query across every issue - see its own docstring for why this
    exists (removing a per-member-issue fetch the project detail page used
    to make just for row party chips)."""
    iid1 = ws_db.create_issue_with_new_id(title="One", state="active", category="other")
    iid2 = ws_db.create_issue_with_new_id(title="Two", state="active", category="other")
    iid3 = ws_db.create_issue_with_new_id(title="No parties", state="active", category="other")
    ws_db.upsert_party(id="party-a", primary_email="a@example.com", display_name="A",
                        affiliation="internal", affiliation_confidence="H", affiliation_source="domain", company=None)
    ws_db.upsert_party(id="party-b", primary_email="b@example.com", display_name="B",
                        affiliation="external", affiliation_confidence="M", affiliation_source="domain", company="Acme")
    ws_db.link_party_to_issue(iid1, "party-a")
    ws_db.link_party_to_issue(iid2, "party-b")

    batched = ws_db.list_parties_for_issues([iid1, iid2, iid3])

    assert [p["id"] for p in batched[iid1]] == ["party-a"]
    assert [p["id"] for p in batched[iid2]] == ["party-b"]
    assert batched[iid3] == []
    assert batched[iid1] == ws_db.list_parties_for_issue(iid1)
    assert batched[iid2] == ws_db.list_parties_for_issue(iid2)


def test_list_parties_for_issues_empty_list_returns_empty_dict(ws_db):
    assert ws_db.list_parties_for_issues([]) == {}


def test_list_parties_for_issues_same_party_on_multiple_issues(ws_db):
    """A shared party (e.g. the same external contact on two issues in one
    project) must appear under EACH issue's own entry, not just one."""
    iid1 = ws_db.create_issue_with_new_id(title="One", state="active", category="other")
    iid2 = ws_db.create_issue_with_new_id(title="Two", state="active", category="other")
    ws_db.upsert_party(id="shared", primary_email="shared@example.com", display_name="Shared",
                        affiliation="external", affiliation_confidence="H", affiliation_source="domain", company=None)
    ws_db.link_party_to_issue(iid1, "shared")
    ws_db.link_party_to_issue(iid2, "shared")

    batched = ws_db.list_parties_for_issues([iid1, iid2])

    assert [p["id"] for p in batched[iid1]] == ["shared"]
    assert [p["id"] for p in batched[iid2]] == ["shared"]


def test_upsert_source_container_is_idempotent(ws_db):
    ws_db.upsert_source_container(id="sc1", source="outlook_mail", container_type="email_conversation",
                                   exact_key="conv1", key_quality="exact", issue_id="marc-1")
    ws_db.upsert_source_container(id="sc1", source="outlook_mail", container_type="email_conversation",
                                   exact_key="conv1", key_quality="exact", issue_id="marc-2")
    rows = ws_db.list_source_containers()
    assert len(rows) == 1
    assert rows[0]["issue_id"] == "marc-2"


def test_list_source_containers_filters_by_issue(ws_db):
    ws_db.upsert_source_container(id="sc1", source="outlook_mail", container_type="email_conversation",
                                   exact_key="conv1", key_quality="exact", issue_id="marc-1")
    ws_db.upsert_source_container(id="sc2", source="teams_chat", container_type="teams_chat",
                                   exact_key="chat1", key_quality="exact", issue_id="marc-2")
    assert [r["id"] for r in ws_db.list_source_containers(issue_id="marc-1")] == ["sc1"]


def test_upsert_work_object_relationship_normalizes_pair_order(ws_db):
    """Task #184 Phase D: the same real pair, detected from either
    direction on two different passes, must land as ONE row - not two
    duplicates depending on which side happened to be iterated first."""
    ws_db.upsert_work_object_relationship(a_id="marc-b", b_id="marc-a", relationship_type="candidate",
                                           match_count=2, matched_signals=["supplier", "stakeholder"])
    ws_db.upsert_work_object_relationship(a_id="marc-a", b_id="marc-b", relationship_type="candidate",
                                           match_count=3, matched_signals=["supplier", "stakeholder", "amount"])
    row = ws_db.get_work_object_relationship("marc-a", "marc-b")
    assert row is not None
    assert row["from_id"] == "marc-a" and row["to_id"] == "marc-b"
    assert row["match_count"] == 3  # the later, richer pass's count won - a real update, not a stale duplicate


def test_upsert_work_object_relationship_never_overwrites_a_resolved_decision(ws_db):
    """A curator/human judgment (confirmed or rejected) is permanent -
    a later detection pass re-finding the same pair as a fresh candidate
    must never silently re-litigate it."""
    rid = ws_db.upsert_work_object_relationship(a_id="marc-a", b_id="marc-b", relationship_type="candidate",
                                                  match_count=2, matched_signals=["supplier", "stakeholder"])
    ws_db.resolve_work_object_relationship(rid, "rejected")
    ws_db.upsert_work_object_relationship(a_id="marc-a", b_id="marc-b", relationship_type="candidate",
                                           match_count=4, matched_signals=["supplier", "stakeholder", "amount", "document"])
    row = ws_db.get_work_object_relationship("marc-a", "marc-b")
    assert row["relationship_type"] == "rejected"
    assert row["match_count"] == 2  # untouched - the rejection stands regardless of later match strength


def test_list_pending_work_object_relationships_ranks_by_match_count_desc(ws_db):
    ws_db.upsert_work_object_relationship(a_id="marc-a", b_id="marc-b", relationship_type="candidate",
                                           match_count=2, matched_signals=["supplier", "stakeholder"])
    ws_db.upsert_work_object_relationship(a_id="marc-c", b_id="marc-d", relationship_type="candidate",
                                           match_count=4, matched_signals=["supplier", "stakeholder", "amount", "document"])
    ws_db.upsert_work_object_relationship(a_id="marc-e", b_id="marc-f", relationship_type="bridge",
                                           match_count=3, matched_signals=["supplier", "amount", "document"])
    rows = ws_db.list_pending_work_object_relationships()
    assert [r["match_count"] for r in rows] == [4, 3, 2]


def test_list_pending_work_object_relationships_excludes_resolved(ws_db):
    rid = ws_db.upsert_work_object_relationship(a_id="marc-a", b_id="marc-b", relationship_type="candidate",
                                                   match_count=2, matched_signals=["supplier", "stakeholder"])
    ws_db.resolve_work_object_relationship(rid, "confirmed")
    assert ws_db.list_pending_work_object_relationships() == []


def test_list_work_object_relationships_for_finds_either_direction(ws_db):
    ws_db.upsert_work_object_relationship(a_id="marc-a", b_id="marc-b", relationship_type="candidate",
                                           match_count=2, matched_signals=["supplier", "stakeholder"])
    ws_db.upsert_work_object_relationship(a_id="marc-c", b_id="marc-a", relationship_type="candidate",
                                           match_count=2, matched_signals=["amount", "document"])
    rows = ws_db.list_work_object_relationships_for("marc-a")
    assert len(rows) == 2
    pairs = {(r["from_id"], r["to_id"]) for r in rows}
    assert pairs == {("marc-a", "marc-b"), ("marc-a", "marc-c")}


def test_upsert_source_session_is_idempotent_and_updates_end(ws_db):
    ws_db.upsert_source_container(id="sc1", source="teams_chat", container_type="teams_chat",
                                   exact_key="chat1", key_quality="exact", issue_id=None)
    ws_db.upsert_source_session(id="ss1", source_container_id="sc1", session_sequence=0,
                                 started_ts=100.0, ended_ts=None, boundary_reason="first_message")
    ws_db.upsert_source_session(id="ss1", source_container_id="sc1", session_sequence=0,
                                 started_ts=100.0, ended_ts=200.0, boundary_reason="first_message")
    rows = ws_db.list_source_sessions("sc1")
    assert len(rows) == 1
    assert rows[0]["ended_ts"] == 200.0


def test_list_source_sessions_orders_by_sequence(ws_db):
    ws_db.upsert_source_container(id="sc1", source="teams_chat", container_type="teams_chat",
                                   exact_key="chat1", key_quality="exact", issue_id=None)
    ws_db.upsert_source_session(id="ss2", source_container_id="sc1", session_sequence=1,
                                 started_ts=200.0, ended_ts=None, boundary_reason="gap_exceeds_72h")
    ws_db.upsert_source_session(id="ss1", source_container_id="sc1", session_sequence=0,
                                 started_ts=100.0, ended_ts=150.0, boundary_reason="first_message")
    rows = ws_db.list_source_sessions("sc1")
    assert [r["session_sequence"] for r in rows] == [0, 1]


def test_create_identity_anchor_dedupes_same_issue(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    first = ws_db.create_identity_anchor(anchor_type="reference", normalized_value="PR1", anchor_strength="strong",
                                          exclusive=True, issue_id=iid)
    second = ws_db.create_identity_anchor(anchor_type="reference", normalized_value="PR1", anchor_strength="strong",
                                           exclusive=True, issue_id=iid)
    assert first is not None
    assert second is None  # already recorded for this issue - not a conflict, just a no-op
    assert len(ws_db.list_identity_anchors(issue_id=iid)) == 1


def test_create_identity_anchor_exclusive_conflict_on_different_issue_returns_none(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ws_db.create_identity_anchor(anchor_type="reference", normalized_value="PR1", anchor_strength="strong",
                                  exclusive=True, issue_id=a)
    result = ws_db.create_identity_anchor(anchor_type="reference", normalized_value="PR1", anchor_strength="strong",
                                           exclusive=True, issue_id=b)
    assert result is None
    assert ws_db.list_identity_anchors(issue_id=b) == []
    assert len(ws_db.list_identity_anchors(issue_id=a)) == 1


def test_create_identity_anchor_non_exclusive_allows_multiple_issues(ws_db):
    """A shared party/company is a real, legitimate relationship signal on
    MANY issues at once - never blocked by the exclusive-anchor index."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    first = ws_db.create_identity_anchor(anchor_type="party", normalized_value="p1", anchor_strength="weak",
                                          exclusive=False, issue_id=a)
    second = ws_db.create_identity_anchor(anchor_type="party", normalized_value="p1", anchor_strength="weak",
                                           exclusive=False, issue_id=b)
    assert first is not None
    assert second is not None


def test_list_raw_items_by_thread_key_spans_multiple_issues(ws_db):
    """The real reason this exists: today's flat thread_key-per-container
    model may already have split one Teams chat's history across more
    than one issue - get_raw_items_for_issue alone can't see the full
    container, this can."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    r1 = ws_db.insert_raw_item(source="teams_chat", stable_key="c1:m1", thread_key="c1", dedupe_key="d1",
                                occurred_ts=1.0, subject=None, from_actor="x", participants_json="[]")
    r2 = ws_db.insert_raw_item(source="teams_chat", stable_key="c1:m2", thread_key="c1", dedupe_key="d2",
                                occurred_ts=2.0, subject=None, from_actor="x", participants_json="[]")
    ws_db.link_raw_item_to_issue(r1, a)
    ws_db.link_raw_item_to_issue(r2, b)

    rows = ws_db.list_raw_items_by_thread_key("teams_chat", "c1")

    assert [r["id"] for r in rows] == [r1, r2]


def test_get_calendar_raw_items_for_remediation_only_returns_calendar_source(ws_db):
    ws_db.insert_raw_item(source="calendar", stable_key="c1", thread_key="c1", dedupe_key="dkc1",
                           occurred_ts=time.time(), subject="cal", from_actor="a@example.com", participants_json="[]")
    ws_db.insert_raw_item(source="outlook_mail", stable_key="m1", thread_key="m1", dedupe_key="dkm1",
                           occurred_ts=time.time(), subject="mail", from_actor="a@example.com", participants_json="[]")

    rows = ws_db.get_calendar_raw_items_for_remediation()

    assert len(rows) == 1
    assert rows[0]["subject"] == "cal"


# --- richer calendar/meeting data (enhancement idea panel #7) -------------

def test_list_calendar_meetings_for_issue_parses_meta_json(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    meta = {"location": "Teams", "is_cancelled": False, "web_link": "https://x", "is_recurring": True}
    rid = ws_db.insert_raw_item(
        source="calendar", stable_key="c1", thread_key="c1", dedupe_key="dkc1",
        occurred_ts=100.0, subject="Weekly sync", from_actor="org@example.com",
        participants_json='["org@example.com","marc@example.com"]', is_organizer=0,
        meta_json=json.dumps(meta),
    )
    ws_db.link_raw_item_to_issue(rid, a)

    meetings = ws_db.list_calendar_meetings_for_issue(a)

    assert len(meetings) == 1
    m = meetings[0]
    assert m["subject"] == "Weekly sync"
    assert m["organizer"] == "org@example.com"
    assert m["is_organizer"] == 0
    assert m["participants"] == ["org@example.com", "marc@example.com"]
    assert m["location"] == "Teams"
    assert m["is_cancelled"] is False
    assert m["is_recurring"] is True


def test_list_calendar_meetings_for_issue_excludes_non_calendar_sources(ws_db):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="m1", thread_key="m1", dedupe_key="dkm1",
                                 occurred_ts=100.0, subject="mail", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, a)

    assert ws_db.list_calendar_meetings_for_issue(a) == []


def test_list_calendar_meetings_for_issue_handles_no_meta_json(ws_db):
    """A calendar raw_item ingested before E7 (meta_json column added but
    never backfilled for old rows) - must not crash, just have no extra
    fields beyond the base ones."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    rid = ws_db.insert_raw_item(source="calendar", stable_key="c1", thread_key="c1", dedupe_key="dkc1",
                                 occurred_ts=100.0, subject="Old event", from_actor="org@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, a)

    meetings = ws_db.list_calendar_meetings_for_issue(a)

    assert len(meetings) == 1
    assert meetings[0]["subject"] == "Old event"
    assert "location" not in meetings[0]
