"""Tests for workgraph_claims.py (design doc Section 9, Phase 3): claims
materialization, deterministic author/owner resolution, repeat_signals-driven
dedup, idempotency, and the claims_revision counter."""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

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


# --- basic materialization -------------------------------------------------

def test_materializes_one_claim_per_field_entry(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "k1", {
        "asks": ["please send the SOW"],
        "decisions": ["going with vendor B"],
        "commitments": ["I'll send it Friday"],
        "dates_mentioned": [{"text": "Friday", "kind": "hard"}],
    }, direction="outbound")

    inserted = wc.materialize_claims_for_raw_item(rid)

    assert inserted == 4
    claims = wc.list_open_claims_for_issue(iid)
    assert {c["claim_type"] for c in claims} == {"ask", "decision", "commitment", "date"}


def test_skips_blank_and_non_string_entries(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "k2", {"asks": ["", "   ", None, 7, "a real ask"]}, direction="outbound")

    wc.materialize_claims_for_raw_item(rid)

    claims = wc.list_open_claims_for_issue(iid, claim_type="ask")
    assert [c["text"] for c in claims] == ["a real ask"]


def test_noop_when_no_extraction_exists(ws_db):
    iid = _issue(ws_db)
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k3", thread_key="k3", dedupe_key="k3",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, iid)

    assert wc.materialize_claims_for_raw_item(rid) == 0
    assert wc.list_open_claims_for_issue(iid) == []


def test_noop_when_raw_item_has_no_issue_id(ws_db):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k4", thread_key="k4", dedupe_key="k4",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.create_extraction(rid, json.dumps({"asks": ["orphaned ask"]}))

    assert wc.materialize_claims_for_raw_item(rid) == 0


# --- idempotency -------------------------------------------------------

def test_materialize_is_idempotent(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "k5", {"asks": ["send the invoice"]}, direction="outbound")

    first = wc.materialize_claims_for_raw_item(rid)
    second = wc.materialize_claims_for_raw_item(rid)

    assert first == 1
    assert second == 0
    assert len(wc.list_open_claims_for_issue(iid)) == 1


# --- author resolution (Section 9.4: deterministic, never a keyword guess) -

