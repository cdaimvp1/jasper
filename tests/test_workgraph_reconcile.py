"""Tests for workgraph_reconcile.py (task #155): claim-resolution
suggestions - suggest-only, never auto-close. Covers both evidence types
(explicit_resolution_signal, issue_closed_with_open_claims), the confirm/
reject lifecycle, and dedup against duplicate pending suggestions."""
from __future__ import annotations

import json
import time

import workgraph_classify as wc_classify
import workgraph_claims as wc
import workgraph_reconcile as wr


def _issue(ws_db, title="Issue", state="active"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category="other")


def _raw_item(ws_db, issue_id, key, extracted_json, direction=None):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, json.dumps(extracted_json))
    if direction is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET direction = ? WHERE id = ?", (direction, rid))
        conn.close()
    return rid


def _set_issue_state(ws_db, issue_id, state):
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET state = ? WHERE id = ?", (state, issue_id))
    conn.close()


# --- explicit_resolution_signal ---------------------------------------------

def test_explicit_resolution_signal_creates_a_resolve_suggestion(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "sig1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]

    rid2 = _raw_item(ws_db, iid, "sig2", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "please send the SOW",
                                 "resolution_note": "signed SOW attached"}],
    }, direction="inbound")
    matched = wr.generate_resolution_signal_suggestions(rid2)

    assert matched == 1
    suggestions = ws_db.list_pending_claim_suggestions(issue_id=iid)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["claim_id"] == claim["id"]
    assert s["suggestion_kind"] == "resolve"
    assert s["evidence_type"] == "explicit_resolution_signal"
    assert s["evidence_note"] == "signed SOW attached"
    assert s["raw_item_id"] == rid2
    assert s["status"] == "pending"


def test_resolution_signal_for_nonexistent_claim_is_skipped(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "sig3", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "an ask that was never made",
                                 "resolution_note": "n/a"}],
    }, direction="inbound")

    matched = wr.generate_resolution_signal_suggestions(rid)

    assert matched == 0
    assert ws_db.list_pending_claim_suggestions(issue_id=iid) == []


def test_resolution_signal_dedupes_against_existing_pending_suggestion(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "sig4", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    rid2 = _raw_item(ws_db, iid, "sig5", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "please send the SOW",
                                 "resolution_note": "first mention"}],
    }, direction="inbound")

    wr.generate_resolution_signal_suggestions(rid2)
    wr.generate_resolution_signal_suggestions(rid2)  # re-run, e.g. a repeated backfill sweep

    assert len(ws_db.list_pending_claim_suggestions(issue_id=iid)) == 1


def test_resolution_signal_with_no_extraction_field_is_a_noop(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "sig6", {"asks": ["please send the SOW"]}, direction="outbound")

    assert wr.generate_resolution_signal_suggestions(rid) == 0


# --- confirm / reject lifecycle ---------------------------------------------

def test_confirm_resolve_suggestion_marks_claim_done_and_logs_event(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "conf1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    rid2 = _raw_item(ws_db, iid, "conf2", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "please send the SOW",
                                 "resolution_note": "attached"}],
    }, direction="inbound")
    wr.generate_resolution_signal_suggestions(rid2)
    suggestion = ws_db.list_pending_claim_suggestions(issue_id=iid)[0]

    ok = wr.confirm_claim_suggestion(suggestion["id"], actor="marc")

    assert ok is True
    assert ws_db.get_claim(claim["id"])["status"] == "done"
    events = ws_db.list_claim_events_for_claim(claim["id"])
    assert any(e["event_type"] == "complete" and "claim-resolution suggestion" in (e["note"] or "")
               for e in events)
    assert ws_db.get_claim_suggestion(suggestion["id"])["status"] == "confirmed"


def test_reject_resolve_suggestion_leaves_claim_open(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "rej1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    rid2 = _raw_item(ws_db, iid, "rej2", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "please send the SOW"}],
    }, direction="inbound")
    wr.generate_resolution_signal_suggestions(rid2)
    suggestion = ws_db.list_pending_claim_suggestions(issue_id=iid)[0]

    ok = wr.reject_claim_suggestion(suggestion["id"], actor="marc")

    assert ok is True
    assert ws_db.get_claim(claim["id"])["status"] == "open"
    assert ws_db.get_claim_suggestion(suggestion["id"])["status"] == "rejected"


