"""Tests for design doc Section 12.3's right-sized additions to the claims
ledger: claim_edges, the 5-event claim_events log (create/escalate/
acknowledge/complete/dismiss), update_claim_status, and the checklist-
action-to-claim status sync."""
from __future__ import annotations

import json
import time

import workgraph_claims as wc
import workgraph_store as ws


def _issue(ws_db, title="Issue"):
    return ws_db.create_issue_with_new_id(title=title, state="active", category="other")


def _raw_item(ws_db, issue_id, key, extracted_json, direction="inbound"):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps(extracted_json))
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET direction = ? WHERE id = ?", (direction, rid))
    conn.close()
    return rid


# --- claim_events: create ------------------------------------------------

def test_materialize_logs_a_create_event(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "r1", {"asks": ["approve this"]})
    wc.materialize_claims_for_raw_item(rid)

    claim = ws.list_open_claims_for_issue(iid)[0]
    events = ws.list_claim_events_for_claim(claim["id"])
    assert [e["event_type"] for e in events] == ["create"]
    assert events[0]["actor"] == "curator"


def test_date_claim_also_logs_create_event(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "r2", {"dates_mentioned": [{"text": "Friday", "kind": "hard"}]})
    wc.materialize_claims_for_raw_item(rid)

    claim = ws.list_open_claims_for_issue(iid, claim_type="date")[0]
    events = ws.list_claim_events_for_claim(claim["id"])
    assert [e["event_type"] for e in events] == ["create"]


# --- claim_events: escalate/acknowledge -----------------------------------

def test_repeat_signal_escalated_logs_escalate_event(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "r3", {"asks": ["please sign the SOW"]})
    wc.materialize_claims_for_raw_item(rid1)
    rid2 = _raw_item(ws_db, iid, "r4", {
        "asks": ["please sign the SOW"],
        "repeat_signals": [{"ask_text": "please sign the SOW", "escalated": True,
                             "escalation_note": "now from the manager"}],
    })
    wc.materialize_claims_for_raw_item(rid2)

    claim = ws.list_open_claims_for_issue(iid)[0]
    events = ws.list_claim_events_for_claim(claim["id"])
    assert [e["event_type"] for e in events] == ["create", "escalate"]
    assert events[1]["note"] == "now from the manager"


def test_repeat_signal_not_escalated_logs_acknowledge_event(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "r5", {"commitments": ["I'll send the PO"]})
    wc.materialize_claims_for_raw_item(rid1)
    rid2 = _raw_item(ws_db, iid, "r6", {
        "commitments": ["I'll send the PO"],
        "repeat_signals": [{"ask_text": "I'll send the PO", "escalated": False}],
    })
    wc.materialize_claims_for_raw_item(rid2)

    claim = ws.list_open_claims_for_issue(iid, claim_type="commitment")[0]
    events = ws.list_claim_events_for_claim(claim["id"])
    assert [e["event_type"] for e in events] == ["create", "acknowledge"]


# --- update_claim_status ---------------------------------------------

def test_update_claim_status_changes_status_and_bumps_revision(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "r7", {"asks": ["approve this"]})
    wc.materialize_claims_for_raw_item(rid)
    claim = ws.list_open_claims_for_issue(iid)[0]
    rev_before = ws.get_claims_revision(iid)

    ws.update_claim_status(claim["id"], "done", actor="marc")

    updated = ws.get_claim(claim["id"])
    assert updated["status"] == "done"
    assert ws.get_claims_revision(iid) > rev_before
    # a done claim no longer shows up in the open-claims list
    assert ws.list_open_claims_for_issue(iid) == []


def test_update_claim_status_rejects_invalid_status(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "r8", {"asks": ["approve this"]})
    wc.materialize_claims_for_raw_item(rid)
    claim = ws.list_open_claims_for_issue(iid)[0]

    import pytest
    with pytest.raises(ValueError):
        ws.update_claim_status(claim["id"], "not-a-real-status", actor="marc")


# --- checklist -> claim status sync -----------------------------------

def test_sync_checklist_done_marks_matching_claim_done(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "r9", {"asks": ["approve requisition PR123"]})
    wc.materialize_claims_for_raw_item(rid)

    synced = wc.sync_checklist_action_to_claim(
        issue_id=iid, kind="ask", text="approve requisition PR123", status="done", actor="marc",
    )

    assert synced is True
    claim = ws.list_open_claims_for_issue(iid)
    assert claim == []  # no longer open


def test_sync_checklist_dismissed_marks_matching_claim_dismissed(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "r10", {"decisions": ["going with vendor B"]})
    wc.materialize_claims_for_raw_item(rid)

    synced = wc.sync_checklist_action_to_claim(
        issue_id=iid, kind="decision", text="going with vendor B", status="dismissed", actor="marc",
    )

    assert synced is True
    conn = ws._connect()
    row = conn.execute("SELECT status FROM claims WHERE issue_id = ?", (iid,)).fetchone()
    conn.close()
    assert row["status"] == "dismissed"


def test_sync_checklist_logs_the_right_event_type(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "r11", {"commitments": ["I'll send the redline"]})
    wc.materialize_claims_for_raw_item(rid)
    claim = ws.list_open_claims_for_issue(iid)[0]

    wc.sync_checklist_action_to_claim(
        issue_id=iid, kind="commitment", text="I'll send the redline", status="done", actor="marc",
    )

    events = ws.list_claim_events_for_claim(claim["id"])
    assert [e["event_type"] for e in events] == ["create", "complete"]


def test_sync_checklist_no_op_for_key_facts_kind(ws_db):
    """key_facts were never claims - kind=None (or anything not a real
    claim_type) is a silent, expected no-op, never an error."""
    iid = _issue(ws_db)
    assert wc.sync_checklist_action_to_claim(
        issue_id=iid, kind=None, text="some key fact", status="dismissed", actor="marc",
    ) is False


def test_sync_checklist_no_op_when_no_matching_claim_exists(ws_db):
    iid = _issue(ws_db)
    assert wc.sync_checklist_action_to_claim(
        issue_id=iid, kind="ask", text="an ask that was never materialized", status="done", actor="marc",
    ) is False


# --- claim_edges -------------------------------------------------------

def test_create_and_list_claim_edge(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "r12", {"asks": ["ask one"]})
    rid2 = _raw_item(ws_db, iid, "r13", {"asks": ["ask two"]})
    wc.materialize_claims_for_raw_item(rid1)
    wc.materialize_claims_for_raw_item(rid2)
    claims = ws.list_open_claims_for_issue(iid)
    a, b = claims[0]["id"], claims[1]["id"]

    edge_id = ws.create_claim_edge(a, b, "contradicts", actor="marc")
    assert edge_id is not None

    edges_a = ws.list_claim_edges_for_claim(a)
    edges_b = ws.list_claim_edges_for_claim(b)
    assert len(edges_a) == 1
    assert len(edges_b) == 1  # found from either side
    assert edges_a[0]["edge_type"] == "contradicts"


def test_claim_edge_rejects_invalid_type(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "r14", {"asks": ["ask one"]})
    rid2 = _raw_item(ws_db, iid, "r15", {"asks": ["ask two"]})
    wc.materialize_claims_for_raw_item(rid1)
    wc.materialize_claims_for_raw_item(rid2)
    claims = ws.list_open_claims_for_issue(iid)

    import pytest
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        ws.create_claim_edge(claims[0]["id"], claims[1]["id"], "not_a_real_type", actor="marc")
