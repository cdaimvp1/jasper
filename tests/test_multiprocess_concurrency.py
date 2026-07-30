"""Regression test for real cross-PROCESS DB concurrency (task #39) - the
earlier same-session 20-thread stress test only validated threading.Lock
within one process; this spawns genuinely separate OS processes (like real
cohort workers each in their own Claude Code session would be) hammering the
same workgraph.db/bus.db concurrently, and verifies zero lost writes, zero id
collisions, and a fully consistent DB afterward.

Lighter than the ad hoc version used to verify this (4 processes / 3s here,
vs 16 processes / 15s ad hoc, which had the same clean result) - this is
regression coverage, not a full stress benchmark, so it stays fast enough to
run in the normal suite.
"""
import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).parent / "_stress_worker_helper.py"
N_WORKERS = 4
DURATION_S = 3.0


def test_multiple_processes_never_lose_or_collide_writes(isolated_paths):
    procs = []
    for i in range(N_WORKERS):
        p = subprocess.Popen(
            [sys.executable, str(HELPER), f"worker{i}", str(isolated_paths.DATA_DIR), str(DURATION_S)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        procs.append(p)

    results = []
    for p in procs:
        out, err = p.communicate(timeout=DURATION_S + 30)
        assert p.returncode == 0, f"worker process crashed: {err[-500:]}"
        results.append(json.loads(out.strip().splitlines()[-1]))

    total_errors = sum(len(r["errors"]) for r in results)
    assert total_errors == 0, f"unexpected errors under concurrent load: {results}"

    all_ids = [iid for r in results for iid in r["created"]]
    assert len(all_ids) == len(set(all_ids)), "duplicate issue ids across separate processes"
    assert len(all_ids) > 0, "sanity check - the workers should have created something in 3s"

    import workgraph_store as ws
    ws.WORKGRAPH_DB = isolated_paths.WORKGRAPH_DB
    missing = [iid for iid in all_ids if ws.get_issue(iid) is None]
    assert missing == [], f"lost writes - claimed-created issues not found in DB: {missing[:5]}"

    real_count = len(ws.list_issues(states=["active"], limit=10000))
    assert real_count == len(all_ids), "query count mismatch - possible silent corruption"
