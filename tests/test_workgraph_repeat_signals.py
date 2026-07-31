"""Regression tests for workgraph_repeat_signals.py (Part D of the grouping/
NBA redesign): surfaces raw_item_extractions' `repeat_signals` field,
per-issue, verbatim, with defensive handling for malformed data."""
from __future__ import annotations

import json
import time

import workgraph_repeat_signals as wrs


def _issue_with_extraction(ws_db, title, key, extracted_json, extracted_ts=None):
    issue_id = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps(extracted_json))
    if extracted_ts is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE raw_item_extractions SET extracted_ts = ? WHERE raw_item_id = ?", (extracted_ts, rid))
        conn.close()
    return issue_id


def test_list_repeat_signals_empty_when_none_exist(ws_db):
    iid = _issue_with_extraction(ws_db, "No repeats", "rs1", {"asks": ["a fresh ask"]})
    assert wrs.list_repeat_signals_for_issue(iid) == []


def test_list_repeat_signals_surfaces_full_shape(ws_db):
    iid = _issue_with_extraction(ws_db, "Reminder", "rs2", {
        "repeat_signals": [{"ask_text": "please sign the SOW", "days_since_first_ask": 6,
                             "escalated": True, "escalation_note": "now from the requester's manager"}],
    })
    entries = wrs.list_repeat_signals_for_issue(iid)
    assert len(entries) == 1
    assert entries[0]["ask_text"] == "please sign the SOW"
    assert entries[0]["days_since_first_ask"] == 6
    assert entries[0]["escalated"] is True
    assert entries[0]["escalation_note"] == "now from the requester's manager"


def test_list_repeat_signals_defaults_escalated_false_and_note_none(ws_db):
    iid = _issue_with_extraction(ws_db, "Reminder", "rs3", {
        "repeat_signals": [{"ask_text": "please sign the SOW", "days_since_first_ask": 3}],
    })
    entries = wrs.list_repeat_signals_for_issue(iid)
    assert entries[0]["escalated"] is False
    assert entries[0]["escalation_note"] is None


def test_list_repeat_signals_scoped_to_one_issue(ws_db):
    iid = _issue_with_extraction(ws_db, "Mine", "rs4", {
        "repeat_signals": [{"ask_text": "mine", "days_since_first_ask": 1}],
    })
    _issue_with_extraction(ws_db, "Other", "rs5", {
        "repeat_signals": [{"ask_text": "not mine", "days_since_first_ask": 1}],
    })
    entries = wrs.list_repeat_signals_for_issue(iid)
    assert [e["ask_text"] for e in entries] == ["mine"]


def test_list_repeat_signals_sorted_most_recently_extracted_first(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Multi", state="active", category="other")
    for key, ts, text in (("rs6", now - 500, "older"), ("rs7", now, "newer")):
        rid = ws_db.insert_raw_item(source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
                                     occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                                     participants_json="[]")
        ws_db.link_raw_item_to_issue(rid, iid)
        ws_db.create_extraction(rid, json.dumps({"repeat_signals": [{"ask_text": text, "days_since_first_ask": 1}]}))
        conn = ws_db._connect()
        conn.execute("UPDATE raw_item_extractions SET extracted_ts = ? WHERE raw_item_id = ?", (ts, rid))
        conn.close()

    entries = wrs.list_repeat_signals_for_issue(iid)
    assert [e["ask_text"] for e in entries] == ["newer", "older"]


def test_list_repeat_signals_non_list_field_ignored(ws_db):
    iid = _issue_with_extraction(ws_db, "Malformed", "rs8", {"repeat_signals": "not a list"})
    assert wrs.list_repeat_signals_for_issue(iid) == []


def test_list_repeat_signals_non_dict_entry_skipped(ws_db):
    iid = _issue_with_extraction(ws_db, "Malformed entry", "rs9", {"repeat_signals": ["just a string", 5]})
    assert wrs.list_repeat_signals_for_issue(iid) == []


def test_list_repeat_signals_missing_ask_text_skipped(ws_db):
    iid = _issue_with_extraction(ws_db, "No ask_text", "rs10", {
        "repeat_signals": [{"days_since_first_ask": 1}],
    })
    assert wrs.list_repeat_signals_for_issue(iid) == []


def test_list_repeat_signals_malformed_days_field_ignored_not_crashed(ws_db):
    iid = _issue_with_extraction(ws_db, "Bad days", "rs11", {
        "repeat_signals": [{"ask_text": "an ask", "days_since_first_ask": "not a number"}],
    })
    entries = wrs.list_repeat_signals_for_issue(iid)
    assert entries[0]["days_since_first_ask"] is None
