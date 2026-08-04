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
    assert wd._normalize_date_mention("Aug 11 - tentative") == \
        {"text": "Aug 11 - tentative", "kind": None, "raw_item_id": None,
         "deadline_type": None, "resolved_date": None}


def test_normalize_new_shape_hard():
    assert wd._normalize_date_mention({"text": "must sign by Aug 11", "kind": "hard"}) == \
        {"text": "must sign by Aug 11", "kind": "hard", "raw_item_id": None,
         "deadline_type": None, "resolved_date": None}


def test_normalize_new_shape_soft():
    assert wd._normalize_date_mention({"text": "shooting for next week", "kind": "soft"}) == \
        {"text": "shooting for next week", "kind": "soft", "raw_item_id": None,
         "deadline_type": None, "resolved_date": None}


def test_normalize_malformed_kind_becomes_none():
    assert wd._normalize_date_mention({"text": "x", "kind": "urgent"}) == \
        {"text": "x", "kind": None, "raw_item_id": None, "deadline_type": None, "resolved_date": None}


def test_normalize_carries_raw_item_id_when_given():
    """Enhancement idea panel #5: the enclosing extraction's own
    raw_item_id, passed through untouched - a real deep-link target,
    never guessed."""
    assert wd._normalize_date_mention("Aug 11", raw_item_id=42) == \
        {"text": "Aug 11", "kind": None, "raw_item_id": 42, "deadline_type": None, "resolved_date": None}


def test_normalize_blank_string_is_none():
    assert wd._normalize_date_mention("   ") is None


# --- deadline_type / resolved_date (task #141, E18) ------------------------

def test_normalize_hard_deadline_with_renewal_type_and_resolved_date():
    entry = {"text": "notice due 2026-11-01", "kind": "hard",
              "deadline_type": "renewal_notice", "resolved_date": "2026-11-01"}
    result = wd._normalize_date_mention(entry)
    assert result["deadline_type"] == "renewal_notice"
    assert result["resolved_date"] == wd._parse_due("2026-11-01")


def test_normalize_malformed_deadline_type_becomes_none():
    entry = {"text": "x", "kind": "hard", "deadline_type": "made_up", "resolved_date": "2026-11-01"}
    result = wd._normalize_date_mention(entry)
    assert result["deadline_type"] is None


def test_normalize_deadline_type_ignored_on_soft_entry():
    """deadline_type/resolved_date are only ever trusted on kind=hard -
    curator is only asked to populate them there."""
    entry = {"text": "x", "kind": "soft", "deadline_type": "renewal_notice", "resolved_date": "2026-11-01"}
    result = wd._normalize_date_mention(entry)
    assert result["deadline_type"] is None
    assert result["resolved_date"] is None


def test_normalize_unparseable_resolved_date_is_none():
    entry = {"text": "x", "kind": "hard", "deadline_type": "renewal_notice", "resolved_date": "not a date"}
    result = wd._normalize_date_mention(entry)
    assert result["resolved_date"] is None


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
    rid = _extraction_with_dates(ws_db, iid, [{"text": "must sign by Aug 11", "kind": "hard"}], "m1")
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    mention = issues[0]["deadline_mentions"][0]
    assert mention["text"] == "must sign by Aug 11" and mention["kind"] == "hard"
    # Enhancement idea panel #5: a real deep-link target, not guessed.
    assert mention["raw_item_id"] == rid
    assert issues[0]["has_hard_deadline"] is True


def test_soft_mention_alone_is_not_hard(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, [{"text": "shooting for next week", "kind": "soft"}], "m2")
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    mention = issues[0]["deadline_mentions"][0]
    assert mention["text"] == "shooting for next week" and mention["kind"] == "soft"
    assert issues[0]["has_hard_deadline"] is False


def test_legacy_unclassified_mention_is_not_hard(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, ["Aug 11 - tentative press release"], "m3")
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    mention = issues[0]["deadline_mentions"][0]
    assert mention["text"] == "Aug 11 - tentative press release" and mention["kind"] is None
    assert issues[0]["has_hard_deadline"] is False


