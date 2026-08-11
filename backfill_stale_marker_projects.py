"""One-time backfill (task #304, 2026-08-11) - NOT part of the regular
scheduled_refresh.py cadence, invoked manually and only once.

Root cause this closes: workgraph_pipeline2.py's OLD (pre-task-#304-item-#3)
run_project_extraction stamped a project's synthesis marker to the CURRENT
claims-revision fingerprint the moment grouping happened, with zero real
claims materialized underneath (it read raw text directly, never the claims
ledger). Since compute_evidence_marker (workgraph_synthesis.py) is a pure
hash over each member work_object's claims_revision, and claims_revision is
ONLY ever bumped by a claim-writing operation - never by a raw_item merely
arriving on a cluster - a project stamped this way is frozen at that
baseline PERMANENTLY: list_stale_entities() will never flag it again no
matter how much genuinely new content piles up on its clusters, because
nothing about that new content touches claims_revision until something
extracts it, and nothing extracts it until something says it's stale.
Confirmed live: 1164 real projects (~3714 raw_items) in this exact deadlock,
overwhelmingly RECENT (95% within 90 days, none older than 180) - not old
dormant history, active real work frozen by this one bug.

Item #3 (2026-08-11) already fixed the forward-looking half (pipeline2 no
longer stamps a fake-fresh marker). This script is the one-time remediation
for everything the bug already froze before that fix landed - bypasses the
staleness gate directly (calling workgraph_synthesis_light.run_light_
synthesis per project, same real mechanism scheduled_refresh.py's own light/
heavy router already uses at LIGHT_PATH_MAX_BYTES=100_000), then immediately
runs the new claim-grounded workgraph_pipeline2.run_project_extraction so
each project gets real issues the moment its claims exist - no waiting on a
separate curator wake to notice.

Deliberately resumable and safe to interrupt: re-run picks up exactly where
it left off (skips any project that already has a claim under a member
issue - the same real signal used to find this backlog in the first place).
Progress logged to backfill_stale_marker_projects.log in this directory.

Scope, honestly: only the projects with real cluster-linked raw_item content
(compute_new_evidence_bytes > 0) AND currently under LIGHT_PATH_MAX_BYTES
are handled here - that's the ~88% majority found live. The remaining
~12% (genuinely large evidence, routed to curator's heavy synthesis wake in
the normal live pipeline) are NOT touched by this script - curator's heavy
path only embeds a short fixed instruction prompt and reads the DB live via
its own Bash-tool calls, so it doesn't share this script's real trigger
(the staleness-gate deadlock) in the same way and needs a separate, smaller
follow-up once list_stale_entities can actually see them (a real remaining
piece, not silently dropped)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import workgraph_store as ws
import workgraph_synthesis_light as wsl
import workgraph_pipeline2 as p2

LOG_PATH = Path(__file__).resolve().parent / "backfill_stale_marker_projects.log"


def _log(line: str) -> None:
    stamped = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}"
    print(stamped, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(stamped + "\n")


def find_candidate_project_ids(*, since_ts: float | None = None) -> list[str]:
    """Every project with at least one cluster member carrying real,
    never-materialized raw_item content, and zero claims anywhere under
    any of its member issues yet - the exact backlog this script exists
    to clear. Re-run-safe: a project this backfill already processed no
    longer matches (it now has claims), so it naturally drops out.

    since_ts (2026-08-11, Marc's own explicit prioritization call):
    optional recency floor - when set, only projects with at least one
    raw_item.occurred_ts >= since_ts anywhere under the project qualify.
    Projects with zero real communication since that date sort to the
    back of the real backlog rather than being dropped outright - they
    just never surface while since_ts is set."""
    conn = ws._connect()
    try:
        since_clause = ""
        params: tuple = ()
        if since_ts is not None:
            since_clause = """
              AND EXISTS (
                SELECT 1 FROM raw_items ri JOIN work_objects w2 ON w2.id = ri.issue_id
                WHERE w2.parent_id = p.id AND ri.occurred_ts >= ?
              )"""
            params = (since_ts,)
        rows = conn.execute(f"""
            SELECT DISTINCT p.id
            FROM projects p
            WHERE EXISTS (SELECT 1 FROM work_objects w WHERE w.parent_id = p.id AND w.is_raw_cluster = 1)
              AND NOT EXISTS (
                SELECT 1 FROM issues i JOIN claims c ON c.issue_id = i.id WHERE i.project_id = p.id
              ){since_clause}
            ORDER BY p.id
        """, params).fetchall()
    finally:
        conn.close()
    return [r["id"] for r in rows]


def run(*, limit: int | None = None, model: str | None = None, since_ts: float | None = None) -> dict:
    candidates = find_candidate_project_ids(since_ts=since_ts)
    if limit is not None:
        candidates = candidates[:limit]
    _log(f"=== backfill run starting: {len(candidates)} candidate projects "
         f"(model={model or 'default'}, since_ts={since_ts or 'none'}) ===")

    processed = 0
    light_ok = 0
    heavy_skipped = 0
    errors = []
    issues_created_total = 0

    for pid in candidates:
        try:
            size = wsl.compute_new_evidence_bytes("project", pid)
        except Exception as e:
            errors.append({"project_id": pid, "stage": "size_check", "error": str(e)})
            _log(f"{pid} ERROR at size_check: {e}")
            continue

        if size == 0:
            continue  # nothing to extract after all - not part of this backlog
        if size >= wsl.LIGHT_PATH_MAX_BYTES:
            heavy_skipped += 1
            _log(f"{pid} SKIP (heavy path, {size} bytes - not handled by this script)")
            continue

        t0 = time.time()
        try:
            synth_result = wsl.run_light_synthesis("project", pid, model=model)
        except Exception as e:
            errors.append({"project_id": pid, "stage": "synthesis", "error": str(e)})
            _log(f"{pid} ERROR at synthesis: {e}")
            continue

        try:
            extract_result = p2.run_project_extraction(pid, model=model)
        except Exception as e:
            errors.append({"project_id": pid, "stage": "extraction", "error": str(e)})
            _log(f"{pid} synthesis ok ({synth_result.get('action')}) but ERROR at extraction: {e}")
            continue

        elapsed = time.time() - t0
        created = extract_result.get("created_issue_ids") or []
        issues_created_total += len(created)
        processed += 1
        light_ok += 1
        _log(f"{pid} ok - synth={synth_result.get('action')} extract={extract_result.get('action')} "
             f"created_issues={len(created)} ({elapsed:.1f}s)")

    summary = {
        "candidates_found": len(candidates),
        "processed_ok": processed,
        "light_ok": light_ok,
        "heavy_skipped": heavy_skipped,
        "issues_created_total": issues_created_total,
        "errors": errors,
    }
    _log(f"=== backfill run finished: {summary} ===")
    return summary


if __name__ == "__main__":
    import json
    import datetime
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else None
    model = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    since_arg = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
    since_ts = datetime.datetime.strptime(since_arg, "%Y-%m-%d").timestamp() if since_arg else None
    result = run(limit=limit, model=model, since_ts=since_ts)
    print(json.dumps(result, indent=2))
