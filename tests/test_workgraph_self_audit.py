"""Tests for workgraph_self_audit.py (task #370): "Jasper auditing its own
representation of reality." One targeted test per check - a real planted
case the check must catch, plus a healthy case it must leave alone - per
task #370's own "don't over-test" scope instruction. Also covers the
persistence contract itself (dedupe-then-touch, dismiss sticks) since that
was the real design judgment call this task made.
"""
from __future__ import annotations

import time

import workgraph_self_audit as wsa

_DAY = 24 * 3600


def _issue(ws_db, title="Issue", state="active"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category="other")


def _project(ws_db, name="Project", status="active"):
    return ws_db.create_project_with_new_id(name=name, category="other", status=status)


def _raw_item(ws_db, issue_id, key, signal_type=None):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com", participants_json="[]",
    )
    if issue_id is not None:
        ws_db.link_raw_item_to_issue(rid, issue_id)
    if signal_type is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", (signal_type, rid))
        conn.commit()
        conn.close()
    return rid


def _claim(ws_db, issue_id, raw_item_id, claim_type, owner="marc", ts=None, canonical_key=None):
    return ws_db.insert_claim(
        issue_id=issue_id, raw_item_id=raw_item_id, claim_type=claim_type, text=f"a {claim_type}",
        author="counterparty", author_basis="direction", owner=owner, ts=ts, canonical_key=canonical_key,
    )


# --- Check 1: stale active project ------------------------------------------

def test_stale_active_project_flagged_when_zero_claims(ws_db):
    p = _project(ws_db, "Stale", status="active")
    a = _issue(ws_db)
    ws_db.assign_issue_to_project(a, p)

    findings = wsa.find_stale_active_projects()

    assert any(f["subject_id"] == p for f in findings)


def test_stale_active_project_not_flagged_with_recent_claim(ws_db):
    now = time.time()
    p = _project(ws_db, "Fresh", status="active")
    a = _issue(ws_db)
    ws_db.assign_issue_to_project(a, p)
    rid = _raw_item(ws_db, a, "k1")
    _claim(ws_db, a, rid, "ask", ts=now)

    findings = wsa.find_stale_active_projects(now=now)

    assert not any(f["subject_id"] == p for f in findings)


# --- Check 2: done project, open commitments --------------------------------

def test_done_project_with_open_commitment_flagged(ws_db):
    p = _project(ws_db, "Finished", status="done")
    a = _issue(ws_db, state="done")
    ws_db.assign_issue_to_project(a, p)
    rid = _raw_item(ws_db, a, "k2")
    claim_id = _claim(ws_db, a, rid, "commitment")

    findings = wsa.find_done_projects_with_open_commitments()

    match = next(f for f in findings if f["subject_id"] == p)
    assert claim_id in match["detail"]["open_commitment_claim_ids"]


def test_done_project_not_flagged_when_commitment_already_resolved(ws_db):
    p = _project(ws_db, "Really finished", status="done")
    a = _issue(ws_db, state="done")
    ws_db.assign_issue_to_project(a, p)
    rid = _raw_item(ws_db, a, "k3")
    claim_id = _claim(ws_db, a, rid, "commitment")
    ws_db.update_claim_status(claim_id, "done", actor="marc")

    findings = wsa.find_done_projects_with_open_commitments()

    assert not any(f["subject_id"] == p for f in findings)


# --- Check 3: contradictory open claims -------------------------------------

def test_contradictory_claims_flagged_when_same_canonical_item_has_different_owners(ws_db):
    a = _issue(ws_db)
    rid = _raw_item(ws_db, a, "k4")
    _claim(ws_db, a, rid, "commitment", owner="marc", canonical_key="pr1170816")
    _claim(ws_db, a, rid, "commitment", owner="counterparty", canonical_key="pr1170816")

    findings = wsa.find_issues_with_contradictory_open_claims()

    assert any(f["subject_id"] == a for f in findings)


def test_contradictory_claims_not_flagged_for_two_unrelated_commitments(ws_db):
    """The real, confirmed false-positive this check's own docstring
    documents finding against the live dev DB: two DIFFERENT open
    commitments on the same issue (different owners, no shared
    canonical_key) is the normal shape of an active bilateral negotiation,
    not a contradiction."""
    a = _issue(ws_db)
    rid = _raw_item(ws_db, a, "k5")
    _claim(ws_db, a, rid, "commitment", owner="marc")
    _claim(ws_db, a, rid, "commitment", owner="counterparty")

    findings = wsa.find_issues_with_contradictory_open_claims()

    assert not any(f["subject_id"] == a for f in findings)


# --- Check 4: closure signal, no matching request ---------------------------

def test_closure_signal_flagged_when_no_matching_request(ws_db):
    a = _issue(ws_db, "PR approval thread")
    _raw_item(ws_db, a, "closure1", signal_type="ariba_pr_fully_approved")

    findings = wsa.find_closure_signals_with_no_matching_request()

    assert any(f["detail"]["issue_id"] == a for f in findings)


def test_closure_signal_not_flagged_when_matching_request_exists(ws_db):
    a = _issue(ws_db, "PR approval thread with a real ask")
    _raw_item(ws_db, a, "ask1", signal_type="ariba_pr_approval_needed")
    _raw_item(ws_db, a, "closure2", signal_type="ariba_pr_fully_approved")

    findings = wsa.find_closure_signals_with_no_matching_request()

    assert not any(f.get("detail", {}).get("issue_id") == a for f in findings)