def test_author_outbound_is_marc(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "k6", {"commitments": ["I'll send the PO"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid)[0]
    assert claim["author"] == "marc"
    assert claim["author_basis"] == "direction"


def test_author_inbound_is_counterparty(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "k7", {"commitments": ["I'll send the PO"]}, direction="inbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid)[0]
    assert claim["author"] == "counterparty"


def test_author_unknown_when_no_direction(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "k8", {"commitments": ["I'll send the PO"]})
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid)[0]
    assert claim["author"] == "unknown"
    assert claim["author_basis"] == "unresolved"


def test_author_internal_is_unknown(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "k9", {"commitments": ["noted internally"]}, direction="internal")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid)[0]
    assert claim["author"] == "unknown"


# --- owner derivation (Section 9.4) -------------------------------------

def test_commitment_owner_is_the_author(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "o1", {"commitments": ["I'll send it"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    assert wc.list_open_claims_for_issue(iid)[0]["owner"] == "marc"


def test_ask_owner_is_the_other_side_from_outbound_author(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "o2", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    assert wc.list_open_claims_for_issue(iid)[0]["owner"] == "counterparty"


def test_ask_owner_is_marc_when_counterparty_asks(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "o3", {"asks": ["can you approve this"]}, direction="inbound")
    wc.materialize_claims_for_raw_item(rid)
    assert wc.list_open_claims_for_issue(iid)[0]["owner"] == "marc"


def test_decision_owner_is_null(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "o4", {"decisions": ["going with vendor B"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    assert wc.list_open_claims_for_issue(iid)[0]["owner"] is None


def test_date_owner_from_whose_field(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "o5", {
        "dates_mentioned": [{"text": "due Friday", "kind": "hard", "whose": "counterparty"}]
    }, direction="inbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="date")[0]
    assert claim["owner"] == "counterparty"
    assert claim["date_kind"] == "hard"


def test_date_owner_unknown_when_whose_missing(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "o6", {"dates_mentioned": [{"text": "due Friday", "kind": "soft"}]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="date")[0]
    assert claim["owner"] == "unknown"


# --- repeat_signals dedup (Section 9.3) ---------------------------------

def test_repeat_signal_updates_existing_open_ask_instead_of_duplicating(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "r1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)

    rid2 = _raw_item(ws_db, iid, "r2", {
        "asks": ["please send the SOW"],
        "repeat_signals": [{"ask_text": "please send the SOW", "days_since_first_ask": 3,
                             "escalated": True, "escalation_note": "third time asking"}],
    }, direction="outbound")
    inserted = wc.materialize_claims_for_raw_item(rid2)

    claims = wc.list_open_claims_for_issue(iid, claim_type="ask")
    assert inserted == 0
    assert len(claims) == 1
    assert claims[0]["escalated"] == 1
    assert claims[0]["escalation_note"] == "third time asking"


def test_ask_without_matching_repeat_signal_inserts_new_claim(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "r3", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)

    rid2 = _raw_item(ws_db, iid, "r4", {"asks": ["please also send the MSA"]}, direction="outbound")
    inserted = wc.materialize_claims_for_raw_item(rid2)

    assert inserted == 1
    assert len(wc.list_open_claims_for_issue(iid, claim_type="ask")) == 2


# --- canonical_key_for_claim (2026-08-04, canonical claim dedup) -----------

def test_canonical_key_prefers_structured_reference():
    key = wc.canonical_key_for_claim("ask", "Please approve PR1161567 by Friday", "counterparty", "PR1161567")
    assert key == "ask|approve|PR1161567|counterparty"


def test_canonical_key_falls_back_to_generic_action_family_when_no_keyword_matches():
    key = wc.canonical_key_for_claim("ask", "Please look into this soon", "counterparty", "PR1161567")
    assert key == "ask|generic|PR1161567|counterparty"


def test_canonical_key_without_reference_uses_conservative_normalization():
    key = wc.canonical_key_for_claim("ask", "Please send the signed SOW by Friday", None, None)
    assert key is not None
    assert key.startswith("ask|text:")
    assert "send" in key and "signed" in key and "sow" in key and "friday" in key


def test_canonical_key_strips_greeting_and_reminder_boilerplate_only():
    with_boilerplate = wc.canonical_key_for_claim(
        "ask", "Hi Marc, this is a friendly reminder: please send the signed SOW. Thanks!", None, None)
    without_boilerplate = wc.canonical_key_for_claim("ask", "please send the signed SOW", None, None)
    assert with_boilerplate == without_boilerplate


def test_canonical_key_preserves_negation_numbers_and_amounts():
    key = wc.canonical_key_for_claim("ask", "The $50,000 invoice was NOT approved on 2026-08-01", None, None)
    assert "not" in key
    assert "50" in key and "000" in key
    assert "2026" in key


def test_canonical_key_returns_none_for_too_short_normalized_text():
    assert wc.canonical_key_for_claim("ask", "ok thanks", None, None) is None


def test_canonical_key_different_reference_ids_never_collapse():
    key_a = wc.canonical_key_for_claim("ask", "Please approve this PO", "counterparty", "PR1000001")
    key_b = wc.canonical_key_for_claim("ask", "Please approve this PO", "counterparty", "PR2000002")
    assert key_a != key_b


# --- materialize_claims_for_raw_item: canonical_key dedup fallback --------

def test_canonical_key_dedup_fires_when_repeat_signals_misses_reference_reminder(ws_db):
    """Real production shape: the SAME Ariba PR reminder re-sent with
    DIFFERENT wording (so byte-exact repeat_signals never matches), but
    around the identical reference ID - must dedup via canonical_key, not
    insert a second open claim."""
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "cd1", {"asks": ["Please approve PR1161567 at your earliest convenience"]},
                      direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR1161567' WHERE id = ?", (rid1,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid1)

    rid2 = _raw_item(ws_db, iid, "cd2", {"asks": ["REMINDER: please approve PR1161567 - still pending"]},
                      direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR1161567' WHERE id = ?", (rid2,))
    conn.close()
    inserted = wc.materialize_claims_for_raw_item(rid2)

    claims = wc.list_open_claims_for_issue(iid, claim_type="ask")
    assert inserted == 0
    assert len(claims) == 1


def test_canonical_key_dedup_does_not_downgrade_existing_escalation(ws_db):
    """A claim already escalated=1 via a real repeat_signals hit must NOT
    be silently reset to escalated=0 just because a LATER repeat lands via
    the canonical_key fallback instead (which carries no escalation info
    of its own)."""
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "esc1", {"asks": ["Please approve PR1161567"]}, direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR1161567' WHERE id = ?", (rid1,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid1)

    rid2 = _raw_item(ws_db, iid, "esc2", {
        "asks": ["Please approve PR1161567"],
        "repeat_signals": [{"ask_text": "Please approve PR1161567", "days_since_first_ask": 3,
                             "escalated": True, "escalation_note": "third time asking"}],
    }, direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR1161567' WHERE id = ?", (rid2,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid2)

    rid3 = _raw_item(ws_db, iid, "esc3", {"asks": ["REMINDER: PR1161567 still needs approval"]}, direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR1161567' WHERE id = ?", (rid3,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid3)

    claims = wc.list_open_claims_for_issue(iid, claim_type="ask")
    assert len(claims) == 1
    assert claims[0]["escalated"] == 1
    assert claims[0]["escalation_note"] == "third time asking"


def test_new_claim_gets_a_canonical_key_set_on_insert(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "ck1", {"asks": ["Please approve PR1161567"]}, direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR1161567' WHERE id = ?", (rid,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid)

    claims = wc.list_open_claims_for_issue(iid, claim_type="ask")
    assert claims[0]["canonical_key"] == "ask|approve|PR1161567|marc"


def test_claim_events_record_raw_item_id_provenance_on_create_and_touch(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "prov1", {"asks": ["Please approve PR1161567"]}, direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR1161567' WHERE id = ?", (rid1,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid1)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]

    rid2 = _raw_item(ws_db, iid, "prov2", {"asks": ["REMINDER: approve PR1161567"]}, direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR1161567' WHERE id = ?", (rid2,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid2)

    events = ws_db.list_claim_events_for_claim(claim["id"])
    raw_item_ids = [e["raw_item_id"] for e in events]
    assert rid1 in raw_item_ids
    assert rid2 in raw_item_ids


# --- corrected-extraction reconciliation (2026-08-04) ---------------------

def _reextract(ws_db, rid, extracted_json):
    """Overwrites raw_item_id's extraction (create_extraction is an
    UPSERT) - simulates curator re-running extraction on the SAME
    raw_item after a correction, which changes content_hash."""
    ws_db.create_extraction(rid, json.dumps(extracted_json))


def test_first_materialization_marks_extraction_materialized(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)

    extraction = ws_db.get_extraction(rid)
    assert extraction["materialized_hash"] == extraction["content_hash"]


def test_unchanged_extraction_is_a_true_noop_on_rerun(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec2", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)

    second = wc.materialize_claims_for_raw_item(rid)

    assert second == 0
    assert len(wc.list_open_claims_for_issue(iid, claim_type="ask")) == 1


def test_correction_adds_a_new_claim(ws_db):
    """Addition: the corrected extraction has an extra ask the first pass
    never saw."""
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec3", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)

    _reextract(ws_db, rid, {"asks": ["please send the SOW", "please also send the MSA"]})
    inserted = wc.materialize_claims_for_raw_item(rid)

    assert inserted == 1
    claims = {c["text"] for c in wc.list_open_claims_for_issue(iid, claim_type="ask")}
    assert claims == {"please send the SOW", "please also send the MSA"}


def test_correction_removes_a_claim_as_superseded_not_done(ws_db):
    """Deletion: the corrected extraction drops an ask that never really
    existed - the old claim must be marked superseded (an extraction
    correction), never 'done' (completed real-world work)."""
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec4", {"asks": ["please send the SOW", "please send the MSA"]},
                     direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    removed_claim = next(c for c in wc.list_open_claims_for_issue(iid, claim_type="ask")
                          if c["text"] == "please send the MSA")

    _reextract(ws_db, rid, {"asks": ["please send the SOW"]})
    wc.materialize_claims_for_raw_item(rid)

    open_claims = wc.list_open_claims_for_issue(iid, claim_type="ask")
    assert [c["text"] for c in open_claims] == ["please send the SOW"]
    removed = ws_db.get_claim(removed_claim["id"])
    assert removed["status"] == "superseded"
    events = ws_db.list_claim_events_for_claim(removed_claim["id"])
    assert any(e["event_type"] == "dismiss" and "not completed real-world work" in (e["note"] or "")
               for e in events)


def test_correction_wording_change_supersedes_old_and_inserts_new(ws_db):
    """Wording correction: no fuzzy matching, so a changed wording shows
    up as one remove + one add - the old, wrongly-worded claim is
    superseded, and a new claim with the corrected text is inserted."""
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec5", {"asks": ["please send the sow"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    old_claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]

    _reextract(ws_db, rid, {"asks": ["please send the SIGNED sow by Friday"]})
    inserted = wc.materialize_claims_for_raw_item(rid)

    assert inserted == 1
    open_claims = wc.list_open_claims_for_issue(iid, claim_type="ask")
    assert len(open_claims) == 1
    assert open_claims[0]["text"] == "please send the SIGNED sow by Friday"
    assert ws_db.get_claim(old_claim["id"])["status"] == "superseded"
    # Fix 3 (2026-08-11): a plain wording change on the one unambiguous
    # old->new pair (no owner/date/dollar difference) logs as 'refined'.
    events = ws_db.list_claim_events_for_claim(old_claim["id"])
    assert any(e["event_type"] == "refined" for e in events)


def test_correction_owner_change_supersedes_and_reinserts(ws_db):
    """Owner/type change: correcting the extraction's claim_type (e.g. a
    decision that was really an ask) or direction (which flips owner) has
    no reliable 1:1 mapping to the old row either - same remove+add
    treatment."""
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec6", {"decisions": ["going with vendor B"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    old_decision = wc.list_open_claims_for_issue(iid, claim_type="decision")[0]

    _reextract(ws_db, rid, {"asks": ["going with vendor B"]})
    inserted = wc.materialize_claims_for_raw_item(rid)

    assert inserted == 1
    assert wc.list_open_claims_for_issue(iid, claim_type="decision") == []
    new_ask = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    assert new_ask["text"] == "going with vendor B"
    assert ws_db.get_claim(old_decision["id"])["status"] == "superseded"
    # Fix 3 (2026-08-11): decision (owner=None) -> ask (owner=counterparty)
    # is a real owner change by this diff's own rule, even though the
    # underlying correction was a claim_type fix, not owner reassignment.
    events = ws_db.list_claim_events_for_claim(old_decision["id"])
    assert any(e["event_type"] == "owner_changed" for e in events)


def test_correction_dollar_figure_change_logs_monetary_changed(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec-money", {"commitments": ["I'll pay $50,000 for this"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    old_claim = wc.list_open_claims_for_issue(iid, claim_type="commitment")[0]

    _reextract(ws_db, rid, {"commitments": ["I'll pay $75,000 for this"]})
    wc.materialize_claims_for_raw_item(rid)

    assert ws_db.get_claim(old_claim["id"])["status"] == "superseded"
    events = ws_db.list_claim_events_for_claim(old_claim["id"])
    assert any(e["event_type"] == "monetary_changed" for e in events)


def test_correction_date_claim_change_logs_timing_changed(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec-timing",
                     {"dates_mentioned": [{"text": "due Friday", "kind": "hard"}]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    old_claim = wc.list_open_claims_for_issue(iid, claim_type="date")[0]

    _reextract(ws_db, rid, {"dates_mentioned": [{"text": "due next Monday instead", "kind": "hard"}]})
    wc.materialize_claims_for_raw_item(rid)

    assert ws_db.get_claim(old_claim["id"])["status"] == "superseded"
    events = ws_db.list_claim_events_for_claim(old_claim["id"])
    assert any(e["event_type"] == "timing_changed" for e in events)


def test_classify_refinement_owner_beats_timing_and_monetary(ws_db):
    """Unit-level: owner_changed wins even when the same pair would also
    qualify for timing_changed/monetary_changed - see _classify_refinement's
    own docstring on why owner comes first."""
    old_claim = {"claim_type": "date", "owner": "marc", "text": "pay $50,000 by Friday"}
    new_spec = {"claim_type": "date", "owner": "counterparty", "text": "pay $75,000 by Monday"}
    assert wc._classify_refinement(old_claim, new_spec) == "owner_changed"


def test_classify_refinement_plain_wording_change_is_refined(ws_db):
    old_claim = {"claim_type": "ask", "owner": "counterparty", "text": "send the sow"}
    new_spec = {"claim_type": "ask", "owner": "counterparty", "text": "send the SIGNED sow"}
    assert wc._classify_refinement(old_claim, new_spec) == "refined"


def test_correction_is_idempotent_second_rerun_is_noop(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec7", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    _reextract(ws_db, rid, {"asks": ["please send the signed SOW"]})

    first = wc.materialize_claims_for_raw_item(rid)
    second = wc.materialize_claims_for_raw_item(rid)

    assert first == 1
    assert second == 0
    assert len(wc.list_open_claims_for_issue(iid, claim_type="ask")) == 1


def test_correction_leaves_already_resolved_claim_untouched(ws_db):
    """A claim a real human action already resolved (done/dismissed)
    before the correction landed must never be silently reopened OR
    re-touched by a later extraction diff."""
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec8", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    resolved_claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    ws_db.update_claim_status(resolved_claim["id"], "done", actor="marc")

    _reextract(ws_db, rid, {"asks": ["a completely different ask"]})
    wc.materialize_claims_for_raw_item(rid)

    untouched = ws_db.get_claim(resolved_claim["id"])
    assert untouched["status"] == "done"
    new_claims = wc.list_open_claims_for_issue(iid, claim_type="ask")
    assert [c["text"] for c in new_claims] == ["a completely different ask"]


def test_correction_only_key_facts_change_marks_materialized_with_no_claim_writes(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec9", {"asks": ["please send the SOW"], "key_facts": ["old fact"]},
                     direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    rev_before = ws_db.get_claims_revision(iid)

    _reextract(ws_db, rid, {"asks": ["please send the SOW"], "key_facts": ["a brand new fact"]})
    inserted = wc.materialize_claims_for_raw_item(rid)

    assert inserted == 0
    assert len(wc.list_open_claims_for_issue(iid, claim_type="ask")) == 1
    assert ws_db.get_claims_revision(iid) > rev_before  # the new key fact still bumps revision
    extraction = ws_db.get_extraction(rid)
    assert extraction["materialized_hash"] == extraction["content_hash"]


def test_reconciliation_rolls_back_completely_on_failure(ws_db):
    """Forces a REAL sqlite3 constraint violation partway through
    reconcile_extraction_claims's own transaction (a second to_insert spec
    with an invalid claim_type, violating the claims.claim_type CHECK
    constraint) instead of mocking the connection layer - wrapping
    _connect() with even a fully transparent passthrough proxy was found
    to corrupt unrelated, already-committed data on the raw on-disk file
    in ways real SQLite semantics never would, so this exercises the
    actual rollback path with a genuine failure rather than a simulated
    one."""
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rec10", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    extraction_before = ws_db.get_extraction(rid)

    valid_spec = {
        "claim_type": "ask", "text": "please also send the MSA", "owner": None,
        "date_kind": None, "canonical_key": None, "author": "counterparty",
        "author_basis": "direction",
    }
    invalid_spec = {
        "claim_type": "not_a_real_claim_type", "text": "this insert must fail", "owner": None,
        "date_kind": None, "canonical_key": None, "author": "counterparty",
        "author_basis": "direction",
    }

    with pytest.raises(sqlite3.IntegrityError):
        ws_db.reconcile_extraction_claims(
            issue_id=iid, raw_item_id=rid, to_insert=[valid_spec, invalid_spec],
            to_supersede=[], new_materialized_hash="fake-hash-that-must-not-stick",
        )

    # The failed attempt must have rolled back completely - no orphaned
    # insert from the valid spec that ran (and would otherwise have
    # committed) before the invalid one failed.
    open_claims = ws_db.list_open_claims_for_issue(iid, claim_type="ask")
    assert [c["text"] for c in open_claims] == ["please send the SOW"]
    extraction = ws_db.get_extraction(rid)
    assert extraction["materialized_hash"] == extraction_before["materialized_hash"]
    assert extraction["materialized_hash"] != "fake-hash-that-must-not-stick"

    # A subsequent, valid call must still succeed cleanly and completely -
    # the failed attempt didn't leave anything half-applied behind.
    ws_db.reconcile_extraction_claims(
        issue_id=iid, raw_item_id=rid, to_insert=[valid_spec], to_supersede=[],
        new_materialized_hash="a-real-hash",
    )
    open_claims_after = ws_db.list_open_claims_for_issue(iid, claim_type="ask")
    assert {c["text"] for c in open_claims_after} == {"please send the SOW", "please also send the MSA"}


# --- claims_revision (Section 9.5) --------------------------------------

def test_claims_revision_bumps_on_insert(ws_db):
    iid = _issue(ws_db)
    assert ws_db.get_claims_revision(iid) == 0
    rid = _raw_item(ws_db, iid, "rev1", {"asks": ["a"], "decisions": ["b"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    assert ws_db.get_claims_revision(iid) == 2


def test_claims_revision_bumps_on_touch(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "rev2", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    rev_after_insert = ws_db.get_claims_revision(iid)

    rid2 = _raw_item(ws_db, iid, "rev3", {
        "asks": ["please send the SOW"],
        "repeat_signals": [{"ask_text": "please send the SOW", "escalated": False}],
    }, direction="outbound")
    wc.materialize_claims_for_raw_item(rid2)

    assert ws_db.get_claims_revision(iid) > rev_after_insert


# --- batched reader ------------------------------------------------------

def test_list_open_claims_for_issues_batched(ws_db):
    iid1 = _issue(ws_db, "One")
    iid2 = _issue(ws_db, "Two")
    rid1 = _raw_item(ws_db, iid1, "b1", {"asks": ["ask one"]}, direction="outbound")
    rid2 = _raw_item(ws_db, iid2, "b2", {"asks": ["ask two"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    wc.materialize_claims_for_raw_item(rid2)

    batched = wc.list_open_claims_for_issues([iid1, iid2])

    assert [c["text"] for c in batched[iid1]] == ["ask one"]
    assert [c["text"] for c in batched[iid2]] == ["ask two"]


def test_list_open_claims_for_issues_empty_list(ws_db):
    assert wc.list_open_claims_for_issues([]) == {}


# --- find_duplicate_or_conflicting_asks_across_project (task #140, E17) ----

def _issue_in_project(ws_db, pid, title):
    iid = _issue(ws_db, title)
    ws_db.assign_issue_to_project(iid, pid)
    return iid


def test_conflicting_ask_across_project_is_flagged(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    a = _issue_in_project(ws_db, pid, "A")
    b = _issue_in_project(ws_db, pid, "B")
    rid_a = _raw_item(ws_db, a, "ca1", {"asks": ["Approve requisition PR854779-V4 for $3,876,200.00"]},
                       direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR854779' WHERE id = ?", (rid_a,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid_a)
    rid_b = _raw_item(ws_db, b, "ca2", {"asks": ["Approve requisition PR854779-V4 for $1,938,100.00"]},
                       direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR854779' WHERE id = ?", (rid_b,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid_b)

    groups = wc.find_duplicate_or_conflicting_asks_across_project()

    assert len(groups) == 1
    group = groups[0]
    assert group["project_id"] == pid
    assert group["verdict"] == "conflicting"
    assert {c["issue_id"] for c in group["claims"]} == {a, b}


def test_duplicate_ask_across_project_is_flagged(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    a = _issue_in_project(ws_db, pid, "A")
    b = _issue_in_project(ws_db, pid, "B")
    for iid, key in ((a, "da1"), (b, "da2")):
        rid = _raw_item(ws_db, iid, key, {"asks": ["Approve requisition PR1112223"]}, direction="inbound")
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET pr_number_base = 'PR1112223' WHERE id = ?", (rid,))
        conn.close()
        wc.materialize_claims_for_raw_item(rid)

    groups = wc.find_duplicate_or_conflicting_asks_across_project()

    assert len(groups) == 1
    assert groups[0]["verdict"] == "duplicate"


def test_same_canonical_key_on_unrelated_issues_not_flagged(ws_db):
    """No shared project - two separate, ungrouped matters that happen to
    reference the same PR is not this feature's job (it's a project-
    grouping question, not a claim-dedup one)."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    for iid, key in ((a, "un1"), (b, "un2")):
        rid = _raw_item(ws_db, iid, key, {"asks": ["Approve requisition PR3334445"]}, direction="inbound")
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET pr_number_base = 'PR3334445' WHERE id = ?", (rid,))
        conn.close()
        wc.materialize_claims_for_raw_item(rid)

    assert wc.find_duplicate_or_conflicting_asks_across_project() == []


def test_closed_issue_excluded_from_duplicate_ask_sweep(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    a = _issue_in_project(ws_db, pid, "A")
    b = _issue_in_project(ws_db, pid, "B")
    for iid, key in ((a, "cl1"), (b, "cl2")):
        rid = _raw_item(ws_db, iid, key, {"asks": ["Approve requisition PR5556667"]}, direction="inbound")
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET pr_number_base = 'PR5556667' WHERE id = ?", (rid,))
        conn.close()
        wc.materialize_claims_for_raw_item(rid)
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET state = 'done' WHERE id = ?", (b,))
    conn.close()

    assert wc.find_duplicate_or_conflicting_asks_across_project() == []


def test_single_issue_with_only_one_open_claim_not_flagged(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    a = _issue_in_project(ws_db, pid, "A")
    rid = _raw_item(ws_db, a, "so1", {"asks": ["Approve requisition PR7778889"]}, direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR7778889' WHERE id = ?", (rid,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid)

    assert wc.find_duplicate_or_conflicting_asks_across_project() == []


# --- reopen suggestion on a resolved-claim reoccurrence (task #304, item #5)

def test_reoccurring_ask_after_resolution_creates_a_reopen_suggestion(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "reopen1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    original = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    ws_db.update_claim_status(original["id"], "done", actor="marc")

    rid2 = _raw_item(ws_db, iid, "reopen2", {"asks": ["please send the SOW"]}, direction="outbound")
    inserted = wc.materialize_claims_for_raw_item(rid2)

    assert inserted == 1  # the fresh claim still gets created, real new content
    suggestions = ws_db.list_pending_claim_suggestions(iid)
    reopen_suggestions = [s for s in suggestions if s["suggestion_kind"] == "reopen"]
    assert len(reopen_suggestions) == 1
    assert reopen_suggestions[0]["claim_id"] == original["id"]
    assert reopen_suggestions[0]["evidence_type"] == "resolved_claim_reoccurred"


def test_reoccurring_ask_while_still_open_creates_no_reopen_suggestion(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "stillopen1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)

    rid2 = _raw_item(ws_db, iid, "stillopen2", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid2)

    suggestions = ws_db.list_pending_claim_suggestions(iid)
    assert [s for s in suggestions if s["suggestion_kind"] == "reopen"] == []


def test_reopen_suggestion_dedupes_against_a_second_reoccurrence(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "dedup1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    original = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    ws_db.update_claim_status(original["id"], "done", actor="marc")

    rid2 = _raw_item(ws_db, iid, "dedup2", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid2)
    rid3 = _raw_item(ws_db, iid, "dedup3", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid3)

    suggestions = ws_db.list_pending_claim_suggestions(iid)
    reopen_suggestions = [s for s in suggestions if s["suggestion_kind"] == "reopen"]
    assert len(reopen_suggestions) == 1


# --- explicit-completion suggestion (task #319, scoped to suggest-only per
# Marc's explicit 2026-08-11 call after review) -----------------------------
# resolution_signals (task #155) already carries curator's judgment that a
# raw_item's content directly names a specific earlier open claim as
# fulfilled - workgraph_reconcile.generate_resolution_signal_suggestions
# already turned that into a suggest-only pending_claim_suggestion via
# byte-exact text matching alone. _resolve_explicit_completions' real,
# still-real value-add is a WIDER match (byte-exact, or - new - a
# reference+action-family canonical_key match) over the same signals, fed
# into the identical suggestion queue via ws.create_claim_suggestion - never
# a direct status change. An initial version of this function auto-resolved
# directly; Marc's explicit review call was that every other claim-closing
# path in this codebase requires a human confirm first, and a rare false
# structural match silently closing a real open commitment was a worse
# failure mode than one extra confirm click - so this stays suggest-only,
# same as everything else in workgraph_reconcile.py.

def test_resolution_signal_creates_suggestion_for_matching_open_claim_by_exact_text(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "comp1", {"commitments": ["I'll send the signed SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    claim = wc.list_open_claims_for_issue(iid, claim_type="commitment")[0]

    rid2 = _raw_item(ws_db, iid, "comp2", {
        "resolution_signals": [{"claim_type": "commitment", "claim_text": "I'll send the signed SOW",
                                 "resolution_note": "signed SOW attached"}],
    }, direction="outbound")
    wc.materialize_claims_for_raw_item(rid2)

    # Suggest-only: the claim itself is untouched until a human confirms.
    assert ws_db.get_claim(claim["id"])["status"] == "open"
    suggestions = ws_db.list_pending_claim_suggestions(iid)
    assert len(suggestions) == 1
    assert suggestions[0]["claim_id"] == claim["id"]
    assert suggestions[0]["suggestion_kind"] == "resolve"
    assert suggestions[0]["evidence_type"] == "explicit_resolution_signal"
    assert suggestions[0]["raw_item_id"] == rid2


def test_resolution_signal_creates_suggestion_via_canonical_key_reference_fallback(ws_db):
    """The real gap fix: a resolution message that shares the SAME
    structured reference (a PR number) as the original ask, but doesn't
    reproduce its text byte-for-byte - byte-exact find_open_claim_by_text
    (and so generate_resolution_signal_suggestions alone) would miss this;
    the reference+action-family canonical_key prefix match must still find
    it and queue a suggestion, same as the byte-exact case."""
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "canon-comp1",
                      {"asks": ["Please approve PR1161567 at your earliest convenience"]}, direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR1161567' WHERE id = ?", (rid1,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid1)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]

    rid2 = _raw_item(ws_db, iid, "canon-comp2", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "Please approve PR1161567 as soon as possible",
                                 "resolution_note": "approved in Ariba"}],
    }, direction="inbound")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR1161567' WHERE id = ?", (rid2,))
    conn.close()
    wc.materialize_claims_for_raw_item(rid2)

    assert ws_db.get_claim(claim["id"])["status"] == "open"
    suggestions = ws_db.list_pending_claim_suggestions(iid)
    assert len(suggestions) == 1
    assert suggestions[0]["claim_id"] == claim["id"]


def test_resolution_signal_without_structural_match_leaves_claim_open(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "nomatch1", {"commitments": ["I'll send the signed SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    claim = wc.list_open_claims_for_issue(iid, claim_type="commitment")[0]

    rid2 = _raw_item(ws_db, iid, "nomatch2", {
        "resolution_signals": [{"claim_type": "commitment", "claim_text": "a totally unrelated paraphrase",
                                 "resolution_note": "?"}],
    }, direction="outbound")
    wc.materialize_claims_for_raw_item(rid2)

    assert ws_db.get_claim(claim["id"])["status"] == "open"
    assert ws_db.list_pending_claim_suggestions(iid) == []


def test_resolution_signal_suggestion_is_not_duplicated_by_the_other_call_site(ws_db):
    """Harmony check: workgraph_reconcile.generate_resolution_signal_
    suggestions still runs off the same resolution_signals field (a
    separate call site, e.g. server_lean.py's ingest handler) using its
    own byte-exact-only match. Both paths target the identical (claim_id,
    evidence_type) pair for a byte-exact case, and ws.create_claim_
    suggestion's own dedupe-then-insert means only ONE pending suggestion
    ever exists regardless of which call site runs first or whether both
    run - never a duplicate row for the same signal."""
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "harmony1", {"commitments": ["I'll send the signed SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)

    rid2 = _raw_item(ws_db, iid, "harmony2", {
        "resolution_signals": [{"claim_type": "commitment", "claim_text": "I'll send the signed SOW",
                                 "resolution_note": "signed SOW attached"}],
    }, direction="outbound")
    wc.materialize_claims_for_raw_item(rid2)  # creates the suggestion via _resolve_explicit_completions
    wr.generate_resolution_signal_suggestions(rid2)  # same signal, byte-exact path - must not duplicate

    assert len(ws_db.list_pending_claim_suggestions(iid)) == 1


# --- project_links dependency-signal writer (task #319) --------------------
# project_links already has the right shape (from/to project id + a
# link_type enum covering depends_on/blocks/enables) but had zero writer
# anywhere - _write_project_link_signals is that writer, driven by a new
# curator-extraction field (dependency_signals, SYNTHESIS_ROUTINE.md).

def test_dependency_signal_writes_a_project_link(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="New H1 Deal")
    proj_b = ws_db.create_project_with_new_id(name="Old H1 Exit")
    iid = _issue_in_project(ws_db, proj_a, "New deal signature")
    rid = _raw_item(ws_db, iid, "dep1", {
        "dependency_signals": [{"relationship": "depends_on", "target_project_id": proj_b,
                                 "reason": "needs the old contract exited first"}],
    }, direction="inbound")

    wc.materialize_claims_for_raw_item(rid)

    links = ws_db.list_project_links_for_project(proj_a)
    assert len(links) == 1
    assert links[0]["from_project_id"] == proj_a
    assert links[0]["to_project_id"] == proj_b
    assert links[0]["link_type"] == "depends_on"
    assert links[0]["created_by"] == "claims_dependency_signal"


def test_dependency_signal_is_idempotent_across_two_raw_items(ws_db):
    """The same detected relationship, restated on a second raw_item on the
    same issue, must not create a second project_links row."""
    proj_a = ws_db.create_project_with_new_id(name="New H1 Deal")
    proj_b = ws_db.create_project_with_new_id(name="Old H1 Exit")
    iid = _issue_in_project(ws_db, proj_a, "New deal signature")
    signal = {"relationship": "depends_on", "target_project_id": proj_b, "reason": "needs the old contract first"}

    rid1 = _raw_item(ws_db, iid, "dep-idem1", {"dependency_signals": [signal]}, direction="inbound")
    wc.materialize_claims_for_raw_item(rid1)
    rid2 = _raw_item(ws_db, iid, "dep-idem2", {"dependency_signals": [signal]}, direction="inbound")
    wc.materialize_claims_for_raw_item(rid2)

    assert len(ws_db.list_project_links_for_project(proj_a)) == 1


def test_dependency_signal_skipped_when_issue_has_no_project(ws_db):
    proj_b = ws_db.create_project_with_new_id(name="Old H1 Exit")
    iid = _issue(ws_db)  # deliberately not grouped into any project
    rid = _raw_item(ws_db, iid, "dep-noproj", {
        "dependency_signals": [{"relationship": "depends_on", "target_project_id": proj_b, "reason": "x"}],
    }, direction="inbound")

    wc.materialize_claims_for_raw_item(rid)

    assert ws_db.list_project_links_for_project(proj_b) == []


def test_dependency_signal_skipped_when_target_project_is_not_real(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="New H1 Deal")
    iid = _issue_in_project(ws_db, proj_a, "New deal signature")
    rid = _raw_item(ws_db, iid, "dep-fake", {
        "dependency_signals": [{"relationship": "depends_on", "target_project_id": "proj-does-not-exist",
                                 "reason": "x"}],
    }, direction="inbound")

    wc.materialize_claims_for_raw_item(rid)

    assert ws_db.list_project_links_for_project(proj_a) == []


def test_dependency_signal_skipped_for_unrecognized_relationship(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="A")
    proj_b = ws_db.create_project_with_new_id(name="B")
    iid = _issue_in_project(ws_db, proj_a, "Issue")
    rid = _raw_item(ws_db, iid, "dep-badrel", {
        "dependency_signals": [{"relationship": "same_supplier", "target_project_id": proj_b, "reason": "x"}],
    }, direction="inbound")

    wc.materialize_claims_for_raw_item(rid)

    assert ws_db.list_project_links_for_project(proj_a) == []
