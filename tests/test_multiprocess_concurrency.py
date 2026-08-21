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
    # Create the schema ONCE, in the parent, before any worker starts.
    #
    # Fixed 2026-08-21 after this test failed twice under full-suite load while
    # passing every time in isolation. The real failure was
    # "sqlite3.OperationalError: no such table: issues" inside a WORKER, not a
    # lost write - all four workers were racing to run init_workgraph() against
    # the same brand-new isolated DB, so one could start writing against a view
    # another had not finished creating yet. In isolation the machine is idle
    # enough that the first worker wins comfortably; under load the interleaving
    # shows up.
    #
    # This is test hermeticity, not a production concurrency defect: in real use
    # the DB already exists and is initialised long before any cohort worker
    # opens it, so a first-init race cannot occur. The helper still calls
    # init_workgraph() itself (harmless and idempotent once the schema exists) -
    # what changes is that it is no longer the FIRST thing to do so. Same class
    # as the "possibly-uninitialized-DB test dependencies" already noted under
    # task #385.
    #
    # What this test is actually for - zero lost writes, zero id collisions
    # across genuinely separate OS processes - is unchanged and still exercised.
    import workgraph_store as ws
    import bus
    ws.init_workgraph()
    bus.init_bus()

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