# --- Check 5: succeeded action, no evidence ---------------------------------

def _succeeded_action(ws_db, issue_id, raw_item_id, idempotency_key):
    claim_id = _claim(ws_db, issue_id, raw_item_id, "ask")
    return claim_id, ws_db.create_prepared_action(
        claim_id=claim_id, action_type="draft_status_update",
        proposed_parameters_json="{}", evidence_refs_json="[]",
        rationale="r", risk_class="low", idempotency_key=idempotency_key, state="succeeded",
    )


def test_succeeded_action_flagged_when_no_evidence(ws_db):
    a = _issue(ws_db)
    rid = _raw_item(ws_db, a, "k6")
    _, action_id = _succeeded_action(ws_db, a, rid, "idem-1")

    findings = wsa.find_succeeded_actions_without_evidence()

    assert any(f["subject_id"] == str(action_id) for f in findings)


def test_succeeded_action_not_flagged_when_worker_action_evidence_exists(ws_db):
    a = _issue(ws_db)
    rid = _raw_item(ws_db, a, "k7")
    _, action_id = _succeeded_action(ws_db, a, rid, "idem-2")
    ws_db.add_evidence(issue_id=a, type="worker_action", summary="drafted and saved the status update")

    findings = wsa.find_succeeded_actions_without_evidence()

    assert not any(f["subject_id"] == str(action_id) for f in findings)


# --- Check 6: orphaned claim or evidence row --------------------------------

def test_orphaned_claim_flagged_after_issue_row_is_gone(ws_db):
    a = _issue(ws_db)
    rid = _raw_item(ws_db, a, "k8")
    claim_id = _claim(ws_db, a, rid, "ask")
    conn = ws_db._connect()
    conn.execute("DELETE FROM work_objects WHERE id = ?", (a,))
    conn.commit()
    conn.close()

    findings = wsa.find_orphaned_claims_and_evidence()

    assert any(f["subject_type"] == "claim" and f["subject_id"] == str(claim_id) for f in findings)


def test_orphaned_claim_not_flagged_for_a_healthy_claim(ws_db):
    a = _issue(ws_db)
    rid = _raw_item(ws_db, a, "k9")
    claim_id = _claim(ws_db, a, rid, "ask")

    findings = wsa.find_orphaned_claims_and_evidence()

    assert not any(f["subject_id"] == str(claim_id) for f in findings)


# --- Check 7: duplicate relationship aliases --------------------------------

def test_duplicate_relationship_alias_flagged_for_prefix_match(ws_db):
    ws_db.get_or_create_relationship_by_name("Sodalis")
    ws_db.get_or_create_relationship_by_name("Sodalis Solutions")

    findings = wsa.find_duplicate_relationship_aliases()

    assert any("Sodalis" in f["description"] for f in findings)


def test_duplicate_relationship_alias_not_flagged_for_unrelated_names(ws_db):
    ws_db.get_or_create_relationship_by_name("Acme")
    ws_db.get_or_create_relationship_by_name("Globex")

    findings = wsa.find_duplicate_relationship_aliases()

    assert findings == []


# --- Persistence contract: dedupe-then-touch, dismiss sticks ----------------

def test_sweep_does_not_duplicate_an_open_finding_on_rerun(ws_db):
    p = _project(ws_db, "Stale2", status="active")
    a = _issue(ws_db)
    ws_db.assign_issue_to_project(a, p)

    wsa.run_self_audit_sweep()
    wsa.run_self_audit_sweep()

    rows = ws_db.list_self_audit_findings(
        status="open", check_name="stale_active_project_zero_claims_30d"
    )
    matches = [r for r in rows if r["subject_id"] == p]
    assert len(matches) == 1


def test_dismissed_finding_does_not_reappear_on_rerun(ws_db):
    p = _project(ws_db, "Stale3", status="active")
    a = _issue(ws_db)
    ws_db.assign_issue_to_project(a, p)

    wsa.run_self_audit_sweep()
    row = next(
        r for r in ws_db.list_self_audit_findings(
            status="open", check_name="stale_active_project_zero_claims_30d")
        if r["subject_id"] == p
    )
    assert ws_db.dismiss_self_audit_finding(row["id"], actor="marc", note="known, watching it") is True

    wsa.run_self_audit_sweep()

    open_rows = ws_db.list_self_audit_findings(
        status="open", check_name="stale_active_project_zero_claims_30d"
    )
    dismissed_rows = ws_db.list_self_audit_findings(
        status="dismissed", check_name="stale_active_project_zero_claims_30d"
    )
    assert not any(r["subject_id"] == p for r in open_rows)
    assert any(r["subject_id"] == p for r in dismissed_rows)


def test_sweep_auto_resolves_a_finding_whose_condition_cleared(ws_db):
    now = time.time()
    p = _project(ws_db, "WasStale", status="active")
    a = _issue(ws_db)
    ws_db.assign_issue_to_project(a, p)

    wsa.run_self_audit_sweep(now=now)

    rid = _raw_item(ws_db, a, "k10")
    _claim(ws_db, a, rid, "ask", ts=now)
    wsa.run_self_audit_sweep(now=now)

    resolved_rows = ws_db.list_self_audit_findings(
        status="resolved", check_name="stale_active_project_zero_claims_30d"
    )
    assert any(r["subject_id"] == p for r in resolved_rows)
