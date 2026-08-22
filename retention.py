"""
retention.py — deterministic, config-driven retention policy enforcement. No
LLM calls, no destructive path that isn't explicitly configured and enabled.

Built 2026-07-29 alongside backup.py, directly motivated by the same incident:
a real, irreplaceable data loss caused by a destructive action taken without
re-verifying scope immediately before acting. The core safety principle here
is the mirror image of that lesson: `enforcement_enabled` defaults to False,
so this module runs in REPORT-ONLY mode (computes and logs what it WOULD do)
until a human explicitly turns it on in Settings — a wrong number in a config
field can't silently delete anything on day one. Every category that CAN
delete logs exactly what it deleted; nothing here is ever silent.

Policy shape (read from config.get("retention", ...) — see DEFAULT_POLICY
below for the full schema and every category's reasoning):
  never_delete / keep_forever  — this module contains no code path that can
                                  ever delete these categories, regardless of
                                  config. Not a config guard — structurally
                                  absent.
  delete_after_days            — rows/files older than N days are deleted
                                  (only when enforcement_enabled).
  archive_after_days           — rows older than N days are appended to a
                                  JSONL archive file, THEN deleted from the
                                  live table (only when enforcement_enabled).
                                  Never a bare delete-and-forget.
  rotate                       — log files older than keep_days are deleted
                                  (only when enforcement_enabled).
  never_auto_delete + alert    — never deleted by this module under any
                                  config; if non-empty, reported as a signal
                                  something upstream is failing, not aged out.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import bus
import config
import paths
import workgraph_store as ws
import backup

DAY = 86400.0

DEFAULT_POLICY = {
    "enforcement_enabled": False,
    "bus_worker_notifications": {"policy": "delete_after_days", "days": 60},
    "bus_events": {"policy": "archive_after_days", "days": 270},
    "socrates_retrieval_log": {"policy": "delete_after_days", "days": 90},
    "logs": {"policy": "rotate", "keep_days": 60},
    "raw_ingest_processed": {"policy": "delete_after_days", "days": 730},
}

_LOG_FILES = ["scheduled_refresh.log", "cockpit_server_task.log", "cockpit_server_task_err.log"]


def _policy_for(category: str) -> dict:
    configured = config.get("retention", category)
    if isinstance(configured, dict) and "policy" in configured:
        return configured
    return DEFAULT_POLICY[category]


def _archive_events_jsonl(rows: list[dict], now: float) -> Path:
    """Appends deleted event rows to a dated JSONL archive file BEFORE they're
    removed from the live table — 'archive' means the data still exists
    somewhere readable, not a fancier name for delete. Lives under
    paths.BACKUPS_DIR, same never-inside-the-code-repo placement as DB
    snapshots."""
    archive_dir = paths.BACKUPS_DIR / "event_archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    label = time.strftime("%Y-%m", time.localtime(now))
    archive_path = archive_dir / f"events.{label}.jsonl"
    with open(archive_path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    return archive_path


def _handle_bus_worker_notifications(enforcement_enabled: bool, now: float) -> dict:
    policy = _policy_for("bus_worker_notifications")
    cutoff = now - policy["days"] * DAY
    would_affect = bus.count_worker_notifications_before(cutoff)
    result = {"policy": policy, "would_affect": would_affect, "deleted": 0}
    if enforcement_enabled and would_affect:
        result["deleted"] = bus.delete_old_worker_notifications(cutoff)
    return result


def _handle_bus_events(enforcement_enabled: bool, now: float) -> dict:
    policy = _policy_for("bus_events")
    cutoff = now - policy["days"] * DAY
    old_rows = bus.get_events_before(cutoff)
    result = {"policy": policy, "would_affect": len(old_rows), "archived": 0, "deleted": 0}
    if enforcement_enabled and old_rows:
        archive_path = _archive_events_jsonl(old_rows, now)
        result["archived"] = len(old_rows)
        result["archive_path"] = str(archive_path)
        result["deleted"] = bus.delete_old_events(cutoff)
    return result


def _handle_socrates_log(enforcement_enabled: bool, now: float) -> dict:
    policy = _policy_for("socrates_retrieval_log")
    cutoff = now - policy["days"] * DAY
    would_affect = ws.count_socrates_log_before(cutoff)
    result = {"policy": policy, "would_affect": would_affect, "deleted": 0}
    if enforcement_enabled and would_affect:
        result["deleted"] = ws.delete_old_socrates_log(cutoff)
    return result


def _handle_logs(enforcement_enabled: bool, now: float) -> dict:
    policy = _policy_for("logs")
    cutoff = now - policy["keep_days"] * DAY
    candidates = []
    for name in _LOG_FILES:
        p = paths.DATA_DIR / name
        if p.is_file() and p.stat().st_mtime < cutoff:
            candidates.append(p)
    result = {"policy": policy, "would_affect": len(candidates), "deleted": 0, "deleted_files": []}
    if enforcement_enabled:
        for p in candidates:
            p.unlink(missing_ok=True)
            result["deleted"] += 1
            result["deleted_files"].append(str(p))
    return result


def _handle_raw_ingest_processed(enforcement_enabled: bool, now: float) -> dict:
    policy = _policy_for("raw_ingest_processed")
    cutoff = now - policy["days"] * DAY
    processed_dir = paths.DATA_DIR / "raw_ingest_processed"
    candidates = []
    if processed_dir.is_dir():
        candidates = [p for p in processed_dir.iterdir() if p.is_file() and p.stat().st_mtime < cutoff]
    result = {"policy": policy, "would_affect": len(candidates), "deleted": 0}
    if enforcement_enabled:
        for p in candidates:
            p.unlink(missing_ok=True)
            result["deleted"] += 1
    return result


def _handle_raw_ingest_failed() -> dict:
    """Never deletes anything, under any config — a non-empty failed/ dir is a
    signal something upstream is broken, not something to age out quietly."""
    failed_dir = paths.DATA_DIR / "raw_ingest_failed"
    count = len(list(failed_dir.iterdir())) if failed_dir.is_dir() else 0
    result = {"policy": {"policy": "never_auto_delete"}, "count": count, "alert": count > 0}
    return result


def run(now: float | None = None) -> dict:
    """The one function scheduled_refresh.py calls, once per day. Returns a
    full report of every category — what its policy is, what would be/was
    affected — regardless of enforcement_enabled, so the report is meaningful
    even in dry-run mode. Never raises: one category's failure (e.g. a
    corrupt log file) shouldn't block every other category's check."""
    if now is None:
        now = time.time()
    enforcement_enabled = bool(config.get("retention", "enforcement_enabled"))

    report: dict = {"enforcement_enabled": enforcement_enabled, "as_of": now, "categories": {}}
    handlers = {
        "bus_worker_notifications": lambda: _handle_bus_worker_notifications(enforcement_enabled, now),
        "bus_events": lambda: _handle_bus_events(enforcement_enabled, now),
        "socrates_retrieval_log": lambda: _handle_socrates_log(enforcement_enabled, now),
        "logs": lambda: _handle_logs(enforcement_enabled, now),
        "raw_ingest_processed": lambda: _handle_raw_ingest_processed(enforcement_enabled, now),
        "raw_ingest_failed": _handle_raw_ingest_failed,
    }
    for name, handler in handlers.items():
        try:
            report["categories"][name] = handler()
        except Exception as e:
            print(f"[retention] category {name!r} failed: {e!r}", file=sys.stderr)
            report["categories"][name] = {"error": repr(e)}
    return report


def _dir_size_bytes(d: Path) -> int:
    if not d.is_dir():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def disk_usage_report() -> dict:
    """Closes the visibility gap flagged this session: no view into total
    data-directory size/growth existed anywhere. Cheap (a handful of stat
    calls plus one recursive walk over documents/), safe to call on every
    Settings page load."""
    return {
        "workgraph_db_bytes": paths.WORKGRAPH_DB.stat().st_size if paths.WORKGRAPH_DB.is_file() else 0,
        "bus_db_bytes": paths.BUS_DB.stat().st_size if paths.BUS_DB.is_file() else 0,
        "documents_bytes": _dir_size_bytes(paths.DOCUMENTS_DIR),
        "backups_bytes": _dir_size_bytes(paths.BACKUPS_DIR),
        "logs_bytes": sum(
            (paths.DATA_DIR / name).stat().st_size
            for name in _LOG_FILES if (paths.DATA_DIR / name).is_file()
        ),
    }


def run_daily_if_due(now: float | None = None) -> dict | None:
    """Gate for scheduled_refresh.py's 5x/day cycle: retention + snapshotting
    only need to run once a day, not once per refresh. Uses the same
    ingest_cursors mechanism the mail/Teams/Calendar cursors already use
    (source='retention', cursor_key='last_run_date') rather than inventing a
    second persistence mechanism for one boolean. Returns None (a real,
    checkable 'did not run' signal) on every call that isn't the day's first,
    so a caller can log the skip explicitly instead of it being silently
    absent."""
    if now is None:
        now = time.time()
    if not ws.due_for_daily_run("retention", now):
        return None
    snapshot_cfg = config.get("retention", "db_snapshots") or {}
    snapshot_result = backup.run_nightly_snapshot(
        daily_keep=snapshot_cfg.get("daily_keep", backup.DEFAULT_DAILY_KEEP),
        weekly_keep=snapshot_cfg.get("weekly_keep", backup.DEFAULT_WEEKLY_KEEP),
        monthly_keep=snapshot_cfg.get("monthly_keep", backup.DEFAULT_MONTHLY_KEEP),
        now=now,
    )
    retention_result = run(now=now)
    return {"snapshot": snapshot_result, "retention": retention_result}


if __name__ == "__main__":
    ws.init_workgraph()
    bus.init_bus()
    print(json.dumps(run(), indent=2, default=str))
