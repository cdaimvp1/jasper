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


# --- backfill_canonical_keys_and_merge_duplicates (2026-08-04) -----------
#
# These simulate LEGACY data that predates canonical_key (two separate
# open claims, canonical_key=NULL on both) by inserting claims directly
# via ws_db.insert_claim rather than through materialize_claims_for_raw_
# item - which, after this same fix, now prevents this exact duplication
# from ever happening to NEW data (see test_workgraph_claims.py's own
# canonical-key-dedup-fallback tests for that live-path coverage). The
# backfill's job is specifically to clean up what already existed before
# the fix shipped.

def _legacy_duplicate_claim(ws_db, issue_id, key, reference, text, direction="inbound"):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = ?, direction = ? WHERE id = ?", (reference, direction, rid))
    conn.commit()
    conn.close()
    author = "counterparty" if direction == "inbound" else "marc"
    owner = "marc" if author == "counterparty" else "counterparty"
    claim_id = ws_db.insert_claim(
        issue_id=issue_id, raw_item_id=rid, claim_type="ask", text=text,
        author=author, author_basis="direction", owner=owner,
    )
    return rid, claim_id


def test_backfill_merges_pre_existing_duplicate_group_missed_by_repeat_signals(ws_db):
    """Simulates a real pre-existing duplicate group (both claims already
    materialized with NO canonical_key, matching data that predates this
    feature) - the backfill must compute canonical_key for both and merge
    them into one open claim."""
    iid = _issue(ws_db, "Disputed PR")
    _legacy_duplicate_claim(ws_db, iid, "dup1a", "PR1161567", "Please approve PR1161567 at your earliest convenience")
    _legacy_duplicate_claim(ws_db, iid, "dup1b", "PR1161567", "REMINDER: please approve PR1161567 - still pending")
    before = ws_db.list_open_claims_for_issue(iid, claim_type="ask")
    assert len(before) == 2  # confirmed real gap: both inserted, no canonical_key yet

    result = backfill.backfill_canonical_keys_and_merge_duplicates()

    assert result["groups_merged"] == 1
    assert result["claims_absorbed"] == 1
    after = ws_db.list_open_claims_for_issue(iid, claim_type="ask")
    assert len(after) == 1


def test_backfill_keeps_the_earliest_claim_as_canonical(ws_db):
    iid = _issue(ws_db, "Disputed PR 2")
    _rid_a, claim_a = _legacy_duplicate_claim(ws_db, iid, "dup2a", "PR1170816", "Please approve PR1170816")
    _rid_b, _claim_b = _legacy_duplicate_claim(ws_db, iid, "dup2b", "PR1170816", "REMINDER: approve PR1170816 please")

    result = backfill.backfill_canonical_keys_and_merge_duplicates()

    after = ws_db.list_open_claims_for_issue(iid, claim_type="ask")
    assert len(after) == 1
    assert after[0]["id"] == claim_a  # claim_a was inserted first, so it's the earliest by first_seen_ts
    assert result["merges"][0]["canonical_claim_id"] == claim_a


def test_backfill_is_idempotent_second_run_merges_nothing(ws_db):
    iid = _issue(ws_db, "Disputed PR 3")
    _legacy_duplicate_claim(ws_db, iid, "dup3a", "PR1169904", "Please approve PR1169904")
    _legacy_duplicate_claim(ws_db, iid, "dup3b", "PR1169904", "REMINDER: PR1169904 needs approval")

    first = backfill.backfill_canonical_keys_and_merge_duplicates()
    second = backfill.backfill_canonical_keys_and_merge_duplicates()

    assert first["groups_merged"] == 1
    assert second["groups_merged"] == 0
    assert second["claims_absorbed"] == 0


def test_backfill_does_not_merge_claims_with_disjoint_references(ws_db):
    iid = _issue(ws_db, "Two different POs")
    _legacy_duplicate_claim(ws_db, iid, "dup4a", "PR854779-V4", "Please approve PR854779-V4")
    _legacy_duplicate_claim(ws_db, iid, "dup4b", "PR854779-V4", "Please approve PR854779-V4 amendment")
    _legacy_duplicate_claim(ws_db, iid, "dup4c", "PR999999999", "Please approve PR999999999 as well")

    result = backfill.backfill_canonical_keys_and_merge_duplicates()

    after = ws_db.list_open_claims_for_issue(iid, claim_type="ask")
    assert len(after) == 2  # the PR854779-V4 pair merged; the disjoint PR999999999 claim stands alone
    assert result["groups_merged"] == 1
    assert result["claims_absorbed"] == 1


