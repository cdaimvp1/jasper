"""
health_check.py — deterministic daily health-check script. No LLM calls, no
judgment calls, just concrete invariants checked against concrete numbers.

This is "layer 1" of the sentinel design discussed with Marc: a bounded,
falsifiable, zero-token-cost check that runs once a day (same gating pattern
as retention.py) and reports findings for review. It does NOT try to catch
"anything that might be wrong" - only the specific things listed below, each
with a concrete, testable pass/fail condition. Layer 2 (an LLM-based sentinel
with a bounded token cap, comparing claimed-vs-actual and checking against
the documented "scar" incident library) is separate, later work.

Checks:
  1. ingest cursors actually advancing (not stuck > STALE_CURSOR_HOURS)
  2. DB/attachments size trend sane (no abnormal shrink or explosive growth
     vs yesterday's snapshot)
  3. scheduled_refresh.py actually ran recently (log file freshness)
  4. the daily backup snapshot actually ran recently
  5. raw_ingest_failed non-empty (signal something upstream is broken)
  6. no runaway count of claude.exe processes (coarse orphan-process signal)

Each check returns {"ok": bool, "detail": str, ...check-specific fields}.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import paths
import workgraph_store as ws
import retention

STALE_CURSOR_HOURS = 48.0
STALE_LOG_HOURS = 30.0
STALE_BACKUP_HOURS = 36.0
ABNORMAL_GROWTH_MULTIPLIER = 2.0  # flag if a tracked size more than doubles in one day
PROCESS_GROWTH_MULTIPLIER = 2.0   # flag if the claude.exe count more than doubles day-over-day

_CURSOR_SOURCES = [
    ("outlook_mail", "folder:Careful"),
    ("teams_chat", "default"),
    ("calendar", "default"),
    ("sharepoint", "default"),
]

_SNAPSHOT_CURSOR_SOURCE = "health_check"
_SNAPSHOT_CURSOR_KEY = "last_snapshot"


def _now_str(now: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))


def _parse_cursor_ts(val: str) -> float | None:
    """Cursor values come in two real shapes (confirmed 2026-07-29 by running
    this against production): calendar/sharepoint store a Unix epoch
    (GRAPH_INGEST_ROUTINE.md sets them to "this wake's run timestamp"), but
    teams_chat stores Microsoft Graph's own lastUpdatedDateTime field
    verbatim - an ISO8601 string like "2026-07-29T20:42:50.569Z". A bare
    float() parse silently swallowed the ISO case entirely (caught by a broad
    except, cursor just never checked) - confirmed by running this for real
    against the live cursors and seeing teams_chat absent from both
    stale_cursors AND any error, simply skipped. Try both formats before
    giving up."""
    try:
        return float(val)
    except (TypeError, ValueError):
        pass
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


def check_cursors_advancing(now: float) -> dict:
    """Flags a cursor that hasn't moved in > STALE_CURSOR_HOURS. A single
    quiet day is normal (weekends, a slow SharePoint week) - the threshold is
    deliberately loose (48h) to avoid noisy false alarms, not a tight SLA."""
    stale = []
    unparseable = []
    values = {}
    for source, key in _CURSOR_SOURCES:
        val = ws.get_cursor(source, key)
        values[f"{source}:{key}"] = val
        if val is None:
            continue  # never run yet - not this check's concern
        cursor_ts = _parse_cursor_ts(val)
        if cursor_ts is None:
            unparseable.append({"source": source, "key": key, "value": val})
            continue
        age_hours = (now - cursor_ts) / 3600.0
        if age_hours > STALE_CURSOR_HOURS:
            stale.append({"source": source, "key": key, "age_hours": round(age_hours, 1)})
    return {"ok": not stale, "stale_cursors": stale, "unparseable_cursors": unparseable, "values": values}


def check_disk_growth_sane(now: float) -> dict:
    """Compares today's disk_usage_report() against yesterday's stored
    snapshot (persisted via the same ingest_cursors mechanism retention.py
    already uses). Flags abnormal shrink (possible data loss) or explosive
    growth (>2x in a day, unusual at personal-inbox scale)."""
    today = retention.disk_usage_report()
    raw = ws.get_cursor(_SNAPSHOT_CURSOR_SOURCE, _SNAPSHOT_CURSOR_KEY)
    if raw is None:
        return {"ok": True, "detail": "no prior snapshot yet - nothing to compare", "today": today}
    try:
        yesterday = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": True, "detail": "prior snapshot unreadable, skipping comparison", "today": today}

    findings = []
    for key in today:
        old = yesterday.get(key)
        new = today[key]
        if old is None or old == 0:
            continue
        if new < old * 0.5:
            findings.append({"metric": key, "old": old, "new": new, "issue": "shrank by >50%"})
        elif new > old * ABNORMAL_GROWTH_MULTIPLIER:
            findings.append({"metric": key, "old": old, "new": new, "issue": "more than doubled in one day"})
    return {"ok": not findings, "findings": findings, "today": today, "yesterday": yesterday}


def check_scheduled_refresh_ran(now: float) -> dict:
    log_path = paths.DATA_DIR / "scheduled_refresh.log"
    if not log_path.is_file():
        return {"ok": False, "detail": "scheduled_refresh.log does not exist"}
    try:
        last_line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        ts_str = " ".join(last_line.split(" ")[:2])
        last_ts = time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S"))
    except (IndexError, ValueError):
        return {"ok": False, "detail": "could not parse last log line's timestamp"}
    age_hours = (now - last_ts) / 3600.0
    return {"ok": age_hours <= STALE_LOG_HOURS, "age_hours": round(age_hours, 1), "last_line": last_line}


def check_backup_recent(now: float) -> dict:
    snap_dir = paths.DB_SNAPSHOTS_DIR / "workgraph"
    if not snap_dir.is_dir():
        return {"ok": False, "detail": "no workgraph snapshot directory yet"}
    snapshots = list(snap_dir.glob("*.db.gz"))
    if not snapshots:
        return {"ok": False, "detail": "workgraph snapshot directory exists but is empty"}
    newest = max(snapshots, key=lambda p: p.stat().st_mtime)
    age_hours = (now - newest.stat().st_mtime) / 3600.0
    return {"ok": age_hours <= STALE_BACKUP_HOURS, "age_hours": round(age_hours, 1), "newest": str(newest)}


def check_raw_ingest_failed() -> dict:
    failed_dir = paths.DATA_DIR / "raw_ingest_failed"
    count = len(list(failed_dir.iterdir())) if failed_dir.is_dir() else 0
    return {"ok": count == 0, "count": count}


def _count_claude_processes() -> int | None:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        )
        if "INFO: No tasks" in result.stdout:
            return 0
        return len([l for l in result.stdout.strip().splitlines() if l.strip()])
    except Exception:
        return None


def check_claude_process_count(now: float) -> dict:
    """Coarse orphan-process signal (Windows-specific, best-effort). NOT
    precise "is THIS specific process orphaned" detection, and deliberately
    NOT a fixed absolute threshold - confirmed via real testing 2026-07-29
    that a perfectly healthy cohort (4 persistent worker sessions + this
    conversation + normal subprocess overhead) legitimately runs 11+
    claude.exe processes at once, which a fixed cap like "10" would falsely
    flag every day. Compares against YESTERDAY's count instead (same
    day-over-day pattern as check_disk_growth_sane) - flags SUBSTANTIAL
    growth (more than doubling), which is what an actual accumulating leak
    produces over time, not a normal day's baseline."""
    count = _count_claude_processes()
    if count is None:
        return {"ok": True, "detail": "could not check (non-fatal - tasklist unavailable)"}
    raw = ws.get_cursor("health_check", "last_process_count")
    ws.set_cursor("health_check", "last_process_count", str(count))
    if raw is None:
        return {"ok": True, "count": count, "detail": "no prior count yet - nothing to compare"}
    try:
        yesterday_count = int(raw)
    except ValueError:
        return {"ok": True, "count": count, "detail": "prior count unreadable, skipping comparison"}
    if yesterday_count == 0:
        return {"ok": True, "count": count, "yesterday_count": yesterday_count}
    grew_abnormally = count > yesterday_count * PROCESS_GROWTH_MULTIPLIER
    return {"ok": not grew_abnormally, "count": count, "yesterday_count": yesterday_count}


