"""Regression tests for workgraph_deadlines.py (task #64/#80, deadline
info attached to issue detail - not a standalone panel, per Marc's direct
feedback). due_date_info (real due dates / real upcoming calendar
evidence) is always safe to compute; deadline_mentions' hard/soft
`kind` comes from curator's real judgment at extraction time and is
never guessed retroactively from a keyword filter - the exact caution
the Ariba expiration-date signal's ~98% error rate showed was needed."""
from __future__ import annotations

import json
import time

import workgraph_deadlines as wd

DAY = 86400.0


def _set_evidence_ts(ws_db, evidence_id, ts):
    conn = ws_db._connect()
    conn.execute("UPDATE evidence SET ts = ? WHERE id = ?", (ts, evidence_id))
    conn.close()


def _issue(ws_db, title="Issue", state="active"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category="other")


def _extraction_with_dates(ws_db, issue_id, dates_mentioned, key):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({"dates_mentioned": dates_mentioned}))
    return rid


# --- _normalize_date_mention -------------------------------------------

def test_normalize_legacy_plain_string():
    assert wd._normalize_date_mention("Aug 11 - tentative") == {"text": "Aug 11 - tentative", "kind": None}


def test_normalize_new_shape_hard():
    assert wd._normalize_date_mention({"text": "must sign by Aug 11", "kind": "hard"}) == \
        {"text": "must sign by Aug 11", "kind": "hard"}


def test_normalize_new_shape_soft():
    assert wd._normalize_date_mention({"text": "shooting for next week", "kind": "soft"}) == \
        {"text": "shooting for next week", "kind": "soft"}


def test_normalize_malformed_kind_becomes_none():
    assert wd._normalize_date_mention({"text": "x", "kind": "urgent"}) == {"text": "x", "kind": None}


def test_normalize_blank_string_is_none():
    assert wd._normalize_date_mention("   ") is None


def test_normalize_object_with_blank_text_is_none():
    assert wd._normalize_date_mention({"text": "  ", "kind": "hard"}) is None


def test_normalize_garbage_types_are_none():
    assert wd._normalize_date_mention(42) is None
    assert wd._normalize_date_mention(None) is None
    assert wd._normalize_date_mention(["x"]) is None


# --- attach_deadline_info: due_date_info --------------------------------

def test_attach_deadline_info_empty_list_is_safe(ws_db):
    assert wd.attach_deadline_info([]) == []


def test_no_signals_yields_empty_and_not_hard(ws_db):
    iid = _issue(ws_db)
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=time.time())

    assert issues[0]["due_date_info"] is None
    assert issues[0]["deadline_mentions"] == []
    assert issues[0]["has_hard_deadline"] is False


def test_future_due_date_is_hard(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    ws_db.update_issue(iid, due=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 5 * DAY)))
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    info = issues[0]["due_date_info"]
    assert info["source"] == "due_date"
    assert info["overdue"] is False
    assert 4.5 < info["days_out"] < 5.5
    assert issues[0]["has_hard_deadline"] is True


def test_overdue_due_date_flagged(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    ws_db.update_issue(iid, due=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 2 * DAY)))
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    assert issues[0]["due_date_info"]["overdue"] is True


def test_malformed_due_date_ignored_not_crashed(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    ws_db.update_issue(iid, due="not-a-real-date")
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)  # must not raise

    assert issues[0]["due_date_info"] is None
    assert issues[0]["has_hard_deadline"] is False


def test_upcoming_calendar_event_within_lookahead_is_hard(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    eid = ws_db.add_evidence(issue_id=iid, type="calendar", summary="Board review")
    _set_evidence_ts(ws_db, eid, now + 10 * DAY)
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    info = issues[0]["due_date_info"]
    assert info["source"] == "calendar"
    assert 9.5 < info["days_out"] < 10.5
    assert issues[0]["has_hard_deadline"] is True


def test_calendar_event_beyond_lookahead_excluded(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    eid = ws_db.add_evidence(issue_id=iid, type="calendar", summary="Far off")
    _set_evidence_ts(ws_db, eid, now + 30 * DAY)
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    assert issues[0]["due_date_info"] is None


def test_past_calendar_event_excluded(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    eid = ws_db.add_evidence(issue_id=iid, type="calendar", summary="Already happened")
    _set_evidence_ts(ws_db, eid, now - 2 * DAY)
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    assert issues[0]["due_date_info"] is None


def test_due_date_takes_priority_over_calendar_evidence(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    ws_db.update_issue(iid, due=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 3 * DAY)))
    eid = ws_db.add_evidence(issue_id=iid, type="calendar", summary="Also has a meeting")
    _set_evidence_ts(ws_db, eid, now + 10 * DAY)
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    assert issues[0]["due_date_info"]["source"] == "due_date"


# --- attach_deadline_info: deadline_mentions -----------------------------

def test_hard_mention_sets_has_hard_deadline(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, [{"text": "must sign by Aug 11", "kind": "hard"}], "m1")
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    assert issues[0]["deadline_mentions"] == [{"text": "must sign by Aug 11", "kind": "hard"}]
    assert issues[0]["has_hard_deadline"] is True


def test_soft_mention_alone_is_not_hard(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, [{"text": "shooting for next week", "kind": "soft"}], "m2")
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    assert issues[0]["deadline_mentions"] == [{"text": "shooting for next week", "kind": "soft"}]
    assert issues[0]["has_hard_deadline"] is False


def test_legacy_unclassified_mention_is_not_hard(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, ["Aug 11 - tentative press release"], "m3")
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    assert issues[0]["deadline_mentions"] == [{"text": "Aug 11 - tentative press release", "kind": None}]
    assert issues[0]["has_hard_deadline"] is False


def test_mixed_hard_and_soft_mentions_both_kept(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, [
        {"text": "termination notice due Sept 1", "kind": "hard"},
        {"text": "hoping to wrap up by Friday", "kind": "soft"},
    ], "m4")
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    kinds = {m["kind"] for m in issues[0]["deadline_mentions"]}
    assert kinds == {"hard", "soft"}
    assert issues[0]["has_hard_deadline"] is True


def test_blank_and_garbage_mentions_skipped(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, ["", "   ", None, 42, {"text": "", "kind": "hard"}, "a real one"], "m5")
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    assert [m["text"] for m in issues[0]["deadline_mentions"]] == ["a real one"]


def test_multiple_issues_do_not_cross_contaminate(ws_db):
    now = time.time()
    hard_issue = _issue(ws_db, title="Hard one")
    _extraction_with_dates(ws_db, hard_issue, [{"text": "must sign", "kind": "hard"}], "m6")
    soft_issue = _issue(ws_db, title="Soft one")
    _extraction_with_dates(ws_db, soft_issue, [{"text": "shooting for", "kind": "soft"}], "m7")
    issues = [ws_db.get_issue(hard_issue), ws_db.get_issue(soft_issue)]

    wd.attach_deadline_info(issues, now=now)

    by_id = {i["id"]: i for i in issues}
    assert by_id[hard_issue]["has_hard_deadline"] is True
    assert by_id[soft_issue]["has_hard_deadline"] is False
