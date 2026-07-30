"""Regression tests for workgraph_deadlines.py (task #64, Deadline Radar).
Two tiers, deliberately never merged: `structured` (real due dates / real
upcoming calendar evidence, safe to sort by actual proximity) and
`mentioned` (free-text dates_mentioned extractions, never auto-parsed into
a day-count - the exact caution the Ariba expiration-date signal's ~98%
error rate showed was needed)."""
from __future__ import annotations

import json
import time

import workgraph_deadlines as wd

DAY = 86400.0


def _set_evidence_ts(ws_db, evidence_id, ts):
    conn = ws_db._connect()
    conn.execute("UPDATE evidence SET ts = ? WHERE id = ?", (ts, evidence_id))
    conn.close()


def test_build_radar_empty_when_no_signals(ws_db):
    ws_db.create_issue_with_new_id(title="No dates anywhere", state="active", category="other")
    radar = wd.build_radar(time.time())
    assert radar["structured"] == []
    assert radar["mentioned"] == []


def test_structured_includes_future_due_date(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Renewal", state="active", category="other")
    due_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 5 * DAY))
    ws_db.update_issue(iid, due=due_iso)

    radar = wd.build_radar(now)

    assert len(radar["structured"]) == 1
    entry = radar["structured"][0]
    assert entry["issue_id"] == iid
    assert entry["source"] == "due_date"
    assert entry["overdue"] is False
    assert 4.5 < entry["days_out"] < 5.5


def test_structured_flags_overdue_due_date(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Overdue thing", state="active", category="other")
    due_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 2 * DAY))
    ws_db.update_issue(iid, due=due_iso)

    radar = wd.build_radar(now)

    assert radar["structured"][0]["overdue"] is True
    assert radar["structured"][0]["days_out"] < 0


def test_malformed_due_date_is_ignored_not_crashed(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Bad date", state="active", category="other")
    ws_db.update_issue(iid, due="not-a-real-date")

    radar = wd.build_radar(now)  # must not raise

    assert radar["structured"] == []


def test_structured_includes_upcoming_calendar_event_within_lookahead(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Board review", state="active", category="other")
    eid = ws_db.add_evidence(issue_id=iid, type="calendar", summary="Board review meeting")
    _set_evidence_ts(ws_db, eid, now + 10 * DAY)

    radar = wd.build_radar(now)

    assert len(radar["structured"]) == 1
    assert radar["structured"][0]["source"] == "calendar"
    assert 9.5 < radar["structured"][0]["days_out"] < 10.5


def test_calendar_event_beyond_lookahead_excluded(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Far-off meeting", state="active", category="other")
    eid = ws_db.add_evidence(issue_id=iid, type="calendar", summary="Way out there")
    _set_evidence_ts(ws_db, eid, now + 30 * DAY)

    radar = wd.build_radar(now)

    assert radar["structured"] == []


def test_past_calendar_event_excluded(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Already happened", state="active", category="other")
    eid = ws_db.add_evidence(issue_id=iid, type="calendar", summary="Past meeting")
    _set_evidence_ts(ws_db, eid, now - 2 * DAY)

    radar = wd.build_radar(now)

    assert radar["structured"] == []


def test_due_date_takes_priority_over_calendar_evidence(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Has both", state="active", category="other")
    due_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 3 * DAY))
    ws_db.update_issue(iid, due=due_iso)
    eid = ws_db.add_evidence(issue_id=iid, type="calendar", summary="Also has a meeting")
    _set_evidence_ts(ws_db, eid, now + 10 * DAY)

    radar = wd.build_radar(now)

    assert len(radar["structured"]) == 1
    assert radar["structured"][0]["source"] == "due_date"
    assert 2.5 < radar["structured"][0]["days_out"] < 3.5


def test_structured_sorted_soonest_first_across_issues(ws_db):
    now = time.time()
    far = ws_db.create_issue_with_new_id(title="Far", state="active", category="other")
    ws_db.update_issue(far, due=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 9 * DAY)))
    near = ws_db.create_issue_with_new_id(title="Near", state="active", category="other")
    ws_db.update_issue(near, due=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 1 * DAY)))

    radar = wd.build_radar(now)

    assert [e["issue_id"] for e in radar["structured"]] == [near, far]


def test_closed_issues_excluded_from_structured(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Done thing", state="done", category="other")
    ws_db.update_issue(iid, due=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 1 * DAY)))

    radar = wd.build_radar(now)

    assert radar["structured"] == []


def test_mentioned_surfaces_raw_extraction_text_verbatim(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Press release deal", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="d1",
                                 occurred_ts=now, subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)
    ws_db.create_extraction(rid, json.dumps({
        "asks": [], "decisions": [], "dates_mentioned": ["Tuesday, August 11 - tentative"],
        "commitments": [], "key_facts": [],
    }))

    radar = wd.build_radar(now)

    assert len(radar["mentioned"]) == 1
    entry = radar["mentioned"][0]
    assert entry["issue_id"] == iid
    assert entry["text"] == "Tuesday, August 11 - tentative"
    # never computed into a day-count or urgency flag - that's the whole point
    assert "days_out" not in entry
    assert "overdue" not in entry


def test_mentioned_skips_blank_and_non_string_entries(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Mixed junk", state="active", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k2", thread_key="k2", dedupe_key="d2",
                                 occurred_ts=now, subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)
    ws_db.create_extraction(rid, json.dumps({
        "dates_mentioned": ["", "   ", None, 42, "a real one"],
    }))

    radar = wd.build_radar(now)

    assert [e["text"] for e in radar["mentioned"]] == ["a real one"]


def test_mentioned_sorted_most_recently_extracted_first(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Multiple mentions", state="active", category="other")
    rid1 = ws_db.insert_raw_item(source="outlook_mail", stable_key="k3", thread_key="k3", dedupe_key="d3",
                                  occurred_ts=now, subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid1, iid)
    rid2 = ws_db.insert_raw_item(source="outlook_mail", stable_key="k4", thread_key="k4", dedupe_key="d4",
                                  occurred_ts=now, subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid2, iid)
    ws_db.create_extraction(rid1, json.dumps({"dates_mentioned": ["older mention"]}))
    conn = ws_db._connect()
    conn.execute("UPDATE raw_item_extractions SET extracted_ts = ? WHERE raw_item_id = ?", (now - 100, rid1))
    conn.close()
    ws_db.create_extraction(rid2, json.dumps({"dates_mentioned": ["newer mention"]}))

    radar = wd.build_radar(now)

    assert [e["text"] for e in radar["mentioned"]] == ["newer mention", "older mention"]


def test_mentioned_excludes_closed_issues(ws_db):
    now = time.time()
    iid = ws_db.create_issue_with_new_id(title="Closed with a date", state="noise-archived", category="other")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k5", thread_key="k5", dedupe_key="d5",
                                 occurred_ts=now, subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)
    ws_db.create_extraction(rid, json.dumps({"dates_mentioned": ["should not appear"]}))

    radar = wd.build_radar(now)

    assert radar["mentioned"] == []
