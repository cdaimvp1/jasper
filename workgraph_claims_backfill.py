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
  backfill_canonical_keys_and_merge_duplicates() — one-time cleanup
                              (2026-08-04) for canonical claim dedup: backs
                              every existing OPEN claim with a canonical_key
                              and merges pre-existing duplicate groups the
                              byte-exact repeat_signals dedup missed.
                              Idempotent, but deliberately NOT wired into
                              run_backfill_daily_if_due below - new
                              duplicates are already prevented at write time
                              by materialize_claims_for_raw_item's own
                              canonical_key fallback, so there's nothing new
                              for a daily re-run of this to find.
  backfill_extraction_content_hashes() — one-time migration (2026-08-04)
                              for corrected-extraction reconciliation:
                              backfills content_hash for pre-existing
                              extraction rows, and grandfathers in
                              materialized_hash=content_hash for every
                              raw_item already materialized under the OLD
                              scheme - so this migration doesn't treat
                              every already-correct extraction as an
                              unreconciled correction the moment it ships.
                              Must run BEFORE relying on materialize_
                              claims_for_raw_item's reconciliation path on
                              a pre-existing DB.
  backfill_resolution_signal_suggestions() — sweep (task #155, wired into
                              run_backfill_daily_if_due, unlike the two
                              backfills above) that turns any extraction's
                              resolution_signals into suggest-only claim-
                              resolution suggestions via workgraph_
                              reconcile.generate_resolution_signal_
                              suggestions - catches any raw_item whose
                              extraction (or correction) landed outside the
                              server_lean.py live-wiring point. Also see
                              workgraph_reconcile.detect_issue_closed_
                              with_open_claims_contradictions, the second
                              (issue-state-side) half of task #155, run in
                              the same daily sweep below.

