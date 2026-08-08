"""Regression tests for workgraph_focus.py (task #283, "what should I
focus on today?"). Pure composition over three already-tested readers
(workgraph_nba.rank_actions, workgraph_meetingprep.find_upcoming_meeting_
prep_candidates, workgraph_deadlines.attach_deadline_info) - these tests
check the composition/filtering, not those readers' own internals."""
from __future__ import annotations

import time

import workgraph_focus as wf

DAY = 86400.0


def _issue(ws_db, title="Issue", state="active"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category="other")


def test_build_focus_today_summary_empty_db(ws_db):
    summary = wf.build_focus_today_summary()

    assert summary == {"top_actions": [], "meetings_today": [], "deliverables_due_soon": []}


def test_build_focus_today_summary_includes_marcs_open_ask(ws_db):
    iid = _issue(ws_db, title="Needs approval")
    rid = ws_db.insert_raw_item(source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
                                 occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]")
    ws_db.insert_claim(issue_id=iid, raw_item_id=rid, claim_type="ask", text="please approve",
                        author="counterparty", author_basis="direction", owner="marc")

    summary = wf.build_focus_today_summary()

    assert len(summary["top_actions"]) == 1
    assert summary["top_actions"][0]["issue_id"] == iid


def test_build_focus_today_summary_includes_meeting_within_24h(ws_db):
    iid = _issue(ws_db, title="Vendor sync")
    now = time.time()
    rid = ws_db.insert_raw_item(source="calendar", stable_key="c1", thread_key="c1", dedupe_key="c1",
                                 occurred_ts=now + 6 * 3600.0, subject="Sync call", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)

    summary = wf.build_focus_today_summary(now=now)

    assert len(summary["meetings_today"]) == 1
    assert summary["meetings_today"][0]["issue_id"] == iid


def test_build_focus_today_summary_excludes_meeting_beyond_24h(ws_db):
    iid = _issue(ws_db, title="Next week sync")
    now = time.time()
    rid = ws_db.insert_raw_item(source="calendar", stable_key="c2", thread_key="c2", dedupe_key="c2",
                                 occurred_ts=now + 3 * DAY, subject="Sync call", from_actor="a@example.com",
                                 participants_json="[]")
    ws_db.link_raw_item_to_issue(rid, iid)

    summary = wf.build_focus_today_summary(now=now)

    assert summary["meetings_today"] == []


def test_build_focus_today_summary_includes_deliverable_due_this_week(ws_db):
    iid = _issue(ws_db, title="Contract due")
    now = time.time()
    ws_db.update_issue(iid, due=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 3 * DAY)))

    summary = wf.build_focus_today_summary(now=now)

    assert len(summary["deliverables_due_soon"]) == 1
    assert summary["deliverables_due_soon"][0]["issue_id"] == iid
    assert summary["deliverables_due_soon"][0]["overdue"] is False


def test_build_focus_today_summary_flags_overdue_deliverable(ws_db):
    iid = _issue(ws_db, title="Overdue contract")
    now = time.time()
    ws_db.update_issue(iid, due=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 2 * DAY)))

    summary = wf.build_focus_today_summary(now=now)

    assert summary["deliverables_due_soon"][0]["overdue"] is True


def test_build_focus_today_summary_excludes_deliverable_beyond_a_week(ws_db):
    iid = _issue(ws_db, title="Far-out contract")
    now = time.time()
    ws_db.update_issue(iid, due=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 30 * DAY)))

    summary = wf.build_focus_today_summary(now=now)

    assert summary["deliverables_due_soon"] == []
