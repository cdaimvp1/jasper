"""Regression tests for backup.py - ported from this session's ad hoc
verification of the new backup/retention system."""
import gzip
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

import backup


def test_snapshot_db_produces_valid_restorable_backup(tmp_path):
    db_path = tmp_path / "test.db"
    dest_dir = tmp_path / "snaps"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO t (val) VALUES (?)", ("hello world",))
    conn.commit()
    conn.close()

    snap_path = backup.snapshot_db(db_path, dest_dir)
    assert snap_path is not None and snap_path.is_file()

    restored_path = tmp_path / "restored.db"
    with gzip.open(snap_path, "rb") as f_in, open(restored_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    rconn = sqlite3.connect(str(restored_path))
    rows = rconn.execute("SELECT val FROM t").fetchall()
    rconn.close()
    assert rows == [("hello world",)]


def test_snapshot_nonexistent_db_returns_none(tmp_path):
    assert backup.snapshot_db(tmp_path / "missing.db", tmp_path / "snaps") is None


def test_snapshot_consistent_while_source_connection_open_in_wal(tmp_path):
    """The whole reason to use sqlite3's backup API instead of a raw file
    copy: a raw copy mid-write on a live WAL-mode DB can capture a torn file."""
    db_path = tmp_path / "live.db"
    dest_dir = tmp_path / "snaps"
    live_conn = sqlite3.connect(str(db_path))
    live_conn.execute("PRAGMA journal_mode=WAL")
    live_conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(100):
        live_conn.execute("INSERT INTO t (val) VALUES (?)", (f"row{i}",))
    live_conn.commit()

    snap_path = backup.snapshot_db(db_path, dest_dir)  # source connection still OPEN
    live_conn.close()

    restored_path = tmp_path / "restored.db"
    with gzip.open(snap_path, "rb") as f_in, open(restored_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    rconn = sqlite3.connect(str(restored_path))
    count = rconn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    rconn.close()
    assert count == 100


def test_prune_snapshots_grandfather_father_son(tmp_path):
    now = time.time()
    DAY = 86400.0
    entries = []
    for days_ago in range(400):
        ts = now - days_ago * DAY
        label = time.strftime("%Y-%m-%dT%H%M%S", time.localtime(ts))
        entries.append(Path(f"workgraph.{label}.db.gz"))

    to_delete = backup.prune_snapshots(entries, daily_keep=14, weekly_keep=8, monthly_keep=12, now=now)
    kept = [e for e in entries if e not in to_delete]

    assert set(entries[:14]).issubset(set(kept)), "all 14 most-recent daily snapshots must survive"
    assert 14 <= len(kept) <= 14 + 8 + 12 + 2

    # idempotence: pruning an already-pruned set deletes nothing further
    to_delete_2 = backup.prune_snapshots(kept, daily_keep=14, weekly_keep=8, monthly_keep=12, now=now)
    assert to_delete_2 == []


def test_run_nightly_snapshot_end_to_end(isolated_paths):
    import json
    isolated_paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(isolated_paths.WORKGRAPH_DB))
    conn.execute("CREATE TABLE issues (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO issues VALUES ('marc-001')")
    conn.commit()
    conn.close()
    conn2 = sqlite3.connect(str(isolated_paths.BUS_DB))
    conn2.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
    conn2.commit()
    conn2.close()
    (isolated_paths.CONFIG_DIR / "settings.json").write_text(
        json.dumps({"manager": {"id": "Marc Lane"}}), encoding="utf-8")

    result = backup.run_nightly_snapshot()
    assert len(result["snapshots_created"]) == 3
    assert result["snapshots_deleted"] == []


def test_create_labeled_snapshot_survives_prune_rotation(isolated_paths):
    conn = sqlite3.connect(str(isolated_paths.WORKGRAPH_DB))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    labeled = backup.create_labeled_snapshot("pre-risky-change")
    assert Path(labeled["dir"]).is_dir()

    backup.run_nightly_snapshot()  # a normal automated pass runs its prune rotation
    assert Path(labeled["dir"]).is_dir(), "labeled snapshots must never be swept up by the automatic prune"