def test_backfill_absorbed_claim_marked_superseded_with_audit_trail(ws_db):
    iid = _issue(ws_db, "Audit trail")
    _rid_a, claim_a = _legacy_duplicate_claim(ws_db, iid, "dup5a", "PR1161567", "Please approve PR1161567")
    _rid_b, claim_b = _legacy_duplicate_claim(ws_db, iid, "dup5b", "PR1161567", "REMINDER: approve PR1161567")

    result = backfill.backfill_canonical_keys_and_merge_duplicates()

    canonical_id = result["merges"][0]["canonical_claim_id"]
    absorbed_ids = result["merges"][0]["absorbed_claim_ids"]
    assert canonical_id == claim_a
    assert absorbed_ids == [claim_b]
    events = ws_db.list_claim_events_for_claim(claim_b)
    assert any(e["event_type"] == "acknowledge" and str(canonical_id) in (e["note"] or "") for e in events)
    absorbed_claim = ws_db.get_claim(claim_b)
    assert absorbed_claim["status"] == "superseded"
    assert absorbed_claim["superseded_by"] == canonical_id


# --- resolve_authoritative_closure_signals (review point #2, 2026-08-11) --

def _raw_item_with_signal_type(ws_db, issue_id, key, signal_type):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", (signal_type, rid))
    conn.commit()
    conn.close()
    return rid


def _open_claim(ws_db, issue_id, raw_item_id, claim_type, text):
    return ws_db.insert_claim(
        issue_id=issue_id, raw_item_id=raw_item_id, claim_type=claim_type, text=text,
        author="marc", author_basis="direction", owner="counterparty",
    )


def test_resolve_authoritative_closure_signals_resolves_the_single_open_claim(ws_db):
    """External-review finding #359 (2026-08-12): the claim's own
    originating raw_item must carry the REAL request signal_type
    (ariba_pr_approval_needed) that REQUEST_TO_CLOSURE_SIGNAL maps to this
    closure (ariba_pr_fully_approved) - correlation, not just claim count."""
    iid = _issue(ws_db, "PR approval thread")
    ask_rid = _raw_item_with_signal_type(ws_db, iid, "ask1", "ariba_pr_approval_needed")
    claim_id = _open_claim(ws_db, iid, ask_rid, "ask", "please approve the PR")
    _raw_item_with_signal_type(ws_db, iid, "closure1", "ariba_pr_fully_approved")

    result = backfill.resolve_authoritative_closure_signals()

    assert result["auto_resolved"] == 1
    assert ws_db.get_claim(claim_id)["status"] == "done"


def test_resolve_authoritative_closure_signals_skips_when_uncorrelated(ws_db):
    """The exact real failure mode the review flagged: exactly one open
    claim exists on the issue, and a real closure signal lands on it, but
    the claim's own originating signal_type has no REQUEST_TO_CLOSURE_
    SIGNAL relationship to this specific closure - e.g. the one open claim
    is an unrelated "send Jane the status" ask (no signal_type at all),
    and a signature_fully_executed notification lands on the same issue.
    Must be left alone, never guessed as a match."""
    iid = _issue(ws_db, "Unrelated ask plus a real closure signal")
    ask_rid = _raw_item_with_extraction(ws_db, iid, "ask_unrelated", None)  # no signal_type at all
    claim_id = _open_claim(ws_db, iid, ask_rid, "ask", "send Jane the implementation status")
    _raw_item_with_signal_type(ws_db, iid, "closure_unrelated", "signature_fully_executed")

    result = backfill.resolve_authoritative_closure_signals()

    assert result["auto_resolved"] == 0
    assert result["skipped_uncorrelated"] == 1
    assert ws_db.get_claim(claim_id)["status"] == "open"


def test_resolve_authoritative_closure_signals_skips_when_correlated_to_a_different_closure_type(ws_db):
    """A claim whose own request signal_type DOES map to a real closure
    counterpart, but not THIS one, must still be left alone - e.g. a
    signature_requested ask claim sitting on an issue that also happens to
    receive an ariba_pr_fully_approved notification (a different real
    transaction's closure, not this claim's own)."""
    iid = _issue(ws_db, "Wrong closure type for this request")
    ask_rid = _raw_item_with_signal_type(ws_db, iid, "ask_sig", "signature_requested")
    claim_id = _open_claim(ws_db, iid, ask_rid, "ask", "please sign the agreement")
    _raw_item_with_signal_type(ws_db, iid, "closure_wrong_type", "ariba_pr_fully_approved")

    result = backfill.resolve_authoritative_closure_signals()

    assert result["auto_resolved"] == 0
    assert result["skipped_uncorrelated"] == 1
    assert ws_db.get_claim(claim_id)["status"] == "open"