def run(now: float | None = None) -> dict:
    if now is None:
        now = time.time()
    checks = {
        "cursors_advancing": check_cursors_advancing(now),
        "disk_growth_sane": check_disk_growth_sane(now),
        "scheduled_refresh_ran": check_scheduled_refresh_ran(now),
        "backup_recent": check_backup_recent(now),
        "raw_ingest_failed": check_raw_ingest_failed(),
        "claude_process_count": check_claude_process_count(now),
    }
    # persist today's disk snapshot for TOMORROW's growth comparison
    ws.set_cursor(_SNAPSHOT_CURSOR_SOURCE, _SNAPSHOT_CURSOR_KEY, json.dumps(retention.disk_usage_report()))

    all_ok = all(c.get("ok", True) for c in checks.values())
    return {"ok": all_ok, "as_of": _now_str(now), "checks": checks}


def run_daily_if_due(now: float | None = None) -> dict | None:
    """Same once-a-day gate as retention.run_daily_if_due - piggybacks the
    5x/day scheduled_refresh cycle without redoing real work 5x."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    last_run = ws.get_cursor("health_check", "last_run_date")
    if last_run == today:
        return None
    result = run(now=now)
    ws.set_cursor("health_check", "last_run_date", today)
    return result


if __name__ == "__main__":
    ws.init_workgraph()
    print(json.dumps(run(), indent=2, default=str))
