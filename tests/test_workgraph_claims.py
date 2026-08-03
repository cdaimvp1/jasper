"""Tests for workgraph_claims.py (design doc Section 9, Phase 3): claims
materialization, deterministic author/owner resolution, repeat_signals-driven
dedup, idempotency, and the claims_revision counter."""
from __future__ import annotations

import json
import time

import workgraph_claims as wc


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
