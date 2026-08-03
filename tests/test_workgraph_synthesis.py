"""Tests for workgraph_synthesis.py's revision-counter staleness marker
(design doc Section 9.5) - including a direct repro of D9/D10, the bug this
replaced the count/max_ts marker to fix: a late-arriving, OLD-timestamped
raw_item must still be detected as new evidence worth re-synthesizing."""
from __future__ import annotations

import json
import time

import workgraph_claims as wc
import workgraph_synthesis as wsyn


def _issue(ws_db, title="Issue", state="active"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category="other")


def _material_raw_item(ws_db, issue_id, key, text, occurred_ts=None, direction="outbound"):
    """A raw_item with a real ask - materializing it bumps claims_revision."""
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=occurred_ts if occurred_ts is not None else time.time(),
        subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({"asks": [text]}))
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET direction = ? WHERE id = ?", (direction, rid))
    conn.close()
    wc.materialize_claims_for_raw_item(rid)
    return rid


def _immaterial_raw_item(ws_db, issue_id, key, occurred_ts=None):
    """A raw_item whose extraction has no ask/decision/commitment/date -
    materializing it should NOT bump claims_revision."""
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=occurred_ts if occurred_ts is not None else time.time(),
        subject="s", from_actor="a@example.com", participants_json="[]", body_preview="fyi only",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({"key_facts": ["some fact"]}))
    wc.materialize_claims_for_raw_item(rid)
    return rid


# --- compute_evidence_marker ---------------------------------------------

def test_marker_is_rev_zero_for_untouched_issue(ws_db):
    iid = _issue(ws_db)
    assert wsyn.compute_evidence_marker("issue", iid) == "rev:0"


def test_marker_bumps_after_material_claim(ws_db):
    iid = _issue(ws_db)
    _material_raw_item(ws_db, iid, "m1", "please send the SOW")
    assert wsyn.compute_evidence_marker("issue", iid) == "rev:1"


def test_marker_does_not_bump_for_immaterial_evidence(ws_db):
    iid = _issue(ws_db)
    _immaterial_raw_item(ws_db, iid, "m2")
    assert wsyn.compute_evidence_marker("issue", iid) == "rev:0"


def test_project_marker_is_max_across_member_issues(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    iid1 = _issue(ws_db, "One")
    iid2 = _issue(ws_db, "Two")
    ws_db.assign_issue_to_project(iid1, pid)
    ws_db.assign_issue_to_project(iid2, pid)
    _material_raw_item(ws_db, iid1, "m3", "ask one")
    _material_raw_item(ws_db, iid2, "m4", "ask two")
    _material_raw_item(ws_db, iid2, "m5", "ask three")

    assert wsyn.compute_evidence_marker("project", pid) == "rev:2"


# --- list_stale_entities ---------------------------------------------

def test_never_synthesized_issue_is_stale(ws_db):
    iid = _issue(ws_db)
    stale = wsyn.list_stale_entities()
    assert any(s["entity_id"] == iid for s in stale)


def test_up_to_date_issue_is_not_stale(ws_db):
    iid = _issue(ws_db)
    _material_raw_item(ws_db, iid, "s1", "an ask")
    marker = wsyn.compute_evidence_marker("issue", iid)
    ws_db.upsert_synthesis(
        entity_type="issue", entity_id=iid, summary="done",
        next_steps_json="[]", suggested_actions_json="[]", synthesized_from_marker=marker,
    )
    stale = wsyn.list_stale_entities()
    assert not any(s["entity_id"] == iid for s in stale)


def test_d9_d10_repro_late_old_timestamped_item_is_detected_as_stale(ws_db):
    """The actual regression this fix targets: synthesize an issue, then
    materialize a NEW claim from a raw_item whose occurred_ts is OLDER than
    anything already synthesized (a backfilled historical item arriving
    late) - it must still show up as stale. Under the old count/max_ts
    marker this was exactly the case _new_raw_items_since's occurred_ts
    filter silently dropped."""
    iid = _issue(ws_db)
    now = time.time()
    _material_raw_item(ws_db, iid, "d1", "first ask", occurred_ts=now)
    marker = wsyn.compute_evidence_marker("issue", iid)
    ws_db.upsert_synthesis(
        entity_type="issue", entity_id=iid, summary="synthesized once",
        next_steps_json="[]", suggested_actions_json="[]", synthesized_from_marker=marker,
    )
    assert not any(s["entity_id"] == iid for s in wsyn.list_stale_entities())

    # Late-arriving item, timestamped BEFORE the already-synthesized one.
    _material_raw_item(ws_db, iid, "d2", "a late-discovered second ask", occurred_ts=now - 1_000_000)

    stale = wsyn.list_stale_entities()
    assert any(s["entity_id"] == iid for s in stale)


def test_immaterial_new_evidence_does_not_reflag_as_stale(ws_db):
    iid = _issue(ws_db)
    _material_raw_item(ws_db, iid, "i1", "an ask")
    marker = wsyn.compute_evidence_marker("issue", iid)
    ws_db.upsert_synthesis(
        entity_type="issue", entity_id=iid, summary="done",
        next_steps_json="[]", suggested_actions_json="[]", synthesized_from_marker=marker,
    )
    _immaterial_raw_item(ws_db, iid, "i2")

    assert not any(s["entity_id"] == iid for s in wsyn.list_stale_entities())


def test_legacy_marker_format_is_treated_as_stale_once(ws_db):
    iid = _issue(ws_db)
    ws_db.upsert_synthesis(
        entity_type="issue", entity_id=iid, summary="pre-migration synthesis",
        next_steps_json="[]", suggested_actions_json="[]",
        synthesized_from_marker="count:3|max_ts:1785000000.0",
    )
    assert any(s["entity_id"] == iid for s in wsyn.list_stale_entities())


def test_stats_dict_reports_skipped_immaterial_zero_and_deferred(ws_db):
    for i in range(3):
        _issue(ws_db, f"Issue {i}")
    stats: dict = {}
    stale = wsyn.list_stale_entities(limit=1, stats=stats)
    assert len(stale) == 1
    assert stats["skipped_immaterial"] == 0
    assert stats["deferred"] == 2


def test_never_synthesized_ranked_before_stale_existing(ws_db):
    iid_old = _issue(ws_db, "Has stale synthesis")
    _material_raw_item(ws_db, iid_old, "r1", "ask")
    ws_db.upsert_synthesis(
        entity_type="issue", entity_id=iid_old, summary="stale one",
        next_steps_json="[]", suggested_actions_json="[]",
        synthesized_from_marker="rev:0",
    )
    iid_new = _issue(ws_db, "Never synthesized")

    stale = wsyn.list_stale_entities()
    ranked_ids = [s["entity_id"] for s in stale]
    assert ranked_ids.index(iid_new) < ranked_ids.index(iid_old)