def test_deadline_mention_carries_deep_links_key(ws_db):
    """Enhancement idea panel #5: reuses deep_links.attach_deep_links
    verbatim - an outlook_mail raw_item's mention gets real action links,
    the same ones its own evidence row would get."""
    now = time.time()
    iid = _issue(ws_db)
    rid = _extraction_with_dates(ws_db, iid, [{"text": "must sign by Aug 11", "kind": "hard"}], "m1x")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET entry_id = ? WHERE id = ?", ("entry-abc", rid))
    conn.commit()
    conn.close()
    issues = [ws_db.get_issue(iid)]

    wd.attach_deadline_info(issues, now=now)

    mention = issues[0]["deadline_mentions"][0]
    assert "deep_links" in mention
    assert isinstance(mention["deep_links"], list)
    assert len(mention["deep_links"]) > 0  # outlook_mail with a real entry_id gets real actions


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


# --- find_renewal_outreach_candidates / renewal_outreach_draft (E18) -------

def _iso(now, days_out):
    return time.strftime("%Y-%m-%d", time.gmtime(now + days_out * DAY))


def _external_party(ws_db, issue_id, email="rep@supplier.com", name="Supplier Rep"):
    ws_db.upsert_party(id=email, primary_email=email, display_name=name,
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain_heuristic", company="Supplier Co")
    ws_db.link_party_to_issue(issue_id, email)


def test_renewal_candidate_within_window_is_found(ws_db):
    now = time.time()
    iid = _issue(ws_db, title="Five9 renewal")
    _extraction_with_dates(ws_db, iid, [
        {"text": "notice due", "kind": "hard", "deadline_type": "renewal_notice",
         "resolved_date": _iso(now, 60)},
    ], "ro1")

    candidates = wd.find_renewal_outreach_candidates(now=now)

    assert len(candidates) == 1
    assert candidates[0]["issue_id"] == iid
    assert candidates[0]["deadline_type"] == "renewal_notice"
    assert 59.0 <= candidates[0]["days_out"] <= 61.0


def test_renewal_candidate_outside_window_is_excluded(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, [
        {"text": "notice due", "kind": "hard", "deadline_type": "renewal_notice",
         "resolved_date": _iso(now, 5)},  # too soon - below the 30-day floor
    ], "ro2")

    assert wd.find_renewal_outreach_candidates(now=now) == []


def test_non_renewal_deadline_type_excluded(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, [
        {"text": "must sign", "kind": "hard", "deadline_type": "signature_deadline",
         "resolved_date": _iso(now, 60)},
    ], "ro3")

    assert wd.find_renewal_outreach_candidates(now=now) == []


def test_hard_deadline_without_resolved_date_excluded(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, [
        {"text": "renewal coming up sometime", "kind": "hard", "deadline_type": "renewal_notice"},
    ], "ro4")

    assert wd.find_renewal_outreach_candidates(now=now) == []


def test_renewal_outreach_draft_has_real_recipient_and_deadline(ws_db):
    now = time.time()
    iid = _issue(ws_db, title="Five9 renewal")
    _extraction_with_dates(ws_db, iid, [
        {"text": "notice must be sent 90 days before anniversary", "kind": "hard",
         "deadline_type": "renewal_notice", "resolved_date": _iso(now, 45)},
    ], "ro5")
    _external_party(ws_db, iid)

    draft = wd.renewal_outreach_draft(iid, now=now)

    assert draft is not None
    assert draft["recipient_email"] == "rep@supplier.com"
    assert "Five9 renewal" in draft["subject"]
    assert "notice must be sent 90 days before anniversary" in draft["body"]
    assert draft["deadline_type"] == "renewal_notice"


def test_renewal_outreach_draft_none_without_external_party(ws_db):
    now = time.time()
    iid = _issue(ws_db)
    _extraction_with_dates(ws_db, iid, [
        {"text": "notice due", "kind": "hard", "deadline_type": "renewal_notice",
         "resolved_date": _iso(now, 45)},
    ], "ro6")

    assert wd.renewal_outreach_draft(iid, now=now) is None


def test_renewal_outreach_draft_none_without_candidate(ws_db):
    iid = _issue(ws_db)
    assert wd.renewal_outreach_draft(iid) is None
