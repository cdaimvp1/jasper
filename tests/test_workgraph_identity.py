"""Regression tests for workgraph_identity.py's backfill_identity_anchors()
(identity formalization v0, 2026-08-03) - materializing source_containers/
identity_anchors from signals that already work, per docs/design/
CONFIDENCE_AND_IDENTITY_REDESIGN.md Section 3.3."""
from __future__ import annotations

import time

import workgraph_identity as wi
import workgraph_signals


def _issue(ws_db, title, category="other"):
    return ws_db.create_issue_with_new_id(title=title, state="active", category=category)


def _raw_item(ws_db, issue_id, subject, key, source="outlook_mail", thread_key=None):
    rid = ws_db.insert_raw_item(
        source=source, stable_key=key, thread_key=thread_key or key, dedupe_key=key,
        occurred_ts=time.time(), subject=subject, from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    m = workgraph_signals.REFERENCE_ID_RE.search(subject or "")
    if m:
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET pr_number = ?, pr_number_base = ? WHERE id = ?",
                     (m.group(0), m.group(0).split("-")[0], rid))
        conn.close()
    return rid


def _link_party(ws_db, issue_id, party_id, email, *, company=None, affiliation="external"):
    ws_db.upsert_party(id=party_id, primary_email=email, display_name=party_id,
                        affiliation=affiliation, affiliation_confidence="H",
                        affiliation_source="domain", company=company)
    ws_db.link_party_to_issue(issue_id, party_id)


def test_backfill_writes_container_from_raw_item_thread_key(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "Hello", "k1", source="outlook_mail", thread_key="conv-1")

    result = wi.backfill_identity_anchors()

    assert result["containers_written"] >= 1
    containers = ws_db.list_source_containers(issue_id=a)
    assert any(c["exact_key"] == "conv-1" and c["container_type"] == "email_conversation" for c in containers)


def test_backfill_writes_reference_anchor_from_real_pr_number(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "Approve PR1111865 - SAP RISE", "k1")

    wi.backfill_identity_anchors()

    anchors = ws_db.list_identity_anchors(issue_id=a)
    ref_anchors = [x for x in anchors if x["anchor_type"] == "reference"]
    assert len(ref_anchors) == 1
    assert ref_anchors[0]["normalized_value"] == "PR1111865"
    assert ref_anchors[0]["exclusive"] == 1
    assert ref_anchors[0]["anchor_strength"] == "strong"


def test_backfill_writes_party_and_company_anchors_non_exclusive(ws_db):
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")

    wi.backfill_identity_anchors()

    anchors = ws_db.list_identity_anchors(issue_id=a)
    assert any(x["anchor_type"] == "party" and x["normalized_value"] == "p1" and x["exclusive"] == 0 for x in anchors)
    assert any(x["anchor_type"] == "company" and x["normalized_value"] == "acme" and x["exclusive"] == 0 for x in anchors)


def test_backfill_excludes_automated_sender_parties(ws_db):
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "no-reply@ansmtp.ariba.com", company="Ariba")

    wi.backfill_identity_anchors()

    anchors = ws_db.list_identity_anchors(issue_id=a)
    assert anchors == []


def test_backfill_reports_real_reference_conflict_without_crashing(ws_db):
    """A pre-existing fragmentation/collision case (the same PR number
    already touching two issues today) must be reported, not raise or
    silently overwrite the first issue's anchor."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, a, "Approve PR2222222", "k1")
    _raw_item(ws_db, b, "REMINDER PR2222222", "k2")

    result = wi.backfill_identity_anchors()

    assert len(result["anchor_conflicts"]) == 1
    conflict = result["anchor_conflicts"][0]
    assert conflict["normalized_value"] == "PR2222222"
    assert {conflict["issue_id"], conflict["held_by"]} == {a, b}


def test_backfill_sessionizes_teams_container_across_issues(ws_db):
    """The real marc-362 shape: two messages on the same Teams chat, one
    already linked to issue a, one to issue b (today's flat model can
    already split one chat's history this way) - the sessionizer must see
    BOTH via list_raw_items_by_thread_key, not just whichever issue's own
    raw_items happen to be scanned first."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    r1 = _raw_item(ws_db, a, None, "c1:m1", source="teams_chat", thread_key="c1")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET occurred_ts = 0 WHERE id = ?", (r1,))
    conn.close()
    r2 = _raw_item(ws_db, b, None, "c1:m2", source="teams_chat", thread_key="c1")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET occurred_ts = ? WHERE id = ?", (100 * 3600.0, r2))
    conn.close()

    result = wi.backfill_identity_anchors()

    assert result["teams_sessions_written"] == 2
    sessions = ws_db.list_source_sessions("sc-teams_chat-c1")
    assert [s["session_sequence"] for s in sessions] == [0, 1]
    assert any(c["thread_key"] == "c1" for c in result["teams_containers_with_multiple_sessions"])


def test_backfill_is_idempotent(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "Approve PR3333333", "k1")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")

    first = wi.backfill_identity_anchors()
    second = wi.backfill_identity_anchors()

    assert second["anchors_written"] == 0
    assert len(ws_db.list_identity_anchors(issue_id=a)) == 3  # reference + party + company, not duplicated


def test_run_backfill_daily_if_due_gates_second_call(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "Approve PR9999999", "k1")

    first = wi.run_backfill_daily_if_due()
    assert first is not None
    assert first["anchors_written"] >= 1

    second = wi.run_backfill_daily_if_due()
    assert second is None
