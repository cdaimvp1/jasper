"""Task #389 (2026-08-13, Marc's explicit go-ahead): one-time backlog
catch-up for the 654 is_raw_cluster=1 work_objects DB-wide carrying 3+
real claims never cited into a tracked issue (the marc-649/Sodalis
finding this whole session traced back to). Re-derives the exact target
list fresh (never trusts a stale count from earlier in the session),
computed once here before any worker starts, then shards round-robin
across N worker subprocesses - same pattern as backfill_stale_marker_
projects_parallel.py.

The default model= below is haiku (Marc's first choice), but the ACTUAL
run was invoked explicitly with model="sonnet": haiku was diagnosed on a
real case (proj-797/Sodalis) as reaching a defensible but thin
already-covered conclusion, and Marc's correction was "reconsider the
model... i do not think we can afford opus on this... so hopefully sonnet
will do". Sonnet reached the same verdict on that case with visibly more
thorough reasoning, so the full 646-project batch ran on sonnet. Result:
646/646 projects processed, 327 real issues created citing 856 claims."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import workgraph_store as ws

_HERE = Path(__file__).resolve().parent
_WORKER_SCRIPT = _HERE / "_item9_extraction_worker.py"


def find_target_project_ids() -> list:
    conn = ws._connect()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT wo.parent_id
            FROM work_objects wo
            JOIN claims c ON c.issue_id = wo.id
            WHERE wo.is_raw_cluster = 1 AND wo.parent_id IS NOT NULL
            GROUP BY wo.id
            HAVING COUNT(c.id) >= 3
            """
        ).fetchall()
    finally:
        conn.close()
    return sorted({r["parent_id"] for r in rows})


def main(n_workers: int = 6, model: str = "haiku") -> None:
    ws.init_workgraph()
    candidates = find_target_project_ids()
    print(f"total target projects: {len(candidates)}, sharding across {n_workers} workers, model={model}", flush=True)
    if not candidates:
        print("nothing to do", flush=True)
        return

    shards = [candidates[i::n_workers] for i in range(n_workers)]
    procs = []
    shard_files = []
    for i, shard in enumerate(shards):
        if not shard:
            continue
        shard_file = _HERE / f"_item9_shard_{i}.json"
        shard_file.write_text(json.dumps(shard), encoding="utf-8")
        shard_files.append(shard_file)
        log_file = _HERE / f"item9_worker_{i}.log"
        proc = subprocess.Popen(
            [sys.executable, str(_WORKER_SCRIPT), str(shard_file), model, str(log_file)],
            cwd=str(_HERE),
        )
        procs.append((i, proc))
        print(f"worker {i}: {len(shard)} projects, pid={proc.pid}", flush=True)

    t0 = time.time()
    for i, proc in procs:
        proc.wait()
        print(f"worker {i} finished (returncode={proc.returncode}) at +{time.time() - t0:.0f}s", flush=True)

    for f in shard_files:
        try:
            f.unlink()
        except OSError:
            pass

    print(f"all workers done in {time.time() - t0:.0f}s total", flush=True)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    model_arg = sys.argv[2] if len(sys.argv) > 2 else "haiku"
    main(n_workers=n, model=model_arg)
