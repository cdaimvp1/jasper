"""Regression tests for health_check.py (task #41) - deterministic daily
health-check script, no LLM involved. Ported from real ad hoc verification,
including a real bug found while testing against production: teams_chat's
cursor is stored as an ISO8601 string (Microsoft Graph's own
lastUpdatedDateTime format), not a Unix epoch like the other three cursors -
a bare float() parse silently skipped it entirely instead of checking it."""
import time
import unittest.mock as mock

import pytest

import health_check as hc


def test_parse_cursor_ts_handles_unix_epoch():
    now = time.time()
    ts = hc._parse_cursor_ts(str(now - 3600))
    assert abs((now - ts) - 3600) < 1


def test_parse_cursor_ts_handles_iso8601_with_z_suffix():
    """The real teams_chat cursor format - confirmed via production testing."""
    now = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 100 * 3600))
    ts = hc._parse_cursor_ts(iso)
    assert abs((now - ts) - 100 * 3600) < 5


def test_parse_cursor_ts_returns_none_for_garbage():
    assert hc._parse_cursor_ts("not-a-timestamp") is None
    assert hc._parse_cursor_ts(None) is None


def test_stale_iso_cursor_is_detected_not_silently_skipped(ws_db):
    now = time.time()
    stale_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 100 * 3600))
    ws_db.set_cursor("teams_chat", "default", stale_iso)
    result = hc.check_cursors_advancing(now)
    assert result["ok"] is False
    assert result["unparseable_cursors"] == []
    assert any(s["source"] == "teams_chat" for s in result["stale_cursors"])


def test_fresh_cursor_is_ok(ws_db):
    now = time.time()
    ws_db.set_cursor("outlook_mail", "folder:Careful", str(now - 3600))
    result = hc.check_cursors_advancing(now)
    assert result["ok"] is True


def test_disk_growth_flags_shrink_and_explosion(ws_db):
    now = time.time()
    ws_db.set_cursor("health_check", "last_snapshot",
                      '{"workgraph_db_bytes": 1000, "bus_db_bytes": 1000, "documents_bytes": 1000, "backups_bytes": 1000, "logs_bytes": 1000}')
    with mock.patch("retention.disk_usage_report", return_value={
        "workgraph_db_bytes": 100, "bus_db_bytes": 1000, "documents_bytes": 1000,
        "backups_bytes": 1000, "logs_bytes": 1000,
    }):
        result = hc.check_disk_growth_sane(now)
    assert result["ok"] is False
    assert result["findings"][0]["metric"] == "workgraph_db_bytes"


def test_disk_growth_no_prior_snapshot_is_ok(ws_db):
    result = hc.check_disk_growth_sane(time.time())
    assert result["ok"] is True


def test_scheduled_refresh_staleness(isolated_paths):
    now = time.time()
    log_path = isolated_paths.DATA_DIR / "scheduled_refresh.log"
    old_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 40 * 3600))
    log_path.write_text(f"{old_ts} UTC REFRESH ok\n", encoding="utf-8")
    result = hc.check_scheduled_refresh_ran(now)
    assert result["ok"] is False

    fresh_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 3600))
    log_path.write_text(f"{fresh_ts} UTC REFRESH ok\n", encoding="utf-8")
    result2 = hc.check_scheduled_refresh_ran(now)
    assert result2["ok"] is True


def test_backup_recency(isolated_paths):
    import os
    now = time.time()
    snap_dir = isolated_paths.DB_SNAPSHOTS_DIR / "workgraph"
    snap_dir.mkdir(parents=True, exist_ok=True)
    old_snap = snap_dir / "workgraph.old.db.gz"
    old_snap.write_bytes(b"x")
    os.utime(old_snap, (now - 50 * 3600, now - 50 * 3600))
    result = hc.check_backup_recent(now)
    assert result["ok"] is False

    new_snap = snap_dir / "workgraph.new.db.gz"
    new_snap.write_bytes(b"x")
    result2 = hc.check_backup_recent(now)
    assert result2["ok"] is True


def test_raw_ingest_failed_flags_nonempty(isolated_paths):
    failed_dir = isolated_paths.DATA_DIR / "raw_ingest_failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    (failed_dir / "x.json").write_text("{}", encoding="utf-8")
    result = hc.check_raw_ingest_failed()
    assert result["ok"] is False
    assert result["count"] == 1


