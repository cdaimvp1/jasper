"""backfill_sharepoint_web_url.py - one-time repair for task #414. No LLM.

WHY. ingest/normalize.py::_process_sharepoint read `webUrl` off each search
result and dropped it on the floor: only stable_key/thread_key/subject/
body_preview survived into raw_items, leaving meta_json NULL. That is the
whole reason all 100 unlinked SharePoint items failed the standalone-FYI gate
(workgraph_classify._fyi_item_has_a_real_signal) - not on judgment, but for
want of any input at all, since that gate's other three checks read a
reference, an email domain, or an Ariba subject and a document row has none.

The normalizer now persists web_url/drive_id going forward. This script
repairs the rows already in the DB, deterministically, by re-reading the
archived drop files those rows came from and matching on
(source='sharepoint', stable_key) - the same drive_id:item_id source_ref the
normalizer built. Nothing is re-ingested and no row is created or deleted;
this only fills meta_json where it is currently NULL.

Deliberately does NOT clear last_link_check_ts. Re-linking these items runs
them into pipeline2's candidate judgment, which is a real LLM cost, so that
step stays a separate, explicitly-authorized decision - see task #414.

Usage:
    python backfill_sharepoint_web_url.py            # dry run, reports only
    python backfill_sharepoint_web_url.py --apply    # write meta_json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import workgraph_store as ws
from paths import DATA_DIR

_ARCHIVE_DIRS = ("raw_ingest_processed", "raw_ingest_failed", "raw_ingest_inbox")


def collect_web_urls() -> dict[str, dict]:
    """stable_key -> {"web_url": ..., "drive_id": ...} from every archived
    SharePoint drop file. Later files win on a repeat stable_key, which is
    the right way round: the same document re-observed later carries the more
    current path if it was ever moved."""
    found: dict[str, dict] = {}
    for dirname in _ARCHIVE_DIRS:
        d = DATA_DIR / dirname
        if not d.exists():
            continue
        for path in sorted(d.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue  # an unreadable archive file is not this script's problem
            if payload.get("source") != "sharepoint":
                continue
            for r in payload.get("results") or []:
                stable_key = f"{r.get('driveId') or ''}:{r.get('id') or ''}"
                meta = {}
                if r.get("webUrl"):
                    meta["web_url"] = r["webUrl"]
                if r.get("driveId"):
                    meta["drive_id"] = r["driveId"]
                if meta:
                    found[stable_key] = meta
    return found


def run(apply: bool = False) -> dict:
    by_key = collect_web_urls()
    conn = ws._connect()
    rows = [
        dict(r) for r in conn.execute(
            "SELECT id, stable_key, subject, meta_json FROM raw_items WHERE source = 'sharepoint'"
        )
    ]

    matched, already, unmatched, written = 0, 0, 0, 0
    for row in rows:
        if row["meta_json"]:
            already += 1
            continue
        meta = by_key.get(row["stable_key"])
        if not meta:
            unmatched += 1
            continue
        matched += 1
        if apply:
            conn.execute(
                "UPDATE raw_items SET meta_json = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), row["id"]),
            )
            written += 1

    return {
        "sharepoint_rows": len(rows),
        "archive_web_urls": len(by_key),
        "already_had_meta": already,
        "matched_and_fillable": matched,
        "no_archive_entry": unmatched,
        "written": written,
        "applied": apply,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write meta_json")
    a = ap.parse_args()
    print(json.dumps(run(apply=a.apply), indent=2))