def test_confirm_or_reject_on_an_already_resolved_suggestion_returns_false(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "twice1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    rid2 = _raw_item(ws_db, iid, "twice2", {
        "resolution_signals": [{"claim_type": "ask", "claim_text": "please send the SOW"}],
    }, direction="inbound")
    wr.generate_resolution_signal_suggestions(rid2)
    suggestion = ws_db.list_pending_claim_suggestions(issue_id=iid)[0]
    wr.confirm_claim_suggestion(suggestion["id"], actor="marc")

    assert wr.confirm_claim_suggestion(suggestion["id"], actor="marc") is False
    assert wr.reject_claim_suggestion(suggestion["id"], actor="marc") is False


def test_confirm_on_unknown_suggestion_id_returns_false(ws_db):
    assert wr.confirm_claim_suggestion(999999, actor="marc") is False


# --- issue_closed_with_open_claims (contradiction) --------------------------

def test_issue_closed_with_open_claims_creates_a_contradiction_suggestion(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "closed1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    _set_issue_state(ws_db, iid, "done")

    result = wr.detect_issue_closed_with_open_claims_contradictions()

    assert result["suggestions_created"] >= 1
    suggestions = ws_db.list_pending_claim_suggestions(issue_id=iid)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["claim_id"] == claim["id"]
    assert s["suggestion_kind"] == "contradiction"
    assert s["evidence_type"] == "issue_closed_with_open_claims"


def test_confirming_a_contradiction_suggestion_never_touches_the_claim(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "closed2", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    _set_issue_state(ws_db, iid, "done")
    wr.detect_issue_closed_with_open_claims_contradictions()
    suggestion = ws_db.list_pending_claim_suggestions(issue_id=iid)[0]

    ok = wr.confirm_claim_suggestion(suggestion["id"], actor="marc")

    assert ok is True
    assert ws_db.get_claim(claim["id"])["status"] == "open"  # never inferred done
    assert ws_db.get_claim_suggestion(suggestion["id"])["status"] == "confirmed"


# --- resolved_claim_reoccurred (reopen) - task #304, item #5 ---------------

def test_confirming_a_reopen_suggestion_sets_the_claim_back_to_open(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "reo1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    original = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    ws_db.update_claim_status(original["id"], "done", actor="marc")
    rid2 = _raw_item(ws_db, iid, "reo2", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid2)
    suggestion = next(s for s in ws_db.list_pending_claim_suggestions(iid) if s["suggestion_kind"] == "reopen")

    ok = wr.confirm_claim_suggestion(suggestion["id"], actor="marc")

    assert ok is True
    assert ws_db.get_claim(original["id"])["status"] == "open"
    assert ws_db.get_claim_suggestion(suggestion["id"])["status"] == "confirmed"
    events = ws_db.list_claim_events_for_claim(original["id"])
    assert any(e["event_type"] == "reopen" for e in events)


def test_rejecting_a_reopen_suggestion_never_touches_the_claim(ws_db):
    iid = _issue(ws_db)
    rid1 = _raw_item(ws_db, iid, "reo3", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid1)
    original = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    ws_db.update_claim_status(original["id"], "done", actor="marc")
    rid2 = _raw_item(ws_db, iid, "reo4", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid2)
    suggestion = next(s for s in ws_db.list_pending_claim_suggestions(iid) if s["suggestion_kind"] == "reopen")

    ok = wr.reject_claim_suggestion(suggestion["id"], actor="marc")

    assert ok is True
    assert ws_db.get_claim(original["id"])["status"] == "done"
    assert ws_db.get_claim_suggestion(suggestion["id"])["status"] == "rejected"


def test_open_issue_with_open_claims_creates_no_contradiction(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "open1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)

    result = wr.detect_issue_closed_with_open_claims_contradictions()

    assert ws_db.list_pending_claim_suggestions(issue_id=iid) == []
    assert result["issues_scanned"] == 0


def test_issue_closed_sweep_is_idempotent_no_duplicate_suggestions(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "closed3", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    _set_issue_state(ws_db, iid, "done")

    wr.detect_issue_closed_with_open_claims_contradictions()
    wr.detect_issue_closed_with_open_claims_contradictions()

    assert len(ws_db.list_pending_claim_suggestions(issue_id=iid)) == 1


def test_issue_closed_with_a_resolved_claim_creates_no_contradiction(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "closed4", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    ws_db.update_claim_status(claim["id"], "done", actor="marc")
    _set_issue_state(ws_db, iid, "done")

    wr.detect_issue_closed_with_open_claims_contradictions()

    assert ws_db.list_pending_claim_suggestions(issue_id=iid) == []


# --- issues_appear_resolved_but_still_open (task #273, the Kinaxis mirror) --

def test_issue_with_all_claims_resolved_but_still_open_creates_a_suggestion(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "kinaxis1", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    ws_db.update_claim_status(claim["id"], "done", actor="marc")

    result = wr.detect_issues_appear_resolved_but_still_open()

    assert result["suggestions_created"] == 1
    suggestions = ws_db.list_issue_state_suggestions(status="pending")
    assert len(suggestions) == 1
    assert suggestions[0]["issue_id"] == iid


def test_issue_with_an_open_claim_creates_no_suggestion(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "kinaxis2", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)

    result = wr.detect_issues_appear_resolved_but_still_open()

    assert result["suggestions_created"] == 0
    assert ws_db.list_issue_state_suggestions(status="pending") == []


def test_issue_with_zero_claims_creates_no_suggestion(ws_db):
    _issue(ws_db, state="active")

    result = wr.detect_issues_appear_resolved_but_still_open()

    assert result["suggestions_created"] == 0


def test_already_closed_issue_with_resolved_claims_creates_no_suggestion(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "kinaxis3", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    ws_db.update_claim_status(claim["id"], "done", actor="marc")
    _set_issue_state(ws_db, iid, "done")  # already closed - not the bug this catches

    result = wr.detect_issues_appear_resolved_but_still_open()

    assert result["suggestions_created"] == 0


def test_appears_resolved_sweep_is_idempotent_no_duplicate_suggestions(ws_db):
    iid = _issue(ws_db, state="active")
    rid = _raw_item(ws_db, iid, "kinaxis4", {"asks": ["please send the SOW"]}, direction="outbound")
    wc.materialize_claims_for_raw_item(rid)
    claim = wc.list_open_claims_for_issue(iid, claim_type="ask")[0]
    ws_db.update_claim_status(claim["id"], "done", actor="marc")

    wr.detect_issues_appear_resolved_but_still_open()
    wr.detect_issues_appear_resolved_but_still_open()

    assert len(ws_db.list_issue_state_suggestions(status="pending")) == 1


def test_resolve_issue_state_suggestion_updates_status(ws_db):
    iid = _issue(ws_db, state="active")
    suggestion_id = ws_db.create_issue_state_suggestion(issue_id=iid, evidence_note="test")

    ws_db.resolve_issue_state_suggestion(suggestion_id, "confirmed")

    resolved = ws_db.list_issue_state_suggestions(status="confirmed")
    assert len(resolved) == 1
    assert resolved[0]["id"] == suggestion_id
    assert resolved[0]["resolved_ts"] is not None


# --- merge_stray_same_reference_clusters (2026-08-08, task #278
# investigation: the concrete Bluefish drift root cause) --------------------

def _classified_raw_item(ws_db, issue_id, key, *, pr_number_base, signal_type=None, item_class="FYI-EVIDENCE"):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.classify_raw_item(
        rid, item_class=item_class, direction="inbound", direction_inferred=False,
        topic="other", topic_inferred=False, sentiment="neutral", sentiment_inferred=False,
        anomaly_flag=False, signal_type=signal_type, pr_number=pr_number_base, pr_number_base=pr_number_base,
    )
    return rid


def test_merge_stray_clusters_absorbs_a_cluster_sharing_reference_with_a_real_issue(ws_db):
    issue = _issue(ws_db, state="active")
    cluster = ws_db.create_cluster_with_new_id(title="Stray cluster", category="other")
    _classified_raw_item(ws_db, issue, "k1", pr_number_base="PR1189827",
                          signal_type="ariba_pr_approval_needed", item_class="ACTIONABLE-ASK")
    stray_rid = _classified_raw_item(ws_db, cluster, "k2", pr_number_base="PR1189827",
                                      signal_type="ariba_pr_fully_approved")

    result = wr.merge_stray_same_reference_clusters()

    assert result["clusters_absorbed"] == 1
    conn = ws_db._connect()
    assert conn.execute("SELECT issue_id FROM raw_items WHERE id = ?", (stray_rid,)).fetchone()[0] == issue
    conn.close()
    assert ws_db.get_cluster(cluster)["state"] == "dismissed"  # get_cluster aliases status AS state


def test_merge_stray_clusters_recomputes_issue_state_after_absorbing(ws_db):
    """The real Bluefish shape: an issue with an open ACTIONABLE-ASK whose
    matching closure signal was sitting unseen in a stray cluster. Once
    absorbed, recompute_issue_state should see both signal_types together
    and no longer treat this specific request/closure pair as still open -
    confirms the sweep doesn't just move data, it makes the fix actually
    take effect."""
    issue = _issue(ws_db, state="active")
    cluster = ws_db.create_cluster_with_new_id(title="Stray cluster", category="other")
    _classified_raw_item(ws_db, issue, "k1", pr_number_base="PR1189827",
                          signal_type="ariba_pr_approval_needed", item_class="FYI-EVIDENCE")
    _classified_raw_item(ws_db, cluster, "k2", pr_number_base="PR1189827",
                          signal_type="ariba_pr_fully_approved")

    wr.merge_stray_same_reference_clusters()

    # No other ACTIONABLE-ASK/WAITING-ON-OTHERS item exists on the issue,
    # and the one request/closure pair is now complete - derive_target_state
    # falls through to "done".
    assert wc_classify.derive_target_state(issue) == "done"


def test_merge_stray_clusters_skips_ambiguous_groups_with_two_real_issues(ws_db):
    """Two real issues sharing a pr_number_base is a different, riskier
    case (a duplicate-issue problem, not a stray-cluster problem) - this
    sweep must not guess which one is "right" and silently absorb into
    either."""
    issue_a = _issue(ws_db, state="active")
    issue_b = _issue(ws_db, state="active")
    _classified_raw_item(ws_db, issue_a, "k1", pr_number_base="PR777")
    _classified_raw_item(ws_db, issue_b, "k2", pr_number_base="PR777")

    result = wr.merge_stray_same_reference_clusters()

    assert result["clusters_absorbed"] == 0


# --- list_identity_conflicts_across_grouped_projects (review point #3, 2026-08-11) --
# This is exactly the "two real issues sharing a pr_number_base" case the
# test above confirms merge_stray_same_reference_clusters deliberately
# skips - here that same skipped case is surfaced for a human instead of
# silently dropped, but ONLY once each issue is grouped into a DIFFERENT
# real project (two ungrouped issues sharing a reference is normal pre-
# grouping state, not a conflict between two already-settled decisions).

def test_identity_conflict_flagged_when_shared_reference_spans_two_projects(ws_db):
    project_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    project_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    issue_a = _issue(ws_db, state="active")
    issue_b = _issue(ws_db, state="active")
    ws_db.assign_issue_to_project(issue_a, project_a, reason="test")
    ws_db.assign_issue_to_project(issue_b, project_b, reason="test")
    _classified_raw_item(ws_db, issue_a, "k1", pr_number_base="PR777")
    _classified_raw_item(ws_db, issue_b, "k2", pr_number_base="PR777")

    conflicts = wr.list_identity_conflicts_across_grouped_projects()

    assert len(conflicts) == 1
    assert conflicts[0]["pr_number_base"] == "PR777"
    flagged_project_ids = {p["project_id"] for p in conflicts[0]["projects"]}
    assert flagged_project_ids == {project_a, project_b}


def test_identity_conflict_not_flagged_when_both_issues_in_the_same_project(ws_db):
    project = ws_db.create_project_with_new_id(name="Same project", category="other")
    issue_a = _issue(ws_db, state="active")
    issue_b = _issue(ws_db, state="active")
    ws_db.assign_issue_to_project(issue_a, project, reason="test")
    ws_db.assign_issue_to_project(issue_b, project, reason="test")
    _classified_raw_item(ws_db, issue_a, "k1", pr_number_base="PR888")
    _classified_raw_item(ws_db, issue_b, "k2", pr_number_base="PR888")

    assert wr.list_identity_conflicts_across_grouped_projects() == []


def test_identity_conflict_not_flagged_when_only_one_issue_is_grouped(ws_db):
    """One issue already in a project, the other still ungrouped - this is
    the common, unremarkable case (an item that will naturally become a
    real candidate for that same project once it's processed), not a
    conflict between two independently-settled decisions."""
    project = ws_db.create_project_with_new_id(name="Project", category="other")
    issue_a = _issue(ws_db, state="active")
    issue_b = _issue(ws_db, state="active")
    ws_db.assign_issue_to_project(issue_a, project, reason="test")
    _classified_raw_item(ws_db, issue_a, "k1", pr_number_base="PR999")
    _classified_raw_item(ws_db, issue_b, "k2", pr_number_base="PR999")

    assert wr.list_identity_conflicts_across_grouped_projects() == []


def test_identity_conflict_not_flagged_for_a_single_ungrouped_pair(ws_db):
    """Baseline: two ungrouped issues sharing a reference is exactly the
    case merge_stray_same_reference_clusters's own skip test above covers
    from the OTHER angle - normal pre-grouping state, never a conflict."""
    issue_a = _issue(ws_db, state="active")
    issue_b = _issue(ws_db, state="active")
    _classified_raw_item(ws_db, issue_a, "k1", pr_number_base="PR000")
    _classified_raw_item(ws_db, issue_b, "k2", pr_number_base="PR000")

    assert wr.list_identity_conflicts_across_grouped_projects() == []


def test_identity_conflict_never_reassigns_or_merges_anything(ws_db):
    """Read-only by construction - confirms the projects/issues themselves
    are completely untouched by calling this."""
    project_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    project_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    issue_a = _issue(ws_db, state="active")
    issue_b = _issue(ws_db, state="active")
    ws_db.assign_issue_to_project(issue_a, project_a, reason="test")
    ws_db.assign_issue_to_project(issue_b, project_b, reason="test")
    _classified_raw_item(ws_db, issue_a, "k1", pr_number_base="PR555")
    _classified_raw_item(ws_db, issue_b, "k2", pr_number_base="PR555")

    wr.list_identity_conflicts_across_grouped_projects()

    assert ws_db.get_issue(issue_a)["project_id"] == project_a
    assert ws_db.get_issue(issue_b)["project_id"] == project_b


def test_merge_stray_clusters_skips_groups_with_only_clusters(ws_db):
    """Two clusters sharing a reference with no promoted issue yet is
    normal pre-promotion state, not this sweep's job - leave it for
    curator's own extraction pass."""
    cluster_a = ws_db.create_cluster_with_new_id(title="A", category="other")
    cluster_b = ws_db.create_cluster_with_new_id(title="B", category="other")
    _classified_raw_item(ws_db, cluster_a, "k1", pr_number_base="PR888")
    _classified_raw_item(ws_db, cluster_b, "k2", pr_number_base="PR888")

    result = wr.merge_stray_same_reference_clusters()

    assert result["clusters_absorbed"] == 0


def test_merge_stray_clusters_is_idempotent(ws_db):
    issue = _issue(ws_db, state="active")
    cluster = ws_db.create_cluster_with_new_id(title="Stray cluster", category="other")
    _classified_raw_item(ws_db, issue, "k1", pr_number_base="PR555")
    _classified_raw_item(ws_db, cluster, "k2", pr_number_base="PR555")

    first = wr.merge_stray_same_reference_clusters()
    second = wr.merge_stray_same_reference_clusters()

    assert first["clusters_absorbed"] == 1
    assert second["clusters_absorbed"] == 0  # already absorbed and dismissed - no longer an open group


# --- merge_stray_signature_confirmation_clusters (task #284) ---------------
# The real Bluefish case this was built from: an Adobe Sign "You signed"
# confirmation (no pr_number_base of its own) landed in its own stray
# cluster under a brand new project, while the real negotiation issue
# shared both a human participant (Aryelle Player) and the underlying
# document's filename with it.

def _signature_confirmation(ws_db, cluster_id, key, *, participants, filenames):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="You signed: something", from_actor="adobesign@adobesign.com",
        participants_json=json.dumps(participants),
    )
    ws_db.link_raw_item_to_issue(rid, cluster_id)
    ws_db.classify_raw_item(
        rid, item_class="FYI-EVIDENCE", direction="inbound", direction_inferred=False,
        topic="contract", topic_inferred=False, sentiment="neutral", sentiment_inferred=False,
        anomaly_flag=False, signal_type="signature_signed_by_me", pr_number=None, pr_number_base=None,
    )
    for i, filename in enumerate(filenames):
        ws_db.create_attachment(
            entity_type="raw_item", entity_id=str(rid), kind="reference", filename=filename,
            stored_path=f"{key}-{i}.pdf", content_type="application/pdf", size_bytes=10,
            sha256_hex=f"hash-{key}-{i}", uploaded_by="outlook_ingest",
        )
    return rid


def test_merge_signature_confirmation_absorbs_on_participant_and_filename_match(ws_db):
    ws_db.upsert_party(id="p-aryelle", primary_email="aryelle.player@lilly.com",
                        display_name="Aryelle L Player", affiliation="internal",
                        affiliation_confidence="M", affiliation_source="domain_heuristic", company=None)
    issue = _issue(ws_db, title="Bluefish AI Accuracy SOW Signature & PR Approval", state="active")
    ws_db.link_party_to_issue(issue, "p-aryelle")
    ws_db.create_attachment(
        entity_type="issue", entity_id=issue, kind="reference",
        filename="Bluefish_Eli Lilly_AI Accuracy SOW 7.14.26.pdf", stored_path="issue.pdf",
        content_type="application/pdf", size_bytes=10, sha256_hex="issue-hash", uploaded_by="outlook_ingest",
    )
    cluster = ws_db.create_cluster_with_new_id(title='You signed: "Bluefish...SOW..."', category="contract")
    _signature_confirmation(
        ws_db, cluster, "sig1", participants=["adobesign@adobesign.com", "Aryelle L Player"],
        filenames=["Bluefish_Eli+Lilly_AI+Accuracy+SOW+7.14.26 (part 1) - signed.pdf"],
    )

    result = wr.merge_stray_signature_confirmation_clusters()

    assert result["clusters_absorbed"] == 1
    assert ws_db.get_cluster(cluster)["state"] == "dismissed"


def test_merge_signature_confirmation_skips_when_filename_does_not_match(ws_db):
    """Shared participant alone isn't enough - Aryelle-type frequent
    internal contacts sit on many unrelated issues in real data."""
    ws_db.upsert_party(id="p-aryelle", primary_email="aryelle.player@lilly.com",
                        display_name="Aryelle L Player", affiliation="internal",
                        affiliation_confidence="M", affiliation_source="domain_heuristic", company=None)
    issue = _issue(ws_db, title="Unrelated other issue", state="active")
    ws_db.link_party_to_issue(issue, "p-aryelle")
    ws_db.create_attachment(
        entity_type="issue", entity_id=issue, kind="reference",
        filename="Completely Different Document.pdf", stored_path="issue.pdf",
        content_type="application/pdf", size_bytes=10, sha256_hex="issue-hash-2", uploaded_by="outlook_ingest",
    )
    cluster = ws_db.create_cluster_with_new_id(title='You signed: "Bluefish...SOW..."', category="contract")
    _signature_confirmation(
        ws_db, cluster, "sig2", participants=["adobesign@adobesign.com", "Aryelle L Player"],
        filenames=["Bluefish_Eli+Lilly_AI+Accuracy+SOW+7.14.26 (part 1) - signed.pdf"],
    )

    result = wr.merge_stray_signature_confirmation_clusters()

    assert result["clusters_absorbed"] == 0


def test_merge_signature_confirmation_skips_when_no_shared_participant(ws_db):
    """Filename overlap alone isn't enough either - both signals required."""
    issue = _issue(ws_db, title="Bluefish AI Accuracy SOW Signature & PR Approval", state="active")
    ws_db.create_attachment(
        entity_type="issue", entity_id=issue, kind="reference",
        filename="Bluefish_Eli Lilly_AI Accuracy SOW 7.14.26.pdf", stored_path="issue.pdf",
        content_type="application/pdf", size_bytes=10, sha256_hex="issue-hash-3", uploaded_by="outlook_ingest",
    )
    cluster = ws_db.create_cluster_with_new_id(title='You signed: "Bluefish...SOW..."', category="contract")
    _signature_confirmation(
        ws_db, cluster, "sig3", participants=["adobesign@adobesign.com", "Nobody Jasper Knows"],
        filenames=["Bluefish_Eli+Lilly_AI+Accuracy+SOW+7.14.26 (part 1) - signed.pdf"],
    )

    result = wr.merge_stray_signature_confirmation_clusters()

    assert result["clusters_absorbed"] == 0


def test_merge_signature_confirmation_skips_when_ambiguous_two_candidates(ws_db):
    ws_db.upsert_party(id="p-aryelle", primary_email="aryelle.player@lilly.com",
                        display_name="Aryelle L Player", affiliation="internal",
                        affiliation_confidence="M", affiliation_source="domain_heuristic", company=None)
    issue_a = _issue(ws_db, title="Bluefish SOW - copy A", state="active")
    ws_db.link_party_to_issue(issue_a, "p-aryelle")
    ws_db.create_attachment(
        entity_type="issue", entity_id=issue_a, kind="reference",
        filename="Bluefish_Eli Lilly_AI Accuracy SOW 7.14.26.pdf", stored_path="issue_a.pdf",
        content_type="application/pdf", size_bytes=10, sha256_hex="issue-hash-a", uploaded_by="outlook_ingest",
    )
    issue_b = _issue(ws_db, title="Bluefish SOW - copy B", state="active")
    ws_db.link_party_to_issue(issue_b, "p-aryelle")
    ws_db.create_attachment(
        entity_type="issue", entity_id=issue_b, kind="reference",
        filename="Bluefish_Eli Lilly_AI Accuracy SOW 7.14.26.pdf", stored_path="issue_b.pdf",
        content_type="application/pdf", size_bytes=10, sha256_hex="issue-hash-b", uploaded_by="outlook_ingest",
    )
    cluster = ws_db.create_cluster_with_new_id(title='You signed: "Bluefish...SOW..."', category="contract")
    _signature_confirmation(
        ws_db, cluster, "sig4", participants=["adobesign@adobesign.com", "Aryelle L Player"],
        filenames=["Bluefish_Eli+Lilly_AI+Accuracy+SOW+7.14.26 (part 1) - signed.pdf"],
    )

    result = wr.merge_stray_signature_confirmation_clusters()

    assert result["clusters_absorbed"] == 0
    assert result["ambiguous_skipped"] == 1


def test_merge_signature_confirmation_ignores_raw_item_with_its_own_reference(ws_db):
    """Already covered by the pr_number_base sweep - not this one's job."""
    ws_db.upsert_party(id="p-aryelle", primary_email="aryelle.player@lilly.com",
                        display_name="Aryelle L Player", affiliation="internal",
                        affiliation_confidence="M", affiliation_source="domain_heuristic", company=None)
    issue = _issue(ws_db, title="Bluefish AI Accuracy SOW Signature & PR Approval", state="active")
    ws_db.link_party_to_issue(issue, "p-aryelle")
    ws_db.create_attachment(
        entity_type="issue", entity_id=issue, kind="reference",
        filename="Bluefish_Eli Lilly_AI Accuracy SOW 7.14.26.pdf", stored_path="issue.pdf",
        content_type="application/pdf", size_bytes=10, sha256_hex="issue-hash-4", uploaded_by="outlook_ingest",
    )
    cluster = ws_db.create_cluster_with_new_id(title='You signed: "Bluefish...SOW..."', category="contract")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="sig5", thread_key="sig5", dedupe_key="sig5",
        occurred_ts=time.time(), subject="You signed", from_actor="adobesign@adobesign.com",
        participants_json=json.dumps(["adobesign@adobesign.com", "Aryelle L Player"]),
    )
    ws_db.link_raw_item_to_issue(rid, cluster)
    ws_db.classify_raw_item(
        rid, item_class="FYI-EVIDENCE", direction="inbound", direction_inferred=False,
        topic="contract", topic_inferred=False, sentiment="neutral", sentiment_inferred=False,
        anomaly_flag=False, signal_type="signature_signed_by_me", pr_number="PR999", pr_number_base="PR999",
    )

    result = wr.merge_stray_signature_confirmation_clusters()

    assert result["raw_items_checked"] == 0
    assert result["clusters_absorbed"] == 0