def test_process_count_no_false_alarm_on_first_run(ws_db):
    """Confirmed via real testing against this machine: a healthy cohort
    legitimately runs 11+ claude.exe processes. A fixed absolute threshold
    would false-alarm every day - must compare day-over-day instead."""
    with mock.patch.object(hc, "_count_claude_processes", return_value=11):
        result = hc.check_claude_process_count(time.time())
    assert result["ok"] is True


def test_process_count_flags_real_doubling(ws_db):
    now = time.time()
    with mock.patch.object(hc, "_count_claude_processes", return_value=11):
        hc.check_claude_process_count(now)  # establishes yesterday's baseline
    with mock.patch.object(hc, "_count_claude_processes", return_value=30):
        result = hc.check_claude_process_count(now)
    assert result["ok"] is False


# --- check_classify_link_progressing (task #30, 2026-08-01) --------------

def test_classify_link_ok_when_no_unlinked_items(ws_db):
    result = hc.check_classify_link_progressing(time.time())
    assert result["ok"] is True


def test_classify_link_ok_when_never_checked_item_is_fresh(ws_db):
    now = time.time()
    ws_db.insert_raw_item(source="outlook_mail", stable_key="hc1", thread_key="hc1", dedupe_key="hc1",
                           occurred_ts=now - 3600, subject="fresh", from_actor="a@example.com", participants_json="[]")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET classified = 1 WHERE stable_key = 'hc1'")
    conn.close()

    result = hc.check_classify_link_progressing(now)

    assert result["ok"] is True


def test_classify_link_flags_a_stale_never_checked_item(ws_db):
    """Exact real incident reproduction: a classified, unlinked item that
    has NEVER been examined by cluster_and_link() at all (last_link_check_ts
    IS NULL), old enough that today's never-checked-first ordering (task
    #25) should have reached it by now - meaning cluster_and_link() has
    stopped running entirely, not a normal backlog effect."""
    now = time.time()
    ws_db.insert_raw_item(source="outlook_mail", stable_key="hc2", thread_key="hc2", dedupe_key="hc2",
                           occurred_ts=now - (hc.STALE_LINK_HOURS + 5) * 3600,
                           subject="stuck", from_actor="a@example.com", participants_json="[]")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET classified = 1 WHERE stable_key = 'hc2'")
    conn.close()

    result = hc.check_classify_link_progressing(now)

    assert result["ok"] is False
    assert result["oldest_unlinked_age_hours"] > hc.STALE_LINK_HOURS


def test_classify_link_ignores_items_already_examined_and_skipped(ws_db):
    """An item that HAS been checked (stamped by cluster_and_link, even if
    it resulted in a skip) is not this check's concern, no matter how old -
    that's the normal, expected permanent-skip backlog, not a stall."""
    now = time.time()
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="hc3", thread_key="hc3", dedupe_key="hc3",
                                 occurred_ts=now - (hc.STALE_LINK_HOURS + 100) * 3600,
                                 subject="old skip", from_actor="a@example.com", participants_json="[]")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET classified = 1 WHERE id = ?", (rid,))
    conn.close()
    ws_db.mark_link_checked(rid, now - 3600)

    result = hc.check_classify_link_progressing(now)

    assert result["ok"] is True


# --- check_suggestion_queue_not_unboundedly_growing (task #31) -----------

def _pending_suggestion(ws_db, a, b, created_ts=None):
    sugg_id = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="link")
    if created_ts is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE pending_project_suggestions SET created_ts = ? WHERE id = ?", (created_ts, sugg_id))
        conn.close()
    return sugg_id


def test_suggestion_queue_no_false_alarm_on_first_run(ws_db):
    """Same reasoning as claude_process_count - a real, large baseline is
    not itself a problem; only comparing against yesterday tells us
    anything. First run has nothing to compare against yet."""
    for i in range(50):
        _pending_suggestion(ws_db, f"issue-a{i}", f"issue-b{i}")
    result = hc.check_suggestion_queue_not_unboundedly_growing(time.time())
    assert result["ok"] is True
    assert result["count"] == 50


def test_suggestion_queue_flags_real_doubling(ws_db):
    now = time.time()
    for i in range(10):
        _pending_suggestion(ws_db, f"issue-c{i}", f"issue-d{i}")
    hc.check_suggestion_queue_not_unboundedly_growing(now)  # establishes yesterday's baseline (10)

    for i in range(30):
        _pending_suggestion(ws_db, f"issue-e{i}", f"issue-f{i}")
    result = hc.check_suggestion_queue_not_unboundedly_growing(now)

    assert result["count"] == 40
    assert result["ok"] is False


