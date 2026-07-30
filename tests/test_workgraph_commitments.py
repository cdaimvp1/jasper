"""Regression tests for workgraph_commitments.py (task #73, Commitments
Tracker). Surfaces raw_item_extractions' commitments field verbatim -
never attributed to Marc specifically (real data checked before building
this: only 5 of 79 real commitments even mention Marc by name), never
sorted/filtered by anything but extraction recency."""
from __future__ import annotations

import json
import time

import workgraph_commitments as wc


def _issue_with_commitments(ws_db, title, key, commitments, extracted_ts=None):
    issue_id = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({"commitments": commitments}))
    if extracted_ts is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE raw_item_extractions SET extracted_ts = ? WHERE raw_item_id = ?", (extracted_ts, rid))
        conn.close()
    return issue_id


def test_list_open_commitments_empty_when_none_exist(ws_db):
    assert wc.list_open_commitments() == []


def test_list_open_commitments_surfaces_text_verbatim(ws_db):
    iid = _issue_with_commitments(ws_db, "Renewal", "c1", ["Brian will send the signed WO by Friday"])

    entries = wc.list_open_commitments()

    assert len(entries) == 1
    assert entries[0]["issue_id"] == iid
    assert entries[0]["text"] == "Brian will send the signed WO by Friday"


def test_list_open_commitments_skips_blank_and_non_string_entries(ws_db):
    _issue_with_commitments(ws_db, "Mixed", "c2", ["", "   ", None, 7, "a real commitment"])

    entries = wc.list_open_commitments()

    assert [e["text"] for e in entries] == ["a real commitment"]


def test_list_open_commitments_excludes_closed_issues(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Closed deal", state="done", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="c3", thread_key="c3", dedupe_key="c3",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({"commitments": ["should not appear"]}))

    assert wc.list_open_commitments() == []


def test_list_open_commitments_sorted_most_recently_extracted_first(ws_db):
    now = time.time()
    _issue_with_commitments(ws_db, "Older", "c4", ["older commitment"], extracted_ts=now - 500)
    _issue_with_commitments(ws_db, "Newer", "c5", ["newer commitment"], extracted_ts=now)

    entries = wc.list_open_commitments()

    assert [e["text"] for e in entries] == ["newer commitment", "older commitment"]


def test_list_open_commitments_multiple_per_issue_all_included(ws_db):
    _issue_with_commitments(ws_db, "Busy thread", "c6", ["first commitment", "second commitment"])

    entries = wc.list_open_commitments()

    assert {e["text"] for e in entries} == {"first commitment", "second commitment"}