def test_resolve_authoritative_closure_signals_skips_when_ambiguous(ws_db):
    iid = _issue(ws_db, "Multiple open claims")
    ask_rid = _raw_item_with_extraction(ws_db, iid, "ask2", None)
    claim_a = _open_claim(ws_db, iid, ask_rid, "ask", "please approve PR A")
    claim_b = _open_claim(ws_db, iid, ask_rid, "commitment", "will approve PR B")
    _raw_item_with_signal_type(ws_db, iid, "closure2", "signature_completed_docusign")

    result = backfill.resolve_authoritative_closure_signals()

    assert result["auto_resolved"] == 0
    assert result["skipped_ambiguous"] == 1
    assert ws_db.get_claim(claim_a)["status"] == "open"
    assert ws_db.get_claim(claim_b)["status"] == "open"


def test_resolve_authoritative_closure_signals_ignores_non_closure_signal_types(ws_db):
    """ariba_pr_approval_needed is 'actionable' treatment, not 'closure' -
    the whole point of tiering: an ask being outstanding is not evidence
    it was fulfilled."""
    iid = _issue(ws_db, "Approval needed")
    ask_rid = _raw_item_with_extraction(ws_db, iid, "ask3", None)
    claim_id = _open_claim(ws_db, iid, ask_rid, "ask", "please approve the PR")
    _raw_item_with_signal_type(ws_db, iid, "notclosure1", "ariba_pr_approval_needed")

    result = backfill.resolve_authoritative_closure_signals()

    assert result["auto_resolved"] == 0
    assert ws_db.get_claim(claim_id)["status"] == "open"


def test_resolve_authoritative_closure_signals_is_idempotent(ws_db):
    iid = _issue(ws_db, "Idempotent")
    ask_rid = _raw_item_with_signal_type(ws_db, iid, "ask4", "ariba_pr_approval_needed")
    _open_claim(ws_db, iid, ask_rid, "ask", "please approve")
    _raw_item_with_signal_type(ws_db, iid, "closure3", "ariba_pr_fully_approved")

    first = backfill.resolve_authoritative_closure_signals()
    second = backfill.resolve_authoritative_closure_signals()

    assert first["auto_resolved"] == 1
    assert second["auto_resolved"] == 0


def test_resolve_authoritative_closure_signals_skips_orphan_with_no_issue(ws_db):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="closure4", thread_key="closure4", dedupe_key="closure4",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", ("ariba_pr_fully_approved", rid))
    conn.commit()
    conn.close()

    result = backfill.resolve_authoritative_closure_signals()

    assert result["auto_resolved"] == 0
    assert result["skipped_no_issue_id"] == 1


def test_resolve_authoritative_closure_signals_respects_live_treatment_override(ws_db):
    """A live signal_treatment_override (Marc's Settings UI) demoting a
    signal_type away from 'closure' must be honored immediately, not just
    at original classification time."""
    iid = _issue(ws_db, "Override test")
    ask_rid = _raw_item_with_extraction(ws_db, iid, "ask5", None)
    claim_id = _open_claim(ws_db, iid, ask_rid, "ask", "please approve")
    _raw_item_with_signal_type(ws_db, iid, "closure5", "ariba_pr_fully_approved")
    ws_db.set_signal_treatment("ariba_pr_fully_approved", "fyi", reason="test override", set_by="marc")

    result = backfill.resolve_authoritative_closure_signals()

    assert result["auto_resolved"] == 0
    assert ws_db.get_claim(claim_id)["status"] == "open"


def test_resolve_authoritative_closure_signals_logs_claim_event(ws_db):
    iid = _issue(ws_db, "Audit trail for auto-resolve")
    ask_rid = _raw_item_with_signal_type(ws_db, iid, "ask6", "signature_requested")
    claim_id = _open_claim(ws_db, iid, ask_rid, "ask", "please approve")
    closure_rid = _raw_item_with_signal_type(ws_db, iid, "closure6", "signature_fully_executed")

    backfill.resolve_authoritative_closure_signals()

    events = ws_db.list_claim_events_for_claim(claim_id)
    assert any(e["event_type"] == "complete" and e["actor"] == "system" and e["raw_item_id"] == closure_rid
               for e in events)


def test_run_backfill_daily_if_due_returns_none_on_second_call_same_day(ws_db, monkeypatch):
    import workgraph_claims_backfill as b
    monkeypatch.setattr(b, "ws", ws_db)

    first = b.run_backfill_daily_if_due(now=1785200000.0)
    second = b.run_backfill_daily_if_due(now=1785200000.0)

    assert first is not None
    assert "claims" in first and "evidence_fts" in first
    assert second is None
