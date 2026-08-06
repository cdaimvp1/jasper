"""One-off runner for workgraph_pipeline2 over the full backlog, with real,
live progress visibility - both via unbuffered stdout (run this with
`python -u`) and via a persisted cursor (ws.get_cursor("pipeline2",
"progress")) that can be checked directly from the DB at any moment,
independent of stdout buffering. Not part of the live scheduled cadence -
a manual, one-time catch-up tool.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workgraph_store as ws
import workgraph_pipeline2 as p2

ws.init_workgraph()

ungrouped = [
    w for w in (ws.list_issues(states=None, limit=10000) + ws.list_clusters(limit=10000))
    if not w.get("project_id")
]
ungrouped.sort(key=lambda w: w.get("opened_at") or w.get("created_ts") or 0)
total = len(ungrouped)
print(f"TOTAL ungrouped work objects to process: {total}", flush=True)
ws.set_cursor("pipeline2", "progress_total", str(total))

counts = {}
start = time.time()
for i, w in enumerate(ungrouped, 1):
    result = p2.process_new_item(w["id"])
    action = result.get("action", "?")
    counts[action] = counts.get(action, 0) + 1
    elapsed = time.time() - start
    rate = i / elapsed if elapsed > 0 else 0
    eta_min = (total - i) / rate / 60 if rate > 0 else 0
    print(f"[{i}/{total}] {w['id']} -> {action} "
          f"(elapsed={elapsed/60:.1f}m, eta={eta_min:.1f}m, counts={counts})", flush=True)
    ws.set_cursor("pipeline2", "progress_done", str(i))
    ws.set_cursor("pipeline2", "progress_last_action", action)

print(f"DONE. total={total} counts={counts} elapsed_min={(time.time()-start)/60:.1f}", flush=True)
