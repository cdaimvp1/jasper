"""judge_clusters.py - bounded candidate judgment over an explicit id list. Task #413.

WHY THIS EXISTS. workgraph_pipeline2.run_pipeline_for_ungrouped_items() is the
right entry point for the scheduled loop, but it sweeps EVERY ungrouped
cluster in the graph with a default limit of 500. For a controlled backfill
that is the wrong shape twice over: it spends LLM on items nobody asked about,
and it makes the result unattributable - you cannot tell which merges came
from the batch you just staged.

This runs process_new_item over exactly the ids you hand it, writes one JSON
line per item as it goes (so a multi-hour run loses nothing if interrupted),
and keeps the id list on disk so the batch stays reversible.

COST. Real LLM spend, roughly 2-4 minutes per item measured across the #413
pilot (25 items / 62 min) and batch one. A 78-item batch is ~3-4 hours. Run it
in the background.

TYPICAL USE - the calendar backfill flow:
    python ingest/calendar_backfill.py --limit 100 --apply     # stage
    python -c "import sys; sys.path.insert(0,'ingest'); from ingest import normalize; normalize.run()"
    python -c "import workgraph_classify as wc; wc.run_classification(limit=300); wc.cluster_and_link(limit=300)"
    # capture the new cluster ids, then:
    python ingest/judge_clusters.py --ids batch.json --out batch_results.jsonl

Usage:
    python judge_clusters.py --ids <file.json> [--out results.jsonl] [--model sonnet]
    python judge_clusters.py --ids <file.json> --dry-run    # report only, no LLM
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workgraph_pipeline2 as wp
import workgraph_store as ws


def run(ids_path: str, out_path: str, model: str = "sonnet", dry_run: bool = False) -> dict:
    ids = json.loads(Path(ids_path).read_text(encoding="utf-8"))
    if not isinstance(ids, list):
        raise ValueError(f"{ids_path} must contain a JSON list of work_object ids")

    if dry_run:
        # find_candidates is deterministic and free - this previews what the
        # judgment would even have to work with, which is the check that would
        # have caught #414's document problem before spending anything.
        rows = []
        for cid in ids:
            issue = ws.get_issue_or_cluster(cid)
            if issue is None:
                rows.append({"cluster": cid, "error": "not found"})
                continue
            try:
                cands = wp.find_candidates(cid, issue)
                rows.append({"cluster": cid, "candidates": len(cands),
                             "best": max((len(c["matched_signals"]) for c in cands), default=0)})
            except Exception as e:
                rows.append({"cluster": cid, "error": f"{type(e).__name__}: {e}"})
        reachable = sum(1 for r in rows if r.get("candidates"))
        return {"dry_run": True, "items": len(ids),
                "with_at_least_one_candidate": reachable,
                "would_become_new_project": len(ids) - reachable, "detail": rows}

    done = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for i, cid in enumerate(ids, 1):
            rec = {"n": i, "cluster": cid}
            t0 = time.time()
            try:
                rec["result"] = wp.process_new_item(cid, model=model)
                rec["ok"] = True
                done += 1
            except Exception as e:
                rec["ok"] = False
                rec["error"] = f"{type(e).__name__}: {e}"
                rec["trace"] = traceback.format_exc()[-400:]
            rec["secs"] = round(time.time() - t0, 1)
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            print(f"[{i}/{len(ids)}] {cid} ok={rec['ok']} {rec['secs']}s "
                  f"{str(rec.get('result'))[:100]}", flush=True)
    return {"items": len(ids), "ok": done, "results": out_path}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True, help="JSON file holding a list of work_object ids")
    ap.add_argument("--out", default="judge_results.jsonl")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--dry-run", action="store_true",
                    help="deterministic preview: how many could reach a candidate at all")
    a = ap.parse_args()
    print(json.dumps(run(a.ids, a.out, a.model, a.dry_run), indent=2, default=str)[:2000])
