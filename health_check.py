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
  7. classify/link pipeline actually linking (task #30, 2026-08-01) - the
     exact real incident this closes: cluster_and_link() silently stopped
     linking anything new for 3 days with zero error anywhere, because
     classification (a separate step) kept advancing fine and nothing
     watched linking specifically
  8. pending project-suggestion queue not growing unboundedly (task #31,
     2026-08-01) - a review-capacity bottleneck, not a correctness bug:
     one fix shipped the same day added 63 pending suggestions in one run,
     with no visibility anywhere into how many exist or how stale the
     oldest one is
  9. Outlook not staying open across scheduled scans (task #149) - every
     ingestion cycle's own COM connection silently cold-starts Outlook if
     it isn't already running; a freshly-launched Cached Exchange Mode
     profile hasn't had time to sync before the scan reads it. Flags 3+
     consecutive cold-started scans, not a single one (which is normal)
  10. Accuracy telemetry (task #304, item #4, 2026-08-11) - false-merge-
      correction rate, false-split catches, claim-correction rate over a
      rolling 7-day window. See workgraph_telemetry.py for the real query
      logic and each metric's honest scope/caveats. Always ok=True - an
      observability check, not a pass/fail gate; nothing here fails the
      overall health-check status.

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
import workgraph_telemetry
import retention

STALE_CURSOR_HOURS = 48.0
STALE_LOG_HOURS = 30.0
STALE_BACKUP_HOURS = 36.0
STALE_LINK_HOURS = 24.0  # tighter than STALE_CURSOR_HOURS - classify/link runs every scheduled_refresh pass (5x/day)
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


def check_classify_link_progressing(now: float) -> dict:
    """Real incident, 2026-08-01: get_items_pending_link's oldest-first
    query let an ever-growing pool of permanently-skipped rows crowd out
    everything newer than whenever that pool first exceeded its LIMIT -
    cluster_and_link() hadn't linked a single new item in 3 days, with no
    error anywhere, because classification (a separate step) kept
    advancing fine and nothing watched linking specifically.

    Fixed same day (task #25): get_items_pending_link now orders never-
    checked items first, so a genuinely new classified item should never
    wait long for its turn. That means this check is simple and exact: if
    workgraph_store.oldest_never_checked_unlinked_ts() is older than
    STALE_LINK_HOURS, cluster_and_link() has stopped running entirely
    (a scheduling failure, an exception, or something else) - not a normal
    backlog effect, since the backlog itself can no longer explain it."""
    oldest_ts = ws.oldest_never_checked_unlinked_ts()
    if oldest_ts is None:
        return {"ok": True, "detail": "no never-checked classified-but-unlinked items"}
    age_hours = (now - oldest_ts) / 3600.0
    return {"ok": age_hours <= STALE_LINK_HOURS, "oldest_unlinked_age_hours": round(age_hours, 1)}


def check_outlook_cache_freshness() -> dict:
    """Task #149, revised task #278 (2026-08-08): outlook_com_ingest.run()
    persists (via the same generic ingest_cursors store the forward cursor
    itself uses) whether its OWN COM connection had to cold-start Outlook,
    plus a running streak of consecutive cold starts.

    This used to gate ok=False once that streak passed a fixed threshold
    (3), on the theory that Outlook essentially NEVER staying open between
    scheduled scans was itself the anomaly worth flagging. Investigating a
    live streak of 7 (2026-08-08) found that premise wrong: outlook_com_
    ingest.run()'s OWN docstring already documents that every real
    invocation - including the live scheduled cadence - runs in a fresh
    subprocess that cold-starts Outlook and lets it fully quit again on
    exit, since nothing keeps a persistent COM host alive BETWEEN separate
    scheduled_refresh.py ticks. Given that architecture, the streak is
    GUARANTEED to climb past any fixed threshold and stay there forever -
    not a signal of degradation, just the inevitable shape of a "spin up a
    fresh subprocess 5x/day" design. A check that can never be un-tripped
    again isn't a health signal, it's permanent noise.

    The actual risk the streak was a proxy for - a cold-started scan
    reading a local cache that hasn't had time to sync down brand-new mail
    yet - is now handled directly: scheduled_refresh.py passes a real
    sync_wait_seconds on every live call (see its own comment), so
    outlook_scan.ps1's SendAndReceive has time to land before the folder
    read happens, cold start or not. Genuine schedule/cursor staleness -
    the job not firing at all, or the cursor not advancing - is already
    covered by the sibling check_scheduled_refresh_ran and
    check_cursors_advancing checks above, which is the right place for a
    real "mail ingestion is stuck" alarm to live.

    So this check stays purely informational now: the streak is still
    reported (useful to see at a glance that cold-starting is happening
    every time, exactly as expected), but no longer flips ok to False on
    its own - there is no longer a threshold at which "cold start" itself
    means something is actually wrong."""
    last_cold_started = ws.get_cursor("outlook_mail", "last_scan_outlook_cold_started")
    if last_cold_started is None:
        return {"ok": True, "detail": "no ingestion run recorded yet"}
    streak = int(ws.get_cursor("outlook_mail", "consecutive_cold_starts") or "0")
    return {
        "ok": True,
        "last_scan_cold_started": last_cold_started == "true",
        "consecutive_cold_starts": streak,
    }


# Real, high threshold on purpose - a handful of encrypted/S-MIME messages
# genuinely can't have their .Body/.HTMLBody read via COM, ever, and that's
# not a bug. What this exists to catch is the OTHER shape: the full-body
# capture pipeline silently starving on every single item, invisibly,
# forever (confirmed live 2026-08-05 - raw_ref was NULL for all 2,757
# raw_items across every source, and outlook_scan.ps1's Save-FullBody had
# been swallowing every failure with a bare `catch { }` since task #43).
BODY_CAPTURE_FAILURE_ALERT_THRESHOLD = 20


def check_body_capture_healthy() -> dict:
    """Sibling check to check_outlook_cache_freshness, same (source,
    cursor_key) persistence shape - outlook_com_ingest.py's run()/
    sweep_unread() both now parse Save-FullBody's JASPER_DIAG: body_
    capture_failed lines off stderr (fixed 2026-08-05, see that function's
    own docstring) and accumulate a running total here. This is the first
    real, ongoing signal that pipeline has ever had - before this fix, a
    100%-failing body-capture path looked EXACTLY like "every item
    legitimately has no body," with zero way to tell the difference."""
    total = ws.get_cursor("outlook_mail", "body_capture_failures_total")
    if total is None:
        return {"ok": True, "detail": "no body-capture failures recorded yet"}
    total = int(total)
    return {
        "ok": total < BODY_CAPTURE_FAILURE_ALERT_THRESHOLD,
        "body_capture_failures_total": total,
        "last_body_capture_failure": ws.get_cursor("outlook_mail", "last_body_capture_failure"),
    }


_TELEMETRY_WINDOW_SECONDS = 7 * 24 * 3600.0


def check_accuracy_telemetry(now: float) -> dict:
    """Task #304, item #4 (2026-08-11) - Marc's own build authorization,
    wired into THIS exact mechanism because he pointed to it directly
    ("it would have caught today's health-check blind spot pattern too").
    Rolling 7-day window - see workgraph_telemetry.py for the real query
    logic and each metric's honest scope/caveats. Always ok=True; this is
    observability, never a pass/fail gate - a rising false_merge_
    correction_rate or a growing false_split_catches count is Marc's own
    signal to look closer, not something this check itself judges."""
    metrics = workgraph_telemetry.compute_accuracy_metrics(
        window_start=now - _TELEMETRY_WINDOW_SECONDS, window_end=now,
    )
    return {"ok": True, "window_days": 7, **metrics}


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
        "classify_link_progressing": check_classify_link_progressing(now),
        "outlook_cache_freshness": check_outlook_cache_freshness(),
        "body_capture_healthy": check_body_capture_healthy(),
        "accuracy_telemetry": check_accuracy_telemetry(now),
    }
    # persist today's disk snapshot for TOMORROW's growth comparison
    ws.set_cursor(_SNAPSHOT_CURSOR_SOURCE, _SNAPSHOT_CURSOR_KEY, json.dumps(retention.disk_usage_report()))

    all_ok = all(c.get("ok", True) for c in checks.values())
    return {"ok": all_ok, "as_of": _now_str(now), "checks": checks}


_LAST_RESULT_SOURCE = "health_check"
_LAST_RESULT_KEY = "last_result"


def run_daily_if_due(now: float | None = None) -> dict | None:
    """Same once-a-day gate as retention.run_daily_if_due - piggybacks the
    5x/day scheduled_refresh cycle without redoing real work 5x. Persists
    the full result (task #74, Settings panel) so it can be READ without
    re-running run() on demand - re-running would corrupt the day-over-day
    comparisons check_disk_growth_sane/check_claude_process_count depend
    on (each call overwrites "yesterday's" baseline with "just now")."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not ws.claim_daily_run("health_check", today):
        return None
    result = run(now=now)
    ws.set_cursor(_LAST_RESULT_SOURCE, _LAST_RESULT_KEY, json.dumps(result))
    return result


def get_last_result() -> dict | None:
    """The most recent daily run_daily_if_due() result, or None if it has
    never run yet in this install. Read-only - never triggers a new run,
    exactly so a Settings panel can be opened any number of times a day
    without disturbing the day-over-day comparisons above."""
    raw = ws.get_cursor(_LAST_RESULT_SOURCE, _LAST_RESULT_KEY)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    ws.init_workgraph()
    print(json.dumps(run(), indent=2, default=str))
