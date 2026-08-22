"""Worker subprocess for run_item9_backlog_catchup.py (task #389, 2026-08-20,
Marc's explicit go-ahead). Processes an explicit, pre-computed shard of
project ids - never re-derives its own candidate list. Unlike
_backfill_parallel_worker.py (which gates on compute_new_evidence_bytes
for the STALE-MARKER backlog), this target set is already exact: every
project here was selected directly via a SQL join on claims already
materialized against an is_raw_cluster=1 member with 3+ uncited claims -
so this calls workgraph_pipeline2.run_project_extraction directly, no
light-synthesis step needed (the claims already exist; only citation is
missing)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import workgraph_pipeline2 as p2


def _log(log_path: Path, line: str) -> None:
    stamped = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}"
    print(stamped, flush=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(stamped + "\n")


def main(shard_file: str, model: str, log_file: str) -> None:
    log_path = Path(log_file)
    project_ids = json.loads(Path(shard_file).read_text(encoding="utf-8"))
    _log(log_path, f"=== worker starting: {len(project_ids)} projects, model={model} ===")

    processed = errors = no_claims = issues_created_total = 0
    for pid in project_ids:
        t0 = time.time()
        try:
            result = p2.run_project_extraction(pid, model=model)
        except Exception as e:
            errors += 1
            _log(log_path, f"{pid} ERROR: {e}")
            continue

        action = result.get("action")
        if action == "no_claims_yet":
            no_claims += 1
            _log(log_path, f"{pid} no_claims_yet (unexpected for this target set)")
            continue
        if action == "not_found":
            errors += 1
            _log(log_path, f"{pid} not_found")
            continue
        if action == "timeout":
            errors += 1
            _log(log_path, f"{pid} timeout")
            continue

        created = result.get("created_issue_ids") or []
        issues_created_total += len(created)
        processed += 1
        _log(log_path, f"{pid} ok - created_issues={len(created)} ids={created} ({time.time() - t0:.1f}s)")

    _log(log_path, f"=== worker finished: processed_ok={processed} errors={errors} "
                    f"no_claims={no_claims} issues_created_total={issues_created_total} ===")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
