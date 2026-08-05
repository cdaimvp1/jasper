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


def _cluster(ws_db, title="Cluster"):
    return ws_db.create_cluster_with_new_id(title=title, category="other")


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
    """A raw_item whose extraction has no ask/decision/commitment/date/
    key_fact at all - materializing it should NOT bump claims_revision."""
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=occurred_ts if occurred_ts is not None else time.time(),
        subject="s", from_actor="a@example.com", participants_json="[]", body_preview="fyi only",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps({}))
    wc.materialize_claims_for_raw_item(rid)
    return rid


def _key_facts_only_raw_item(ws_db, issue_id, key, occurred_ts=None):
    """A raw_item whose extraction has a key_fact but no ask/decision/
    commitment/date - fixed 2026-08-04: a key_fact is never itself a
    claim, but it IS real new material information, so materializing it
    should still bump claims_revision (see workgraph_claims.
    materialize_claims_for_raw_item's own docstring)."""
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


def test_marker_bumps_for_key_facts_only_evidence(ws_db):
    """Fixed 2026-08-04: a key_fact is never a claim, but IS real new
    material information - materializing it must still change the
    marker, not leave it byte-for-byte identical."""
    iid = _issue(ws_db)
    before = wsyn.compute_evidence_marker("issue", iid)
    _key_facts_only_raw_item(ws_db, iid, "m2b")
    after = wsyn.compute_evidence_marker("issue", iid)
    assert before != after


