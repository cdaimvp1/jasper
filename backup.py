"""
backup.py — deterministic, automated database snapshot system. No LLM calls.

Built 2026-07-29, directly in response to a real incident: the previous
backup habit was ad hoc, manually-named .db.bak-* files and a junk-drawer
folder (setup/_backups/) sitting INSIDE the body/ code repo, mixed in with
genuinely-disposable pre-git snapshots. That mixing is exactly how an
irreplaceable quarantined data copy ended up looking like disposable clutter
and got rm -rf'd along with the rest of the folder.

Two structural decisions fix that, not just paper over it:
  1. Snapshots live under paths.BACKUPS_DIR, which is inside DATA_DIR (the
     real data location), never inside the body/ code repo. A future "clean
     up the repo" pass has no path that reaches this directory at all.
  2. Every prune operation logs exactly what it deleted (see prune_snapshots)
     - no silent deletion, ever, matching the same discipline as retention.py.

Uses sqlite3's own backup API (Connection.backup()), not a raw file copy - a
raw copy of a live WAL-mode database mid-write can capture a torn/inconsistent
file; the backup API produces a real, consistent point-in-time copy regardless
of what the live server is doing concurrently.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import time
from pathlib import Path

import paths

# Retention shape: keep every DAILY snapshot for daily_keep days, then thin to
# one per week for weekly_keep weeks, then one per month for monthly_keep
# months, delete anything older/denser than that. Classic grandfather-father-
# son rotation. Values are read from config at call time (see run_nightly_
# snapshot) - these are just the fallback if config has nothing set yet.
DEFAULT_DAILY_KEEP = 14
DEFAULT_WEEKLY_KEEP = 8
DEFAULT_MONTHLY_KEEP = 12


def _timestamp_label(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H%M%S", time.localtime(now))


def snapshot_db(db_path: Path, dest_dir: Path, now: float | None = None) -> Path | None:
    """Consistent, gzip-compressed point-in-time copy of a SQLite DB file.
    Returns the snapshot path, or None if db_path doesn't exist yet (a fresh
    install with no data - not an error, just nothing to snapshot)."""
    if not db_path.is_file():
        return None
    if now is None:
        now = time.time()
    dest_dir.mkdir(parents=True, exist_ok=True)
    label = _timestamp_label(now)
    tmp_path = dest_dir / f"{db_path.stem}.{label}.db.tmp"
    final_path = dest_dir / f"{db_path.stem}.{label}.db.gz"

    src_conn = sqlite3.connect(str(db_path))
    dst_conn = sqlite3.connect(str(tmp_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    with open(tmp_path, "rb") as f_in, gzip.open(final_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    tmp_path.unlink()
    return final_path


def snapshot_config(config_dir: Path, dest_dir: Path, now: float | None = None) -> Path | None:
    """Copies every *.json in config_dir into a single timestamped subfolder
    under dest_dir. Config files are tiny (a few KB) - no compression needed,
    kept as plain readable JSON so a snapshot can be diffed/inspected directly
    without decompressing anything first."""
    json_files = list(config_dir.glob("*.json"))
    if not json_files:
        return None
    if now is None:
        now = time.time()
    label = _timestamp_label(now)
    snap_dir = dest_dir / label
    snap_dir.mkdir(parents=True, exist_ok=True)
    for f in json_files:
        shutil.copy2(f, snap_dir / f.name)
    return snap_dir


def _parse_snapshot_ts(path: Path) -> float | None:
    """Recovers the timestamp embedded in a snapshot's own filename/dirname
    (format from _timestamp_label) rather than trusting filesystem mtime,
    which a copy/move/restore could silently change."""
    name = path.stem if path.is_file() else path.name
    # strip a leading db-stem prefix like "workgraph." if present
    parts = name.split(".")
    for part in parts:
        try:
            return time.mktime(time.strptime(part, "%Y-%m-%dT%H%M%S"))
        except ValueError:
            continue
    return None


def prune_snapshots(entries: list[Path], *, daily_keep: int, weekly_keep: int,
                     monthly_keep: int, now: float | None = None) -> list[Path]:
    """Grandfather-father-son thinning over a flat list of snapshot paths
    (files or directories, as long as _parse_snapshot_ts can read a timestamp
    out of the name). Returns the list of paths that should be DELETED - this
    function only decides, callers are responsible for actually deleting (and
    logging what they deleted) so this stays pure/testable."""
    if now is None:
        now = time.time()
    DAY = 86400.0
    dated = [(p, ts) for p in entries if (ts := _parse_snapshot_ts(p)) is not None]
    dated.sort(key=lambda pt: pt[1], reverse=True)

    keep: set[Path] = set()
    daily_cutoff = now - daily_keep * DAY
    weekly_cutoff = daily_cutoff - weekly_keep * 7 * DAY
    monthly_cutoff = weekly_cutoff - monthly_keep * 30 * DAY

    seen_weeks: set[int] = set()
    seen_months: set[str] = set()
    for p, ts in dated:
        if ts >= daily_cutoff:
            keep.add(p)
        elif ts >= weekly_cutoff:
            week_num = int(ts // (7 * DAY))
            if week_num not in seen_weeks:
                seen_weeks.add(week_num)
                keep.add(p)
        elif ts >= monthly_cutoff:
            month_key = time.strftime("%Y-%m", time.localtime(ts))
            if month_key not in seen_months:
                seen_months.add(month_key)
                keep.add(p)
        # older than monthly_cutoff: never kept

    return [p for p, _ in dated if p not in keep]


def _delete_snapshot(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def run_nightly_snapshot(*, daily_keep: int = DEFAULT_DAILY_KEEP,
                          weekly_keep: int = DEFAULT_WEEKLY_KEEP,
                          monthly_keep: int = DEFAULT_MONTHLY_KEEP,
                          now: float | None = None) -> dict:
    """The one function scheduled_refresh.py calls. Snapshots workgraph.db,
    bus.db, and config/*.json, then prunes each snapshot set to the configured
    retention shape. Every deletion is returned in the result (never silent -
    same discipline as retention.py) so the caller can log it."""
    if now is None:
        now = time.time()
    paths.ensure_dirs()

    result: dict = {"snapshots_created": [], "snapshots_deleted": []}

    workgraph_snap = snapshot_db(paths.WORKGRAPH_DB, paths.DB_SNAPSHOTS_DIR / "workgraph", now=now)
    if workgraph_snap:
        result["snapshots_created"].append(str(workgraph_snap))
    bus_snap = snapshot_db(paths.BUS_DB, paths.DB_SNAPSHOTS_DIR / "bus", now=now)
    if bus_snap:
        result["snapshots_created"].append(str(bus_snap))
    config_snap = snapshot_config(paths.CONFIG_DIR, paths.CONFIG_SNAPSHOTS_DIR, now=now)
    if config_snap:
        result["snapshots_created"].append(str(config_snap))

    for snap_root in (paths.DB_SNAPSHOTS_DIR / "workgraph", paths.DB_SNAPSHOTS_DIR / "bus"):
        if not snap_root.is_dir():
            continue
        entries = list(snap_root.iterdir())
        to_delete = prune_snapshots(entries, daily_keep=daily_keep, weekly_keep=weekly_keep,
                                     monthly_keep=monthly_keep, now=now)
        for p in to_delete:
            _delete_snapshot(p)
            result["snapshots_deleted"].append(str(p))

    if paths.CONFIG_SNAPSHOTS_DIR.is_dir():
        entries = [p for p in paths.CONFIG_SNAPSHOTS_DIR.iterdir() if p.is_dir()]
        to_delete = prune_snapshots(entries, daily_keep=daily_keep, weekly_keep=weekly_keep,
                                     monthly_keep=monthly_keep, now=now)
        for p in to_delete:
            _delete_snapshot(p)
            result["snapshots_deleted"].append(str(p))

    return result


def create_labeled_snapshot(label: str) -> dict:
    """On-demand named snapshot - the replacement for the old ad hoc
    'let me save a copy before I do this risky thing' .db.bak-* habit.
    Goes through the exact same mechanism (and the same never-inside-the-
    code-repo location) as the automatic nightly snapshots, just outside
    the prune rotation (labeled snapshots are never auto-deleted - only the
    dated/unlabeled nightly ones are subject to the retention schedule)."""
    now = time.time()
    paths.ensure_dirs()
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
    dest_root = paths.BACKUPS_DIR / "labeled" / f"{_timestamp_label(now)}_{safe_label}"
    dest_root.mkdir(parents=True, exist_ok=True)
    created = []
    for db_path in (paths.WORKGRAPH_DB, paths.BUS_DB):
        snap = snapshot_db(db_path, dest_root, now=now)
        if snap:
            created.append(str(snap))
    return {"label": label, "dir": str(dest_root), "snapshots_created": created}