Same "one-time backfill, then a daily-if-due sweep for anything that landed
since" shape as workgraph_identity.backfill_identity_anchors /
run_backfill_daily_if_due - neither claims materialization nor FTS indexing
is wired into the live ingest/classify path yet, so this sweep is what keeps
both current for raw_items that arrive after the first run.
"""
from __future__ import annotations

import json
import time

import text_extract
import workgraph_claims
import workgraph_reconcile
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


def backfill_extraction_content_hashes() -> dict:
    """One-time migration (2026-08-04, corrected-extraction reconciliation,
    architecture-review follow-up P1): computes content_hash for every
    existing raw_item_extractions row that doesn't have one yet (rows
    written before create_extraction started computing it automatically),
    and GRANDFATHERS IN materialized_hash = content_hash for every raw_item
    that already has real claims (materialized under the pre-reconciliation
    scheme, via the old has_claims_for_raw_item guard) - so this migration
    does NOT retroactively treat every already-correct, unchanged
    extraction as needing a from-scratch reconciliation diff the moment
    this ships. Extractions with NO claims yet (immaterial, or simply not
    yet processed) are deliberately left with materialized_hash=NULL -
    same as any real never-materialized raw_item, so the normal first-time
    insert-only path in materialize_claims_for_raw_item still applies to
    them on the next backfill_claims() run."""
    raw_item_ids = ws.list_raw_item_ids_with_extractions()
    hashes_backfilled = 0
    grandfathered = 0
    for rid in raw_item_ids:
        extraction = ws.get_extraction(rid)
        if not extraction:
            continue
        content_hash = extraction.get("content_hash")
        if content_hash is None:
            content_hash = ws.canonical_json_hash(json.dumps(extraction["extracted_json"]))
            ws.set_extraction_content_hash(rid, content_hash)
            hashes_backfilled += 1
        if extraction.get("materialized_hash") is None and ws.has_claims_for_raw_item(rid):
            ws.mark_extraction_materialized(rid, content_hash)
            grandfathered += 1
    return {
        "raw_items_scanned": len(raw_item_ids),
        "hashes_backfilled": hashes_backfilled,
        "grandfathered": grandfathered,
    }


def backfill_canonical_keys_and_merge_duplicates() -> dict:
    """One-time (and safely re-runnable) backfill for canonical claim
    deduplication (2026-08-04, architecture-review follow-up P1):

    (1) computes canonical_key for every OPEN claim that doesn't have one
    yet, via workgraph_claims.canonical_key_for_claim, using the claim's
    own producing raw_item's real pr_number_base when one exists.

    (2) groups OPEN claims by (issue_id, claim_type, canonical_key) and,
    for every group with more than one member, keeps the EARLIEST
    (first_seen_ts) as the real canonical claim and marks the rest
    status='superseded' (superseded_by=<canonical id>) - never a delete,
    same reversible-bookkeeping convention as every other resolution in
    this codebase (expire_stale_project_suggestions, etc). Real known live
    duplicate groups this closes: the same Ariba PR reminder re-sent with
    different wording around PR1161567/PR1170816/PR1169904/PR854779-V4.

    Deliberately idempotent: a claim that already has a canonical_key is
    skipped in step 1 (not recomputed), and an already-'superseded' claim
    is excluded from list_all_open_claims entirely - re-running this after
    a first clean pass finds nothing left to merge. Per the design's own
    sequencing requirement, this must run and be verified clean BEFORE any
    uniqueness could ever be considered on (issue_id, claim_type,
    canonical_key) - see workgraph_store's own schema comment for why no
    such constraint exists yet."""
    claims = ws.list_all_open_claims()
    computed = 0
    for claim in claims:
        if claim.get("canonical_key"):
            continue
        raw_item = ws.get_raw_item(claim["raw_item_id"])
        reference_base = (raw_item or {}).get("pr_number_base")
        canonical_key = workgraph_claims.canonical_key_for_claim(
            claim["claim_type"], claim["text"], claim.get("owner"), reference_base,
        )
        if canonical_key is None:
            continue
        ws.set_claim_canonical_key(claim["id"], canonical_key)
        claim["canonical_key"] = canonical_key
        computed += 1

    groups: dict = {}
    for claim in claims:
        key = claim.get("canonical_key")
        if not key:
            continue
        groups.setdefault((claim["issue_id"], claim["claim_type"], key), []).append(claim)

    groups_merged = 0
    claims_absorbed = 0
    merges = []
    for (issue_id, _claim_type, _key), members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda c: c["first_seen_ts"])
        canonical = members[0]
        duplicates = members[1:]
        groups_merged += 1
        for dup in duplicates:
            ws.update_claim_status(dup["id"], "superseded", actor="system", superseded_by=canonical["id"])
            ws.log_claim_event(
                dup["id"], "acknowledge", actor="system",
                note=f"merged into canonical claim #{canonical['id']} (canonical_key backfill)",
                raw_item_id=dup["raw_item_id"],
            )
            claims_absorbed += 1
        ws.touch_claim(canonical["id"], ts=max(c["last_seen_ts"] for c in members))
        merges.append({
            "issue_id": issue_id, "canonical_claim_id": canonical["id"],
            "absorbed_claim_ids": [c["id"] for c in duplicates],
        })

    return {
        "open_claims_scanned": len(claims),
        "canonical_keys_computed": computed,
        "groups_merged": groups_merged,
        "claims_absorbed": claims_absorbed,
        "merges": merges,
    }


def backfill_resolution_signal_suggestions(*, limit: int | None = None) -> dict:
    """One-time-then-daily sweep (task #155) for raw_items whose extraction
    already carries resolution_signals but were extracted (or re-extracted
    via a correction) before the live-wiring point in server_lean.py ran
    for them - same shape as backfill_claims. Idempotent: create_claim_
    suggestion's own (claim_id, evidence_type) dedupe means re-running
    this never grows duplicate pending suggestions."""
    raw_item_ids = ws.list_raw_item_ids_with_extractions()
    if limit is not None:
        raw_item_ids = raw_item_ids[:limit]
    matched = 0
    for rid in raw_item_ids:
        matched += workgraph_reconcile.generate_resolution_signal_suggestions(rid)
    return {"raw_items_scanned": len(raw_item_ids), "signals_matched": matched}


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
        "resolution_signal_suggestions": backfill_resolution_signal_suggestions(),
        "issue_closed_contradictions": workgraph_reconcile.detect_issue_closed_with_open_claims_contradictions(),
        "issues_appear_resolved": workgraph_reconcile.detect_issues_appear_resolved_but_still_open(),
        "stray_reference_clusters_merged": workgraph_reconcile.merge_stray_same_reference_clusters(),
        "stray_signature_confirmation_clusters_merged": workgraph_reconcile.merge_stray_signature_confirmation_clusters(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps({
        "claims": backfill_claims(),
        "evidence_fts": backfill_evidence_fts(),
    }, indent=2))
