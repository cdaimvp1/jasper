"""Tests for workgraph_claims_backfill.py (Section 9.9 steps 3-4): the
one-time-then-ongoing sweep that materializes claims from existing
extractions and indexes evidence_fts from resolved raw text."""
from __future__ import annotations

import json
import time

import workgraph_claims_backfill as backfill


def _issue(ws_db, title="Issue"):
    return ws_db.create_issue_with_new_id(title=title, state="active", category="other")


def _raw_item_with_extraction(ws_db, issue_id, key, extracted_json, body_preview=None, direction="outbound"):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
        body_preview=body_preview,
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    if extracted_json is not None:
        ws_db.create_extraction(rid, json.dumps(extracted_json))
    if direction is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET direction = ? WHERE id = ?", (direction, rid))
        conn.close()
    return rid


def test_backfill_claims_materializes_across_all_extracted_raw_items(ws_db):
    iid1 = _issue(ws_db, "One")
    iid2 = _issue(ws_db, "Two")
    _raw_item_with_extraction(ws_db, iid1, "bk1", {"asks": ["ask one"]})
    _raw_item_with_extraction(ws_db, iid2, "bk2", {"commitments": ["commitment two"]})

    stats = backfill.backfill_claims()

    assert stats["raw_items_scanned"] == 2
    assert stats["claims_inserted"] == 2
    assert stats["already_materialized"] == 0


def test_backfill_claims_is_idempotent(ws_db):
    iid = _issue(ws_db)
    _raw_item_with_extraction(ws_db, iid, "bk3", {"asks": ["ask"]})

    first = backfill.backfill_claims()
    second = backfill.backfill_claims()

    assert first["claims_inserted"] == 1
    assert second["claims_inserted"] == 0
    assert second["already_materialized"] == 1


def test_backfill_claims_counts_no_issue_id_case(ws_db):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="bk4", thread_key="bk4", dedupe_key="bk4",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.create_extraction(rid, json.dumps({"asks": ["orphaned"]}))

    stats = backfill.backfill_claims()

    assert stats["claims_inserted"] == 0
    assert stats["skipped_no_issue_id"] == 1


def test_backfill_claims_respects_limit(ws_db):
    iid = _issue(ws_db)
    _raw_item_with_extraction(ws_db, iid, "bk5", {"asks": ["a"]})
    _raw_item_with_extraction(ws_db, iid, "bk6", {"asks": ["b"]})

    stats = backfill.backfill_claims(limit=1)

    assert stats["raw_items_scanned"] == 1


def test_backfill_evidence_fts_indexes_body_preview_fallback(ws_db):
    iid = _issue(ws_db)
    _raw_item_with_extraction(ws_db, iid, "bk7", None, body_preview="a searchable renewal note")

    stats = backfill.backfill_evidence_fts()

    assert stats["indexed"] == 1
    results = ws_db.search_evidence_fts("renewal")
    assert len(results) == 1


def test_backfill_evidence_fts_skips_items_with_no_text(ws_db):
    iid = _issue(ws_db)
    _raw_item_with_extraction(ws_db, iid, "bk8", None, body_preview="")

    stats = backfill.backfill_evidence_fts()

    assert stats["skipped_empty_text"] == 1
    assert stats["indexed"] == 0


def test_run_backfill_daily_if_due_returns_none_on_second_call_same_day(ws_db, monkeypatch):
    import workgraph_claims_backfill as b
    monkeypatch.setattr(b, "ws", ws_db)

    first = b.run_backfill_daily_if_due(now=1785200000.0)
    second = b.run_backfill_daily_if_due(now=1785200000.0)

    assert first is not None
    assert "claims" in first and "evidence_fts" in first
    assert second is None
