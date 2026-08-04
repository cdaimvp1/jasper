"""Tests for workgraph_reconcile.py (task #155): claim-resolution
suggestions - suggest-only, never auto-close. Covers both evidence types
(explicit_resolution_signal, issue_closed_with_open_claims), the confirm/
reject lifecycle, and dedup against duplicate pending suggestions."""
from __future__ import annotations

import json
import time

import workgraph_claims as wc
import workgraph_reconcile as wr


def _issue(ws_db, title="Issue", state="active"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category="other")


def _raw_item(ws_db, issue_id, key, extracted_json, direction=None):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps(extracted_json))
    if direction is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET direction = ? WHERE id = ?", (direction, rid))
        conn.close()
    return rid


def _set_issue_state(ws_db, issue_id, state):
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET state = ? WHERE id = ?", (state, issue_id))
    conn.close()


# --- explicit_resolution_signal ---------------------------------------------

def test_explicit_resolution_signal_creates_a_resolve_suggestion(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "sig1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]

    rid2 = _raw_item(ws_db, iid, "sig2", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "please send the SOW",
                                 "resolution_note": "signed SOW attached"}],
    }, direction="inbound")
    matched = wr.generate_resolution_signal_suggestions(rid2)

    assert matched == 1
    suggestions = ws_db.list_pending_claim_suggestions(issue_id=iid)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["claim_id"] == claim["id"]
    assert s["suggestion_kind"] == "resolve"
    assert s["evidence_type"] == "explicit_resolution_signal"
    assert s["evidence_note"] == "signed SOW attached"
    assert s["raw_item_id"] == rid2
    assert s["status"] == "pending"


def test_resolution_signal_for_nonexistent_claim_is_skipped(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "sig3", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "an ask that was never made",
                                 "resolution_note": "n/a"}],
    }, direction="inbound")

    matched = wr.generate_resolution_signal_suggestions(rid)

    assert matched == 0
    assert ws_db.list_pending_claim_suggestions(issue_id=iid) == []


def test_resolution_signal_dedupes_against_existing_pending_suggestion(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "sig4", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    rid2 = _raw_item(ws_db, iid, "sig5", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "please send the SOW",
                                 "resolution_note": "first mention"}],
    }, direction="inbound")

    wr.generate_resolution_signal_suggestions(rid2)
    wr.generate_resolution_signal_suggestions(rid2)  # re-run, e.g. a repeated backfill sweep

    assert len(ws_db.list_pending_claim_suggestions(issue_id=iid)) == 1


def test_resolution_signal_with_no_extraction_field_is_a_noop(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "sig6", {"asks": ["please send the SOW"]}, direction="outbound")

    assert wr.generate_resolution_signal_suggestions(rid) == 0


# --- confirm / reject lifecycle ---------------------------------------------

def test_confirm_resolve_suggestion_marks_claim_done_and_logs_event(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "conf1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    rid2 = _raw_item(ws_db, iid, "conf2", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "please send the SOW",
                                 "resolution_note": "attached"}],
    }, direction="inbound")
    wr.generate_resolution_signal_suggestions(rid2)
    suggestion = ws_db.list_pending_claim_suggestions(issue_id=iid)[0]

    ok = wr.confirm_claim_suggestion(suggestion["id"], actor="marc")

    assert ok is True
    assert ws_db.get_claim(claim["id"])["status"] == "done"
    events = ws_db.list_claim_events_for_claim(claim["id"])
    assert any(e["event_type"] == "complete" and "claim-resolution suggestion" in (e["note"] or "")
               for e in events)
    assert ws_db.get_claim_suggestion(suggestion["id"])["status"] == "confirmed"


def test_reject_resolve_suggestion_leaves_claim_open(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "rej1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    rid2 = _raw_item(ws_db, iid, "rej2", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "please send the SOW"}],
    }, direction="inbound")
    wr.generate_resolution_signal_suggestions(rid2)
    suggestion = ws_db.list_pending_claim_suggestions(issue_id=iid)[0]

    ok = wr.reject_claim_suggestion(suggestion["id"], actor="marc")

    assert ok is True
    assert ws_db.get_claim(claim["id"])["status"] == "open"
    assert ws_db.get_claim_suggestion(suggestion["id"])["status"] == "rejected"


def test_confirm_or_reject_on_an_already_resolved_suggestion_returns_false(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "twice1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    rid2 = _raw_item(ws_db, iid, "twice2", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "please send the SOW"}],
    }, direction="inbound")
    wr.generate_resolution_signal_suggestions(rid2)
    suggestion = ws_db.list_pending_claim_suggestions(issue_id=iid)[0]
    wr.confirm_claim_suggestion(suggestion["id"], actor="marc")

    assert wr.confirm_claim_suggestion(suggestion["id"], actor="marc") is False
    assert wr.reject_claim_suggestion(suggestion["id"], actor="marc") is False


def test_confirm_on_unknown_suggestion_id_returns_false(ws_db):
    assert wr.confirm_claim_suggestion(999999, actor="marc") is False


# --- issue_closed_with_open_claims (contradiction) --------------------------

def test_issue_closed_with_open_claims_creates_a_contradiction_suggestion(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "closed1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    _set_issue_state(ws_db, iid, "done")

    result = wr.detect_issue_closed_with_open_claims_contradictions()

    assert result["suggestions_created"] >= 1
    suggestions = ws_db.list_pending_claim_suggestions(issue_id=iid)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["claim_id"] == claim["id"]
    assert s["suggestion_kind"] == "contradiction"
    assert s["evidence_type"] == "issue_closed_with_open_claims"


def test_confirming_a_contradiction_suggestion_never_touches_the_claim(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "closed2", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    _set_issue_state(ws_db, iid, "done")
    wr.detect_issue_closed_with_open_claims_contradictions()
    suggestion = ws_db.list_pending_claim_suggestions(issue_id=iid)[0]

    ok = wr.confirm_claim_suggestion(suggestion["id"], actor="marc")

    assert ok is True
    assert ws_db.get_claim(claim["id"])["status"] == "open"  # never inferred done
    assert ws_db.get_claim_suggestion(suggestion["id"])["status"] == "confirmed"


def test_open_issue_with_open_claims_creates_no_contradiction(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "open1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)

    result = wr.detect_issue_closed_with_open_claims_contradictions()

    assert ws_db.list_pending_claim_suggestions(issue_id=iid) == []
    assert result["issues_scanned"] == 0


def test_issue_closed_sweep_is_idempotent_no_duplicate_suggestions(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "closed3", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    _set_issue_state(ws_db, iid, "done")

    wr.detect_issue_closed_with_open_claims_contradictions()
    wr.detect_issue_closed_with_open_claims_contradictions()

    assert len(ws_db.list_pending_claim_suggestions(issue_id=iid)) == 1


def test_issue_closed_with_a_resolved_claim_creates_no_contradiction(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "closed4", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    ws_db.update_claim_status(claim["id"], "done", actor="marc")
    _set_issue_state(ws_db, iid, "done")

    wr.detect_issue_closed_with_open_claims_contradictions()

    assert ws_db.list_pending_claim_suggestions(issue_id=iid) == []