def test_project_marker_changes_when_a_member_gets_a_new_claim(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    iid1 = _issue(ws_db, "One")
    iid2 = _issue(ws_db, "Two")
    ws_db.assign_issue_to_project(iid1, pid)
    ws_db.assign_issue_to_project(iid2, pid)
    _material_raw_item(ws_db, iid1, "m3", "ask one")
    _material_raw_item(ws_db, iid2, "m4", "ask two")
    _material_raw_item(ws_db, iid2, "m5", "ask three")

    marker = wsyn.compute_evidence_marker("project", pid)
    assert marker != "rev:0"
    assert marker.startswith("rev:")


def test_project_marker_changes_when_non_max_member_gets_a_new_claim(ws_db):
    """Real aggregation bug this fix closes: MAX(claims_revision) across
    members misses a non-max member's OWN revision changing (member A at
    rev 10, member B goes 2 -> 3 doesn't move the max at all if A stays
    at 10) - the fingerprint must change even when the CURRENT max member
    isn't the one that got new activity."""
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    high_rev_member = _issue(ws_db, "High")
    low_rev_member = _issue(ws_db, "Low")
    ws_db.assign_issue_to_project(high_rev_member, pid)
    ws_db.assign_issue_to_project(low_rev_member, pid)
    for i in range(5):
        _material_raw_item(ws_db, high_rev_member, f"high{i}", f"ask {i}")
    _material_raw_item(ws_db, low_rev_member, "low0", "ask zero")

    before = wsyn.compute_evidence_marker("project", pid)
    _material_raw_item(ws_db, low_rev_member, "low1", "ask one")  # low_rev_member still isn't the max
    after = wsyn.compute_evidence_marker("project", pid)

    assert ws_db.get_claims_revision(low_rev_member) < ws_db.get_claims_revision(high_rev_member)
    assert before != after


def test_project_marker_changes_when_a_member_is_added(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    iid1 = _issue(ws_db, "One")
    ws_db.assign_issue_to_project(iid1, pid)
    _material_raw_item(ws_db, iid1, "add1", "ask one")

    before = wsyn.compute_evidence_marker("project", pid)
    iid2 = _issue(ws_db, "Two")
    ws_db.assign_issue_to_project(iid2, pid)
    after = wsyn.compute_evidence_marker("project", pid)

    assert before != after


def test_project_marker_changes_when_a_member_is_removed(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    iid1 = _issue(ws_db, "One")
    iid2 = _issue(ws_db, "Two")
    ws_db.assign_issue_to_project(iid1, pid)
    ws_db.assign_issue_to_project(iid2, pid)
    _material_raw_item(ws_db, iid1, "rem1", "ask one")

    before = wsyn.compute_evidence_marker("project", pid)
    ws_db.update_issue(iid2, project_id=None)
    after = wsyn.compute_evidence_marker("project", pid)

    assert before != after


def test_project_marker_stays_the_same_when_nothing_changes(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    iid1 = _issue(ws_db, "One")
    iid2 = _issue(ws_db, "Two")
    ws_db.assign_issue_to_project(iid1, pid)
    ws_db.assign_issue_to_project(iid2, pid)
    _material_raw_item(ws_db, iid1, "same1", "ask one")
    _material_raw_item(ws_db, iid2, "same2", "ask two")

    first = wsyn.compute_evidence_marker("project", pid)
    second = wsyn.compute_evidence_marker("project", pid)

    assert first == second


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


# --- Corrected pipeline Phase D (2026-08-05): clusters must participate ---
# in claims_revision/synthesis-staleness exactly like real issues - a
# raw_item now attaches to a CLUSTER first (Phase B), and the real bug this
# closes: bump_claims_revision/get_claims_revision/get_project_claims_
# fingerprint used to write/read through the `issues` view, which silently
# no-ops for any is_raw_cluster=1 row (zero matching rows), so a cluster's
# revision counter never advanced at all and a project made entirely of
# clusters would look permanently fresh after its first (empty) marker.

def test_marker_bumps_after_material_claim_on_a_cluster(ws_db):
    cid = _cluster(ws_db)
    assert wsyn.compute_evidence_marker("issue", cid) == "rev:0"
    _material_raw_item(ws_db, cid, "cm1", "please send the SOW")
    assert wsyn.compute_evidence_marker("issue", cid) == "rev:1"


def test_project_marker_changes_when_a_cluster_member_gets_a_new_claim(ws_db):
    """The real gap this phase closes: a project made up ENTIRELY of
    clusters (the common shape right after a Phase C promotion, before
    curator has extracted any real issue from it) must not read as
    permanently fresh no matter how much real claims activity accumulates
    on its cluster members."""
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    cid = _cluster(ws_db, "Cluster One")
    ws_db.assign_issue_to_project(cid, pid)

    before = wsyn.compute_evidence_marker("project", pid)
    _material_raw_item(ws_db, cid, "cm2", "an ask on the cluster")
    after = wsyn.compute_evidence_marker("project", pid)

    assert before != after


def test_project_with_mixed_cluster_and_issue_members_aggregates_both(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    cid = _cluster(ws_db, "Cluster")
    iid = _issue(ws_db, "Issue")
    ws_db.assign_issue_to_project(cid, pid)
    ws_db.assign_issue_to_project(iid, pid)
    _material_raw_item(ws_db, iid, "mix1", "ask on the real issue")

    before = wsyn.compute_evidence_marker("project", pid)
    _material_raw_item(ws_db, cid, "mix2", "ask on the cluster")
    after = wsyn.compute_evidence_marker("project", pid)

    assert before != after


# --- has_confirmed_grouping (Phase D trigger) -----------------------------

def test_stale_project_has_confirmed_grouping_false_by_default(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    cid = _cluster(ws_db, "Cluster")
    ws_db.assign_issue_to_project(cid, pid)

    stale = wsyn.list_stale_entities()
    entry = next(s for s in stale if s["entity_id"] == pid)
    assert entry["has_confirmed_grouping"] is False


def test_stale_project_has_confirmed_grouping_true_once_a_member_is_confirmed(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    cid = _cluster(ws_db, "Cluster")
    ws_db.assign_issue_to_project(cid, pid)
    ws_db.confirm_work_object_membership(cid)

    stale = wsyn.list_stale_entities()
    entry = next(s for s in stale if s["entity_id"] == pid)
    assert entry["has_confirmed_grouping"] is True


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
