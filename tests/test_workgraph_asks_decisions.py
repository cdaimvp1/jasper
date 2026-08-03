"""Regression tests for workgraph_asks_decisions.py (enhancement #2, Asks
& Decisions Tracker). Same shape/pattern as workgraph_commitments.py -
surfaces raw_item_extractions' asks/decisions fields verbatim, sorted only
by extraction recency."""
from __future__ import annotations

import json
import time

import workgraph_asks_decisions as wad
import workgraph_claims


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
    # These readers surface `claims` (Section 9.8), materialized from the
    # extraction just like the live extraction-write route does.
    workgraph_claims.materialize_claims_for_raw_item(rid)
    return issue_id


def test_list_open_asks_empty_when_none_exist(ws_db):
    assert wad.list_open_asks() == []


def test_list_open_decisions_empty_when_none_exist(ws_db):
    assert wad.list_open_decisions() == []


def test_list_open_asks_surfaces_text_verbatim(ws_db):
    iid = _issue_with_extraction(ws_db, "Renewal", "a1", {"asks": ["Please review the redline by Friday"]})

    entries = wad.list_open_asks()

    assert len(entries) == 1
    assert entries[0]["issue_id"] == iid
    assert entries[0]["text"] == "Please review the redline by Friday"


def test_list_open_decisions_surfaces_text_verbatim(ws_db):
    iid = _issue_with_extraction(ws_db, "Renewal", "d1", {"decisions": ["Legal approves the quote, no concerns"]})

    entries = wad.list_open_decisions()

    assert len(entries) == 1
    assert entries[0]["issue_id"] == iid
    assert entries[0]["text"] == "Legal approves the quote, no concerns"


def test_asks_and_decisions_are_independent_fields(ws_db):
    """The same extraction row can carry both - each tracker only ever
    surfaces its own field, never the other's."""
    _issue_with_extraction(ws_db, "Both", "ad1", {
        "asks": ["Can you confirm the budget line"],
        "decisions": ["Approved for FY27"],
    })

    assert [e["text"] for e in wad.list_open_asks()] == ["Can you confirm the budget line"]
    assert [e["text"] for e in wad.list_open_decisions()] == ["Approved for FY27"]


def test_list_open_asks_skips_blank_and_non_string_entries(ws_db):
    _issue_with_extraction(ws_db, "Mixed", "a2", {"asks": ["", "   ", None, 7, "a real ask"]})

    entries = wad.list_open_asks()

    assert [e["text"] for e in entries] == ["a real ask"]


def test_list_open_decisions_excludes_closed_issues(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Closed deal", state="done", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="d2", thread_key="d2", dedupe_key="d2",
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({"decisions": ["should not appear"]}))
    workgraph_claims.materialize_claims_for_raw_item(rid)

    assert wad.list_open_decisions() == []


def test_list_open_asks_sorted_most_recently_extracted_first(ws_db):
    now = time.time()
    _issue_with_extraction(ws_db, "Older", "a3", {"asks": ["older ask"]}, extracted_ts=now - 500)
    _issue_with_extraction(ws_db, "Newer", "a4", {"asks": ["newer ask"]}, extracted_ts=now)

    entries = wad.list_open_asks()

    assert [e["text"] for e in entries] == ["newer ask", "older ask"]


# --- enhancement #87: per-issue scoping (issue detail panel) -------------

def test_list_asks_for_issue_scoped_to_one_issue(ws_db):
    iid = _issue_with_extraction(ws_db, "Renewal", "pi1", {"asks": ["confirm the redline"]})
    _issue_with_extraction(ws_db, "Other", "pi2", {"asks": ["unrelated ask"]})

    entries = wad.list_asks_for_issue(iid)
    assert [e["text"] for e in entries] == ["confirm the redline"]
    assert entries[0]["raw_item_id"] is not None


def test_list_decisions_for_issue_scoped_to_one_issue(ws_db):
    iid = _issue_with_extraction(ws_db, "Renewal", "pi3", {"decisions": ["approved for FY27"]})
    _issue_with_extraction(ws_db, "Other", "pi4", {"decisions": ["unrelated decision"]})

    assert [e["text"] for e in wad.list_decisions_for_issue(iid)] == ["approved for FY27"]


