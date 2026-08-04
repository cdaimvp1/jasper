"""Tests for workgraph_meetingprep.py (task #143, E20): a deterministic
meeting-prep draft built from real, already-ingested data - open
claims, key facts, deadline info, calendar attendees/agenda. No LLM
call, no guessing; a genuine draft Marc reviews before the meeting."""
from __future__ import annotations

import json
import time

import workgraph_claims
import workgraph_meetingprep as mp

DAY = 86400.0


def _issue(ws_db, title="Issue", state="active"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category="other")


def _calendar_meeting(ws_db, issue_id, key, hours_out, meta=None):
    now = time.time()
    rid = ws_db.insert_raw_item(
        source="calendar", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=now + hours_out * 3600.0, subject="Sync call", from_actor="a@example.com",
        participants_json="[]", meta_json=json.dumps(meta) if meta else None,
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    return rid


def _extraction(ws_db, issue_id, key, blob, direction="inbound"):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET direction = ? WHERE id = ?", (direction, rid))
    conn.close()
    ws_db.create_extraction(rid, json.dumps(blob))
    workgraph_claims.materialize_claims_for_raw_item(rid)
    return rid


def test_upcoming_meeting_within_window_is_found(ws_db):
    iid = _issue(ws_db, title="Vendor sync")
    rid = _calendar_meeting(ws_db, iid, "mp1", hours_out=24)

    candidates = mp.find_upcoming_meeting_prep_candidates()

    assert len(candidates) == 1
    assert candidates[0]["issue_id"] == iid
    assert candidates[0]["meeting_raw_item_id"] == rid


def test_meeting_outside_window_is_excluded(ws_db):
    iid = _issue(ws_db)
    _calendar_meeting(ws_db, iid, "mp2", hours_out=72)  # beyond the default 48h window

    assert mp.find_upcoming_meeting_prep_candidates() == []


def test_past_meeting_is_excluded(ws_db):
    iid = _issue(ws_db)
    _calendar_meeting(ws_db, iid, "mp3", hours_out=-1)

    assert mp.find_upcoming_meeting_prep_candidates() == []


def test_nearest_of_two_meetings_is_chosen(ws_db):
    iid = _issue(ws_db)
    later = _calendar_meeting(ws_db, iid, "mp4a", hours_out=40)
    nearer = _calendar_meeting(ws_db, iid, "mp4b", hours_out=5)

    candidates = mp.find_upcoming_meeting_prep_candidates()

    assert len(candidates) == 1
    assert candidates[0]["meeting_raw_item_id"] == nearer


def test_draft_includes_open_asks_and_key_facts(ws_db):
    iid = _issue(ws_db, title="Vendor sync")
    _calendar_meeting(ws_db, iid, "mp5", hours_out=10,
                       meta={"attendees_detailed": [{"name": "Jane Doe"}], "full_agenda_text": "Review pricing"})
    _extraction(ws_db, iid, "mp5x", {"asks": ["please confirm the SOW"], "key_facts": ["deal is worth $50k"]},
                direction="outbound")

    draft = mp.meeting_prep_draft(iid)

    assert draft is not None
    assert draft["open_ask_count"] == 1
    assert draft["key_fact_count"] == 1
    assert "please confirm the SOW" in draft["narrative"]
    assert "deal is worth $50k" in draft["narrative"]
    assert "Jane Doe" in draft["narrative"]
    assert "Review pricing" in draft["narrative"]


def test_draft_flags_hard_deadline(ws_db):
    iid = _issue(ws_db)
    _calendar_meeting(ws_db, iid, "mp6", hours_out=10)
    _extraction(ws_db, iid, "mp6x", {"dates_mentioned": [{"text": "must sign by Friday", "kind": "hard"}]})

    draft = mp.meeting_prep_draft(iid)

    assert draft["has_hard_deadline"] is True
    assert "hard deadline" in draft["narrative"]


def test_draft_is_none_without_upcoming_meeting(ws_db):
    iid = _issue(ws_db)
    assert mp.meeting_prep_draft(iid) is None


def test_draft_notes_no_open_items_when_none_exist(ws_db):
    iid = _issue(ws_db)
    _calendar_meeting(ws_db, iid, "mp7", hours_out=10)

    draft = mp.meeting_prep_draft(iid)

    assert draft["open_ask_count"] == 0
    assert "no open items" in draft["narrative"]
