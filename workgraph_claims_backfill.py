"""
workgraph_claims_backfill.py — one-time (and ongoing-maintenance) backfills
for Phase 3 (design doc Section 9):

  backfill_claims()        — materializes claims (Section 9.2) for every
                              raw_item that already has an extraction, via
                              workgraph_claims.materialize_claims_for_raw_item.
                              Idempotent: safe to re-run any time, including
                              after new extractions land.
  backfill_evidence_fts()  — indexes evidence_fts (Section 9.6) over every
                              raw_item's resolved text
                              (text_extract.resolve_item_text - a LOCAL file
                              read, never a live Outlook/Graph call, so this
                              is safe and cheap to run over the whole
                              historical corpus). Idempotent per raw_item
                              (workgraph_store.index_evidence_fts deletes
                              then re-inserts).

Same "one-time backfill, then a daily-if-due sweep for anything that landed
since" shape as workgraph_identity.backfill_identity_anchors /
run_backfill_daily_if_due - neither claims materialization nor FTS indexing
is wired into the live ingest/classify path yet, so this sweep is what keeps
both current for raw_items that arrive after the first run.
"""
from __future__ import annotations

import time

import text_extract
import workgraph_claims
import workgraph_store as ws


def backfill_claims(*, limit: int | None = None) -> dict:
    raw_item_ids = ws.list_raw_item_ids_with_extractions()
    if limit is not None:
        raw_item_ids = raw_item_ids[:limit]

    processed = 0
    inserted = 0
    skipped_already_materialized = 0
    skipped_no_issue = 0

    for rid in raw_item_ids:
        already = ws.has_claims_for_raw_item(rid)
        n = workgraph_claims.materialize_claims_for_raw_item(rid)
        processed += 1
        if already:
            skipped_already_materialized += 1
        elif n == 0:
            raw_item = ws.get_raw_item(rid)
            if not raw_item or not raw_item.get("issue_id"):
                skipped_no_issue += 1
        inserted += n

    return {
        "raw_items_scanned": processed,
        "claims_inserted": inserted,
        "already_materialized": skipped_already_materialized,
        "skipped_no_issue_id": skipped_no_issue,
    }


def backfill_evidence_fts(*, limit: int | None = None) -> dict:
    raw_item_ids = ws.list_all_raw_item_ids()
    if limit is not None:
        raw_item_ids = raw_item_ids[:limit]

    indexed = 0
    skipped_empty = 0

    for rid in raw_item_ids:
        raw_item = ws.get_raw_item(rid)
        if not raw_item:
            continue
        body = text_extract.resolve_item_text(raw_item)
        if not body or not body.strip():
            skipped_empty += 1
            continue
        ws.index_evidence_fts(rid, raw_item.get("issue_id"), body)
        indexed += 1

    return {
        "raw_items_scanned": len(raw_item_ids),
        "indexed": indexed,
        "skipped_empty_text": skipped_empty,
    }


def run_backfill_daily_if_due(now: float | None = None) -> dict | None:
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not ws.claim_daily_run("claims_and_fts_backfill", today):
        return None
    return {
        "claims": backfill_claims(),
        "evidence_fts": backfill_evidence_fts(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps({
        "claims": backfill_claims(),
        "evidence_fts": backfill_evidence_fts(),
    }, indent=2))