def test_list_asks_for_issue_includes_closed_issues(ws_db):
    """Unlike the global rollup, the per-issue read has no open-state
    filter - Marc looking at a specific (even closed) issue should still
    see what was really asked on it."""
    issue_id = ws_db.create_issue_with_new_id(title="Closed deal", state="done", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="pi5", thread_key="pi5", dedupe_key="pi5",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({"asks": ["closed-issue ask"]}))
    workgraph_claims.materialize_claims_for_raw_item(rid)

    assert [e["text"] for e in wad.list_asks_for_issue(issue_id)] == ["closed-issue ask"]


def test_list_asks_for_issue_empty_when_none(ws_db):
    iid = _issue_with_extraction(ws_db, "No asks", "pi6", {"decisions": ["something"]})

    assert wad.list_asks_for_issue(iid) == []


# --- hardening pass #3: malformed extracted_json shape must not crash ----

def test_list_open_asks_non_list_field_value_ignored(ws_db):
    """A malformed extraction row (e.g. asks written as a bare int/string
    instead of a list) must not crash the whole rollup or iterate
    characters of a stray string."""
    _issue_with_extraction(ws_db, "Malformed int", "m1", {"asks": 5})
    _issue_with_extraction(ws_db, "Malformed str", "m2", {"asks": "not a list"})
    iid = _issue_with_extraction(ws_db, "Real one", "m3", {"asks": ["a real ask"]})

    entries = wad.list_open_asks()

    assert [e["text"] for e in entries] == ["a real ask"]
    assert entries[0]["issue_id"] == iid


def test_list_asks_for_issue_non_list_field_value_ignored(ws_db):
    iid = _issue_with_extraction(ws_db, "Malformed", "m4", {"asks": 5})

    assert wad.list_asks_for_issue(iid) == []


# --- N+1 fix (2026-08-02): batched *_for_issues() must match the singular
# *_for_issue() call made once per issue, but via one extractions fetch. ---

def test_list_asks_for_issues_batched_matches_per_issue_calls(ws_db):
    iid1 = _issue_with_extraction(ws_db, "One", "b1", {"asks": ["ask one"]})
    iid2 = _issue_with_extraction(ws_db, "Two", "b2", {"asks": ["ask two"]})
    iid3 = ws_db.create_issue_with_new_id(title="No asks", state="active", category="other")

    batched = wad.list_asks_for_issues([iid1, iid2, iid3])

    assert [e["text"] for e in batched[iid1]] == ["ask one"]
    assert [e["text"] for e in batched[iid2]] == ["ask two"]
    assert batched[iid3] == []
    # Exactly the same shape/content the per-issue call already returns.
    assert batched[iid1] == wad.list_asks_for_issue(iid1)
    assert batched[iid2] == wad.list_asks_for_issue(iid2)


def test_list_decisions_for_issues_batched_matches_per_issue_calls(ws_db):
    iid1 = _issue_with_extraction(ws_db, "One", "b3", {"decisions": ["decision one"]})
    iid2 = _issue_with_extraction(ws_db, "Two", "b4", {"decisions": ["decision two"]})

    batched = wad.list_decisions_for_issues([iid1, iid2])

    assert batched[iid1] == wad.list_decisions_for_issue(iid1)
    assert batched[iid2] == wad.list_decisions_for_issue(iid2)


def test_list_asks_for_issues_empty_list_returns_empty_dict(ws_db):
    assert wad.list_asks_for_issues([]) == {}


def test_list_asks_for_issues_does_not_leak_across_issues(ws_db):
    """A malformed/absent field on one issue must never contaminate another
    issue's own entry in the same batched call."""
    iid1 = _issue_with_extraction(ws_db, "Good", "b5", {"asks": ["real ask"]})
    iid2 = _issue_with_extraction(ws_db, "Bad", "b6", {"asks": 5})

    batched = wad.list_asks_for_issues([iid1, iid2])

    assert [e["text"] for e in batched[iid1]] == ["real ask"]
    assert batched[iid2] == []