def test_suggestion_queue_ok_on_normal_growth(ws_db):
    now = time.time()
    for i in range(10):
        _pending_suggestion(ws_db, f"issue-g{i}", f"issue-h{i}")
    hc.check_suggestion_queue_not_unboundedly_growing(now)

    _pending_suggestion(ws_db, "issue-i1", "issue-j1")  # +1, nowhere near doubling
    result = hc.check_suggestion_queue_not_unboundedly_growing(now)

    assert result["ok"] is True


def test_suggestion_queue_reports_oldest_pending_age(ws_db):
    now = time.time()
    _pending_suggestion(ws_db, "issue-k1", "issue-k2", created_ts=now - 5 * 3600)
    _pending_suggestion(ws_db, "issue-k3", "issue-k4", created_ts=now - 50 * 3600)

    result = hc.check_suggestion_queue_not_unboundedly_growing(now)

    assert result["oldest_pending_age_hours"] == pytest.approx(50.0, abs=0.1)


def test_suggestion_queue_empty_is_ok_with_no_age(ws_db):
    result = hc.check_suggestion_queue_not_unboundedly_growing(time.time())
    assert result["ok"] is True
    assert result["count"] == 0
    assert result["oldest_pending_age_hours"] is None


# --- check_outlook_cache_freshness (task #149) ----------------------------

def test_outlook_cache_freshness_ok_with_no_prior_run(ws_db):
    result = hc.check_outlook_cache_freshness()
    assert result["ok"] is True
    assert result["detail"] == "no ingestion run recorded yet"


def test_outlook_cache_freshness_ok_on_a_single_cold_start(ws_db):
    ws_db.set_cursor("outlook_mail", "last_scan_outlook_cold_started", "true")
    ws_db.set_cursor("outlook_mail", "consecutive_cold_starts", "1")

    result = hc.check_outlook_cache_freshness()

    assert result["ok"] is True
    assert result["last_scan_cold_started"] is True
    assert result["consecutive_cold_starts"] == 1


def test_outlook_cache_freshness_flags_a_real_streak(ws_db):
    ws_db.set_cursor("outlook_mail", "last_scan_outlook_cold_started", "true")
    ws_db.set_cursor("outlook_mail", "consecutive_cold_starts", "3")

    result = hc.check_outlook_cache_freshness()

    assert result["ok"] is False
    assert result["consecutive_cold_starts"] == 3


def test_outlook_cache_freshness_ok_when_already_running(ws_db):
    ws_db.set_cursor("outlook_mail", "last_scan_outlook_cold_started", "false")
    ws_db.set_cursor("outlook_mail", "consecutive_cold_starts", "0")

    result = hc.check_outlook_cache_freshness()

    assert result["ok"] is True
    assert result["last_scan_cold_started"] is False


def test_daily_gate_runs_once_per_day(ws_db):
    struct = time.localtime()
    now = time.mktime((struct.tm_year, struct.tm_mon, struct.tm_mday, 12, 0, 0, 0, 0, -1))
    r1 = hc.run_daily_if_due(now=now)
    assert r1 is not None
    r2 = hc.run_daily_if_due(now=now + 3600)
    assert r2 is None
    r3 = hc.run_daily_if_due(now=now + 86400)
    assert r3 is not None


# --- task #74: get_last_result / persistence -----------------------------

def test_get_last_result_none_when_never_run(ws_db):
    assert hc.get_last_result() is None


def test_get_last_result_reflects_the_run_that_actually_happened(ws_db):
    now = time.time()
    ran = hc.run_daily_if_due(now=now)

    stored = hc.get_last_result()

    assert stored is not None
    assert stored["ok"] == ran["ok"]
    assert stored["as_of"] == ran["as_of"]
    assert set(stored["checks"].keys()) == set(ran["checks"].keys())


def test_get_last_result_does_not_change_when_gated(ws_db):
    struct = time.localtime()
    now = time.mktime((struct.tm_year, struct.tm_mon, struct.tm_mday, 12, 0, 0, 0, 0, -1))
    hc.run_daily_if_due(now=now)
    first = hc.get_last_result()

    skipped = hc.run_daily_if_due(now=now + 3600)  # same day - gated, must not overwrite

    assert skipped is None
    assert hc.get_last_result() == first


def test_get_last_result_survives_malformed_stored_value(ws_db):
    ws_db.set_cursor("health_check", "last_result", "not valid json")
    assert hc.get_last_result() is None
