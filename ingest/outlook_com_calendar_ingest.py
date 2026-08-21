"""
outlook_com_calendar_ingest.py — deterministic Outlook calendar ingestion, no LLM.

Task #413. Companion to outlook_com_ingest.py (mail); same COM/MAPI mechanism,
same never-let-one-failure-block-the-rest discipline, same no-worker/no-LLM
dependency.

WHY. Calendar previously came in through the relay — an LLM session asked to
fetch events and write them to a drop file. Measured 2026-08-20, that path
silently lost data: one file archived as SUCCESS contained
{"events_catchup_count":25,"events_lookahead_count":25,"note":"Event details
truncated in this sample..."} — a description of 50 events instead of the
events, with the cursor advanced past them. An LLM cannot be a reliable data
pipe. Graph API would be the obvious replacement, but Lilly does not issue
Graph credentials, so it is permanently unavailable. Outlook COM is not: the
same namespace already used for mail exposes the calendar, and a live probe
found 2,492 items against 59 then ingested.

DESIGN. This deliberately does NOT write raw_items itself. It runs the COM
scanner, writes the resulting payload as an ordinary drop file into
raw_ingest_inbox, and lets ingest/normalize.py consume it exactly as it would
a relay file. That means the already-tested calendar parsing, the
recurring-series thread_key logic (#109), the dedupe_key, and the drop-file
guard added in commit 2d3ba0a all apply unchanged — only the unreliable
transcription step is replaced. Verified on live data: 40 events -> 40 parsed
items, guard clean, 20 recurring events collapsed into 32 series thread_keys.

COST WARNING. Ingestion itself is free. What follows it is NOT: new raw_items
flow into classify and then pipeline2, whose candidate judgment and issue
extraction are real LLM calls. Backfilling the full 2,492-item calendar would
cascade into substantial downstream spend. That is why the default window here
is small and why run() takes explicit bounds rather than defaulting to
everything.

Usage:
    python outlook_com_calendar_ingest.py [--days-back 30] [--days-forward 14] [--max-items 200]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workgraph_store as ws
from paths import DATA_DIR

_HERE = Path(__file__).resolve().parent
_SCANNER = _HERE / "outlook_calendar_scan.ps1"
_INBOX = DATA_DIR / "raw_ingest_inbox"
_SOURCE = "calendar"


def scan(days_back: int = 30, days_forward: int = 14, max_items: int = 200,
         timeout: int = 180) -> dict:
    """Run the COM scanner and return its parsed payload. Raises on failure -
    a scan that did not produce parseable output must be loud, since the whole
    point of this module is that the previous path failed quietly."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(_SCANNER),
         "-DaysBack", str(days_back), "-DaysForward", str(days_forward),
         "-MaxItems", str(max_items)],
        capture_output=True, encoding="utf-8", timeout=timeout,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise RuntimeError(
            f"calendar scan failed (rc={proc.returncode}): {(proc.stderr or '')[:400]}"
        )
    return json.loads(proc.stdout)


def run(days_back: int = 30, days_forward: int = 14, max_items: int = 200,
        write_drop_file: bool = True) -> dict:
    """Scan and stage a drop file for normalize.py. Returns a summary.

    Does not call normalize.run() itself - staging and consuming stay separate
    processes, exactly as with the relay, so the guard and the failure/archive
    routing behave identically no matter who produced the file.
    """
    payload = scan(days_back=days_back, days_forward=days_forward, max_items=max_items)
    n = len(payload.get("events") or [])

    drop_path = None
    if write_drop_file and n:
        _INBOX.mkdir(parents=True, exist_ok=True)
        drop_path = _INBOX / f"calendar_{int(time.time())}.json"
        drop_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    ws.set_cursor(_SOURCE, "last_com_scan_ts", str(time.time()))
    return {
        "source": _SOURCE, "events": n,
        "window": [payload.get("window_start"), payload.get("window_end")],
        "drop_file": str(drop_path) if drop_path else None,
        "note": "staged only - run ingest/normalize.py to insert raw_items",
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=30)
    ap.add_argument("--days-forward", type=int, default=14)
    ap.add_argument("--max-items", type=int, default=200)
    ap.add_argument("--no-write", action="store_true",
                    help="scan and report only; stage nothing")
    a = ap.parse_args()
    print(json.dumps(run(days_back=a.days_back, days_forward=a.days_forward,
                         max_items=a.max_items, write_drop_file=not a.no_write), indent=2))
