"""Regression tests for retention.py - ported from this session's ad hoc
verification of the new backup/retention system."""
import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def retention_env(isolated_paths, monkeypatch):
    import bus, workgraph_store as ws, config, retention
    monkeypatch.setattr(ws, "WORKGRAPH_DB", isolated_paths.WORKGRAPH_DB)
    monkeypatch.setattr(bus, "BUS_DB", isolated_paths.BUS_DB)
    monkeypatch.setattr(config, "SETTINGS_PATH", isolated_paths.CONFIG_DIR / "settings.json")
    monkeypatch.setattr(config, "_cache", {})
    monkeypatch.setattr(config, "_cache_mtime", 0.0)
    ws.init_workgraph()
    bus.init_bus()
    return retention


def test_report_only_mode_computes_counts_but_deletes_nothing(retention_env):
    import bus
    now = time.time()
    very_old_ts = now - 300 * 86400
    old_ts = now - 100 * 86400
    recent_ts = now - 5 * 86400

    conn = bus._connect()
    conn.execute("INSERT INTO events (ts, source, kind, actor, target, payload) VALUES (?,?,?,?,?,?)",
                 (very_old_ts, "test", "k", "a", "t", "{}"))
    conn.execute("INSERT INTO events (ts, source, kind, actor, target, payload) VALUES (?,?,?,?,?,?)",
                 (recent_ts, "test", "k", "a", "t", "{}"))
    conn.execute("INSERT INTO worker_notifications (ts, recipient, kind, source, summary, event_id, payload_ref) VALUES (?,?,?,?,?,?,?)",
                 (old_ts, "tia", "k", "s", "sum", None, None))
    conn.commit()
    conn.close()

    report = retention_env.run(now=now)
    assert report["enforcement_enabled"] is False
    assert report["categories"]["bus_events"]["would_affect"] == 1
    assert report["categories"]["bus_events"]["deleted"] == 0
    assert report["categories"]["bus_worker_notifications"]["would_affect"] == 1
    assert report["categories"]["bus_worker_notifications"]["deleted"] == 0

    conn = bus._connect()
    n_events = conn.execute("SELECT COUNT(*) as n FROM events").fetchone()["n"]
    conn.close()
    assert n_events == 2, "report-only mode must never actually delete anything"


def test_enforcement_mode_deletes_old_rows_archives_events(retention_env):
    import bus, config
    now = time.time()
    very_old_ts = now - 300 * 86400
    recent_ts = now - 5 * 86400

    conn = bus._connect()
    conn.execute("INSERT INTO events (ts, source, kind, actor, target, payload) VALUES (?,?,?,?,?,?)",
                 (very_old_ts, "test", "k", "a", "t", "{}"))
    conn.execute("INSERT INTO events (ts, source, kind, actor, target, payload) VALUES (?,?,?,?,?,?)",
                 (recent_ts, "test", "k", "a", "t", "{}"))
    conn.commit()
    conn.close()

    config.set_value(True, "retention", "enforcement_enabled")
    report = retention_env.run(now=now)
    assert report["enforcement_enabled"] is True
    assert report["categories"]["bus_events"]["deleted"] == 1
    assert report["categories"]["bus_events"]["archived"] == 1
    archive_path = Path(report["categories"]["bus_events"]["archive_path"])
    assert archive_path.is_file()
    assert "test" in archive_path.read_text(encoding="utf-8")

    conn = bus._connect()
    remaining = conn.execute("SELECT ts FROM events").fetchall()
    conn.close()
    assert len(remaining) == 1 and abs(remaining[0]["ts"] - recent_ts) < 1


def test_raw_ingest_failed_never_deleted_but_alerts(retention_env, isolated_paths):
    import config
    failed_dir = isolated_paths.DATA_DIR / "raw_ingest_failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    (failed_dir / "broken_drop.json").write_text("{}", encoding="utf-8")

    config.set_value(True, "retention", "enforcement_enabled")
    report = retention_env.run()

    assert (failed_dir / "broken_drop.json").exists(), "raw_ingest_failed must NEVER auto-delete"
    assert report["categories"]["raw_ingest_failed"]["alert"] is True
    assert report["categories"]["raw_ingest_failed"]["count"] == 1


def test_log_rotation_deletes_only_old_files(retention_env, isolated_paths):
    import config
    now = time.time()
    old_mtime = now - 100 * 86400

    log_path = isolated_paths.DATA_DIR / "scheduled_refresh.log"
    log_path.write_text("old", encoding="utf-8")
    os.utime(log_path, (old_mtime, old_mtime))

    recent_log = isolated_paths.DATA_DIR / "cockpit_server_task.log"
    recent_log.write_text("recent", encoding="utf-8")

    config.set_value(True, "retention", "enforcement_enabled")
    retention_env.run(now=now)

    assert not log_path.exists()
    assert recent_log.exists()


def test_daily_gate_skips_same_day_runs_once_next_day(retention_env):
    import workgraph_store as ws
    DAY = 86400.0
    struct = time.localtime()
    now = time.mktime((struct.tm_year, struct.tm_mon, struct.tm_mday, 12, 0, 0, 0, 0, -1))

    r1 = retention_env.run_daily_if_due(now=now)
    assert r1 is not None

    r2 = retention_env.run_daily_if_due(now=now + 3600)
    assert r2 is None

    r3 = retention_env.run_daily_if_due(now=now + 6 * 3600)
    assert r3 is None

    r4 = retention_env.run_daily_if_due(now=now + DAY)
    assert r4 is not None
