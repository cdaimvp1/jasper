"""Regression tests for workgraph_key_facts.py (enhancement #2, Key Facts
panel). Same shape/pattern as workgraph_commitments.py - surfaces
raw_item_extractions' key_facts field verbatim, sorted only by extraction
recency."""
from __future__ import annotations

import json
import time

import workgraph_key_facts as wkf


def _issue_with_facts(ws_db, title, key, facts, extracted_ts=None):
    issue_id = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({"key_facts": facts}))
    if extracted_ts is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE raw_item_extractions SET extracted_ts = ? WHERE raw_item_id = ?", (extracted_ts, rid))
        conn.close()
    return issue_id


def test_list_open_key_facts_empty_when_none_exist(ws_db):
    assert wkf.list_open_key_facts() == []


def test_list_open_key_facts_surfaces_text_verbatim(ws_db):
    iid = _issue_with_facts(ws_db, "Renewal", "k1", ["Contract value is $2.4M over 3 years"])

    entries = wkf.list_open_key_facts()

    assert len(entries) == 1
    assert entries[0]["issue_id"] == iid
    assert entries[0]["text"] == "Contract value is $2.4M over 3 years"


def test_list_open_key_facts_skips_blank_and_non_string_entries(ws_db):
    _issue_with_facts(ws_db, "Mixed", "k2", ["", "   ", None, 7, "a real fact"])

    entries = wkf.list_open_key_facts()

    assert [e["text"] for e in entries] == ["a real fact"]


def test_list_open_key_facts_excludes_closed_issues(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Closed deal", state="done", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k3", thread_key="k3", dedupe_key="k3",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({"key_facts": ["should not appear"]}))

    assert wkf.list_open_key_facts() == []


def test_list_open_key_facts_sorted_most_recently_extracted_first(ws_db):
    now = time.time()
    _issue_with_facts(ws_db, "Older", "k4", ["older fact"], extracted_ts=now - 500)
    _issue_with_facts(ws_db, "Newer", "k5", ["newer fact"], extracted_ts=now)

    entries = wkf.list_open_key_facts()

    assert [e["text"] for e in entries] == ["newer fact", "older fact"]


def test_list_open_key_facts_multiple_per_issue_all_included(ws_db):
    _issue_with_facts(ws_db, "Busy thread", "k6", ["first fact", "second fact"])

    entries = wkf.list_open_key_facts()

    assert {e["text"] for e in entries} == {"first fact", "second fact"}


# --- enhancement #87: per-issue scoping (issue detail panel) -------------

def test_list_key_facts_for_issue_scoped_to_one_issue(ws_db):
    iid = _issue_with_facts(ws_db, "Renewal", "pk1", ["Contract value is $2.4M"])
    _issue_with_facts(ws_db, "Other", "pk2", ["unrelated fact"])

    assert wkf.list_key_facts_for_issue(iid) == ["Contract value is $2.4M"]


def test_list_key_facts_for_issue_includes_closed_issues(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Closed deal", state="done", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="pk3", thread_key="pk3", dedupe_key="pk3",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({"key_facts": ["closed-issue fact"]}))

    assert wkf.list_key_facts_for_issue(issue_id) == ["closed-issue fact"]


def test_list_key_facts_for_issue_empty_when_none(ws_db):
    iid = ws_db.create_issue_with_new_id(title="No facts", state="active", category="other")

    assert wkf.list_key_facts_for_issue(iid) == []


# --- hardening pass #3: malformed extracted_json shape must not crash ----

def test_list_open_key_facts_non_list_field_value_ignored(ws_db):
    _issue_with_facts(ws_db, "Malformed int", "m1", 5)
    iid = _issue_with_facts(ws_db, "Real one", "m2", ["a real fact"])

    entries = wkf.list_open_key_facts()

    assert [e["text"] for e in entries] == ["a real fact"]
    assert entries[0]["issue_id"] == iid


def test_list_key_facts_for_issue_non_list_field_value_ignored(ws_db):
    iid = _issue_with_facts(ws_db, "Malformed", "m3", "not a list")

    assert wkf.list_key_facts_for_issue(iid) == []


# --- N+1 fix (2026-08-02): batched list_key_facts_for_issues() must match
# calling the singular version once per issue, but via one extractions fetch.

def test_list_key_facts_for_issues_batched_matches_per_issue_calls(ws_db):
    iid1 = _issue_with_facts(ws_db, "One", "b1", ["fact one"])
    iid2 = _issue_with_facts(ws_db, "Two", "b2", ["fact two", "fact two-b"])
    iid3 = ws_db.create_issue_with_new_id(title="No facts", state="active", category="other")

    batched = wkf.list_key_facts_for_issues([iid1, iid2, iid3])

    assert batched[iid1] == wkf.list_key_facts_for_issue(iid1) == ["fact one"]
    assert set(batched[iid2]) == set(wkf.list_key_facts_for_issue(iid2)) == {"fact two", "fact two-b"}
    assert batched[iid3] == []


def test_list_key_facts_for_issues_empty_list_returns_empty_dict(ws_db):
    assert wkf.list_key_facts_for_issues([]) == {}


def test_list_key_facts_for_issues_does_not_leak_across_issues(ws_db):
    iid1 = _issue_with_facts(ws_db, "Good", "b3", ["real fact"])
    iid2 = _issue_with_facts(ws_db, "Bad", "b4", "not a list")

    batched = wkf.list_key_facts_for_issues([iid1, iid2])

    assert batched[iid1] == ["real fact"]
    assert batched[iid2] == []
