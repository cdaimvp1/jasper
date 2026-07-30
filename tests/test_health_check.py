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


def test_daily_gate_runs_once_per_day(ws_db):
    struct = time.localtime()
    now = time.mktime((struct.tm_year, struct.tm_mon, struct.tm_mday, 12, 0, 0, 0, 0, -1))
    r1 = hc.run_daily_if_due(now=now)
    assert r1 is not None
    r2 = hc.run_daily_if_due(now=now + 3600)
    assert r2 is None
    r3 = hc.run_daily_if_due(now=now + 86400)
    assert r3 is not None
