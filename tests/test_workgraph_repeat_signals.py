"""Regression tests for workgraph_repeat_signals.py, rewritten 2026-08-03
(Section 9.8) for the claims-backed contract: a repeat signal now only has
meaning as a real repeat TOUCH on an existing open claim (Section 9.3), not
free-floating metadata read verbatim off an extraction blob. A raw_item
whose `repeat_signals` names an `ask_text` with no matching open ask on the
issue is simply new information, not a repeat - nothing to surface here."""
from __future__ import annotations

import json
import time

import workgraph_claims
import workgraph_repeat_signals as wrs


def _issue(ws_db, title="Issue"):
    return ws_db.create_issue_with_new_id(title=title, state="active", category="other")


def _raw_item(ws_db, issue_id, key, extracted_json, direction="outbound"):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps(extracted_json))
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET direction = ? WHERE id = ?", (direction, rid))
    conn.close()
    workgraph_claims.materialize_claims_for_raw_item(rid)
    return rid


def _ask_then_repeat(ws_db, issue_id, first_key, second_key, text, escalated=False, escalation_note=None):
    _raw_item(ws_db, issue_id, first_key, {"asks": [text]})
    signal = {"ask_text": text, "days_since_first_ask": 6, "escalated": escalated}
    if escalation_note is not None:
        signal["escalation_note"] = escalation_note
    _raw_item(ws_db, issue_id, second_key, {"asks": [text], "repeat_signals": [signal]})


def test_list_repeat_signals_empty_when_none_exist(ws_db):
    iid = _issue(ws_db)
    _raw_item(ws_db, iid, "rs1", {"asks": ["a fresh ask"]})
    assert wrs.list_repeat_signals_for_issue(iid) == []


def test_first_occurrence_alone_is_not_a_repeat(ws_db):
    """A claim that's only ever been seen once (first_seen_ts == last_seen_ts)
    was never touched as a repeat - nothing to surface."""
    iid = _issue(ws_db)
    _raw_item(ws_db, iid, "rs2", {"asks": ["please sign the SOW"]})
    assert wrs.list_repeat_signals_for_issue(iid) == []


def test_real_repeat_surfaces_escalated_true_and_note(ws_db):
    iid = _issue(ws_db)
    _ask_then_repeat(ws_db, iid, "rs3a", "rs3b", "please sign the SOW",
                      escalated=True, escalation_note="now from the requester's manager")

    entries = wrs.list_repeat_signals_for_issue(iid)

    assert len(entries) == 1
    assert entries[0]["ask_text"] == "please sign the SOW"
    assert entries[0]["escalated"] is True
    assert entries[0]["escalation_note"] == "now from the requester's manager"
    assert entries[0]["days_since_first_ask"] >= 0


def test_real_repeat_defaults_escalated_false_and_note_none(ws_db):
    iid = _issue(ws_db)
    _ask_then_repeat(ws_db, iid, "rs4a", "rs4b", "please sign the SOW")

    entries = wrs.list_repeat_signals_for_issue(iid)

    assert entries[0]["escalated"] is False
    assert entries[0]["escalation_note"] is None


def test_repeat_scoped_to_one_issue(ws_db):
    iid1 = _issue(ws_db, "Mine")
    iid2 = _issue(ws_db, "Other")
    _ask_then_repeat(ws_db, iid1, "rs5a", "rs5b", "mine")
    _ask_then_repeat(ws_db, iid2, "rs6a", "rs6b", "not mine")

    entries = wrs.list_repeat_signals_for_issue(iid1)

    assert [e["ask_text"] for e in entries] == ["mine"]


def test_repeat_works_for_commitments_and_decisions_too(ws_db):
    """Section 9.3's real gap fix: repeat_signals now covers commitments and
    decisions, not just asks."""
    iid = _issue(ws_db)
    _raw_item(ws_db, iid, "rs7a", {"commitments": ["I'll send the PO"]})
    _raw_item(ws_db, iid, "rs7b", {
        "commitments": ["I'll send the PO"],
        "repeat_signals": [{"ask_text": "I'll send the PO", "escalated": False}],
    })

    entries = wrs.list_repeat_signals_for_issue(iid)

    assert [e["ask_text"] for e in entries] == ["I'll send the PO"]


def test_repeat_signals_non_list_field_does_not_crash(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "rs8", {"asks": ["an ask"], "repeat_signals": "not a list"})
    assert ws_db.has_claims_for_raw_item(rid)
    assert wrs.list_repeat_signals_for_issue(iid) == []


def test_repeat_signals_non_dict_entry_does_not_match(ws_db):
    iid = _issue(ws_db)
    _raw_item(ws_db, iid, "rs9a", {"asks": ["an ask"]})
    _raw_item(ws_db, iid, "rs9b", {"asks": ["an ask"], "repeat_signals": ["just a string", 5]})

    assert wrs.list_repeat_signals_for_issue(iid) == []


# --- N+1 fix (2026-08-02, carried over to the claims-backed path): batched
# list_repeat_signals_for_issues() must match calling the singular version
# once per issue, but via one fetch. ---

def test_list_repeat_signals_for_issues_batched_matches_per_issue_calls(ws_db):
    iid1 = _issue(ws_db, "One")
    iid2 = _issue(ws_db, "Two")
    iid3 = _issue(ws_db, "None")
    _ask_then_repeat(ws_db, iid1, "b1a", "b1b", "signal one")
    _ask_then_repeat(ws_db, iid2, "b2a", "b2b", "signal two", escalated=True)

    batched = wrs.list_repeat_signals_for_issues([iid1, iid2, iid3])

    assert batched[iid1] == wrs.list_repeat_signals_for_issue(iid1)
    assert batched[iid2] == wrs.list_repeat_signals_for_issue(iid2)
    assert batched[iid3] == []


def test_list_repeat_signals_for_issues_empty_list_returns_empty_dict(ws_db):
    assert wrs.list_repeat_signals_for_issues([]) == {}
