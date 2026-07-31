"""Regression tests for remediate_calendar_series.py (step 7, meeting-
grouping/related-project identity design pass).

Real bug this remediates: before the step-3 thread_key fix, every real
occurrence of a recurring calendar series got its own thread_key (its own
event id), so a long-running series like "DROP-IN HOURS" fragmented into
7+ separate Issues. The step-3 fix only changes FUTURE ingestion - this
script finds and consolidates the already-created fragments.
"""
from __future__ import annotations

import time

import remediate_calendar_series as rcs
import workgraph_store as ws


def _calendar_item(ws_db, issue_id, subject, organizer, occurred_ts, key):
    rid = ws_db.insert_raw_item(
        source="calendar", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=occurred_ts, subject=subject, from_actor=organizer, participants_json="[]",
    )
    if issue_id:
        ws_db.link_raw_item_to_issue(rid, issue_id)
    return rid


def test_calendar_series_key_for_row_matches_across_case_variants():
    """The real DROP-IN HOURS subjects vary in casing ("DROP-IN HOURS" vs
    "Drop-IN Hours") - normalize_topic_key already handles this."""
    ts = 1785000000.0
    key1 = rcs._calendar_series_key_for_row("OPTIONAL: LEAH - SFA and EVAL Agreement - DROP-IN HOURS", "marcia.hakala@lilly.com", ts)
    key2 = rcs._calendar_series_key_for_row("OPTIONAL: LEAH - SFA and EVAL Agreement - Drop-IN Hours", "marcia.hakala@lilly.com", ts)
    assert key1 == key2


def test_calendar_series_key_for_row_distinguishes_different_organizers():
    ts = 1785000000.0
    key1 = rcs._calendar_series_key_for_row("Weekly Sync", "alice@example.com", ts)
    key2 = rcs._calendar_series_key_for_row("Weekly Sync", "bob@example.com", ts)
    assert key1 != key2


def test_find_consolidation_groups_finds_a_real_fragmented_series(ws_db):
    ts_base = time.mktime(time.strptime("2026-07-13 19:00:00", "%Y-%m-%d %H:%M:%S"))
    conn = ws_db._connect()
    issue_ids = []
    for i in range(3):
        iid = ws_db.create_issue_with_new_id(title=f"DROP-IN HOURS occurrence {i}", state="active", category="other")
        conn.execute("UPDATE issues SET opened_at = ? WHERE id = ?", (ts_base + i * 86400, iid))
        _calendar_item(ws_db, iid, "OPTIONAL: LEAH - DROP-IN HOURS", "marcia@lilly.com", ts_base + i * 86400, f"evt-{i}")
        issue_ids.append(iid)
    conn.close()

    groups = rcs.find_consolidation_groups()

    assert len(groups) == 1
    assert set(l["id"] for l in groups[0]["losers"]) | {groups[0]["winner"]["id"]} == set(issue_ids)
    assert groups[0]["winner"]["id"] == issue_ids[0]  # earliest-opened wins


def test_find_consolidation_groups_keeps_different_series_separate(ws_db):
    ts_base = time.time()
    a1 = ws_db.create_issue_with_new_id(title="DROP-IN HOURS occ 1", state="active", category="other")
    a2 = ws_db.create_issue_with_new_id(title="DROP-IN HOURS occ 2", state="active", category="other")
    b1 = ws_db.create_issue_with_new_id(title="AI Model Weekly occ 1", state="active", category="other")
    b2 = ws_db.create_issue_with_new_id(title="AI Model Weekly occ 2", state="active", category="other")
    _calendar_item(ws_db, a1, "DROP-IN HOURS", "marcia@lilly.com", ts_base, "evt-a1")
    _calendar_item(ws_db, a2, "DROP-IN HOURS", "marcia@lilly.com", ts_base + 86400, "evt-a2")
    _calendar_item(ws_db, b1, "Ai Model Weekly", "nikhil@leahai.com", ts_base + 3600, "evt-b1")
    _calendar_item(ws_db, b2, "Ai Model Weekly", "nikhil@leahai.com", ts_base + 3600 + 86400, "evt-b2")

    groups = rcs.find_consolidation_groups()

    assert len(groups) == 2
    all_grouped = {frozenset({g["winner"]["id"]} | {l["id"] for l in g["losers"]}) for g in groups}
    assert frozenset({a1, a2}) in all_grouped
    assert frozenset({b1, b2}) in all_grouped


def test_find_consolidation_groups_ignores_already_single_issue_series(ws_db):
    """A series with only ONE real occurrence-issue needs no consolidation -
    it was already correctly a single Issue."""
    iid = ws_db.create_issue_with_new_id(title="One-off calendar item", state="active", category="other")
    _calendar_item(ws_db, iid, "One-off calendar item", "marcia@lilly.com", time.time(), "evt-solo")

    groups = rcs.find_consolidation_groups()

    assert groups == []


def test_find_consolidation_groups_ignores_unlinked_raw_items(ws_db):
    """Calendar raw_items with no issue_id at all (not yet linked) don't
    contribute a "distinct issue" to any group."""
    iid = ws_db.create_issue_with_new_id(title="DROP-IN HOURS", state="active", category="other")
    _calendar_item(ws_db, iid, "DROP-IN HOURS", "marcia@lilly.com", time.time(), "evt-1")
    _calendar_item(ws_db, None, "DROP-IN HOURS", "marcia@lilly.com", time.time() + 86400, "evt-2")

    groups = rcs.find_consolidation_groups()

    assert groups == []


def test_execute_group_consolidates_via_remediate_merge_issue_identity(ws_db):
    winner = ws_db.create_issue_with_new_id(title="Winner", state="active", category="other")
    loser = ws_db.create_issue_with_new_id(title="Loser", state="active", category="other")
    rid = ws_db.insert_raw_item(source="calendar", stable_key="evt-x", thread_key="evt-x", dedupe_key="dkx",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, loser)

    group = {"series_key": "test", "winner": ws_db.get_issue(winner), "losers": [ws_db.get_issue(loser)]}
    result = rcs.execute_group(group)

    assert result["losers_merged"] == [loser]
    assert result["errors"] == []
    assert ws_db.get_raw_item(rid)["issue_id"] == winner
    assert ws_db.get_issue(loser)["state"] == "noise-archived"


def test_main_dry_run_writes_nothing(ws_db, monkeypatch, capsys):
    """The safety default - running with no args must never write."""
    a = ws_db.create_issue_with_new_id(title="DROP-IN HOURS occ 1", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="DROP-IN HOURS occ 2", state="active", category="other")
    _calendar_item(ws_db, a, "DROP-IN HOURS", "marcia@lilly.com", time.time(), "evt-1")
    _calendar_item(ws_db, b, "DROP-IN HOURS", "marcia@lilly.com", time.time() + 86400, "evt-2")

    monkeypatch.setattr("sys.argv", ["remediate_calendar_series.py"])
    rcs.main()

    captured = capsys.readouterr()
    assert "Found 1 consolidation group" in captured.out
    assert "nothing written" in captured.out.lower()
    assert ws_db.get_issue(a)["state"] == "active"
    assert ws_db.get_issue(b)["state"] == "active"
