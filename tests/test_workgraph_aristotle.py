"""Regression tests for workgraph_aristotle.py (task #51) - a taught
prerequisite/gate check: a rule says a trigger signal_type shouldn't be
treated as ready to act on until a required signal_type has been seen for
the same project or supplier. Rules are only ever created via explicit
input (never inferred), so every test here creates rules directly through
workgraph_store's own CRUD functions - the same path Settings would use."""
import time

import workgraph_aristotle as ar


def _issue(ws_db, title="Issue", project_id=None):
    issue_id = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    if project_id:
        ws_db.update_issue(issue_id, project_id=project_id)
    return issue_id


def _raw_item(ws_db, issue_id, signal_type, key):
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com",
        participants_json="[]", body_preview="b",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", (signal_type, row_id))
    return row_id


def test_check_prerequisites_none_when_no_signal_types(ws_db):
    issue_id = _issue(ws_db)
    assert ar.check_prerequisites(issue_id, []) is None


def test_check_prerequisites_none_when_no_matching_rule(ws_db):
    issue_id = _issue(ws_db)
    _raw_item(ws_db, issue_id, "signature_requested_docusign", "k1")
    raw_items = ws_db.get_raw_items_for_issue(issue_id)
    assert ar.check_prerequisites(issue_id, raw_items) is None


def test_check_prerequisites_fires_when_unsatisfied_same_issue(ws_db):
    issue_id = _issue(ws_db)
    _raw_item(ws_db, issue_id, "signature_requested_docusign", "k1")
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved Ariba PO for this project", created_by="marc",
    )
    raw_items = ws_db.get_raw_items_for_issue(issue_id)

    result = ar.check_prerequisites(issue_id, raw_items)

    assert result is not None
    assert "No confirmation seen yet" in result["warning"]
    assert "an approved Ariba PO for this project" in result["warning"]


def test_check_prerequisites_satisfied_within_same_project(ws_db):
    proj = ws_db.create_project_with_new_id(name="Acme Deal")
    issue_a = _issue(ws_db, "Signature request", project_id=proj)
    issue_b = _issue(ws_db, "PO approval", project_id=proj)
    _raw_item(ws_db, issue_a, "signature_requested_docusign", "k1")
    _raw_item(ws_db, issue_b, "ariba_pr_fully_approved", "k2")
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved PO", created_by="marc",
    )
    raw_items = ws_db.get_raw_items_for_issue(issue_a)

    assert ar.check_prerequisites(issue_a, raw_items) is None


def test_check_prerequisites_unsatisfied_across_different_projects(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="Acme Deal")
    proj_b = ws_db.create_project_with_new_id(name="Unrelated Deal")
    issue_a = _issue(ws_db, "Signature request", project_id=proj_a)
    issue_b = _issue(ws_db, "PO approval", project_id=proj_b)
    _raw_item(ws_db, issue_a, "signature_requested_docusign", "k1")
    _raw_item(ws_db, issue_b, "ariba_pr_fully_approved", "k2")  # different project - shouldn't count
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved PO", created_by="marc",
    )
    raw_items = ws_db.get_raw_items_for_issue(issue_a)

    result = ar.check_prerequisites(issue_a, raw_items)
    assert result is not None


def test_check_prerequisites_satisfied_via_supplier_match(ws_db):
    conn = ws_db._connect()
    conn.execute(
        "INSERT INTO parties (id, primary_email, display_name, affiliation, company, first_seen_ts, last_seen_ts) "
        "VALUES ('p1','vendor@acme.com','Vendor Contact','external','Acme Corp', ?, ?)",
        (time.time(), time.time()),
    )
    issue_a = _issue(ws_db, "Signature request")
    issue_b = _issue(ws_db, "PO approval")
    ws_db.link_party_to_issue(issue_a, "p1")
    ws_db.link_party_to_issue(issue_b, "p1")
    _raw_item(ws_db, issue_a, "signature_requested_docusign", "k1")
    _raw_item(ws_db, issue_b, "ariba_pr_fully_approved", "k2")
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="supplier", reason="an approved PO", created_by="marc",
    )
    raw_items = ws_db.get_raw_items_for_issue(issue_a)

    assert ar.check_prerequisites(issue_a, raw_items) is None


def test_check_prerequisites_ignores_inactive_rule(ws_db):
    issue_id = _issue(ws_db)
    _raw_item(ws_db, issue_id, "signature_requested_docusign", "k1")
    rule_id = ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved PO", created_by="marc",
    )
    ws_db.set_prerequisite_rule_active(rule_id, False)
    raw_items = ws_db.get_raw_items_for_issue(issue_id)

    assert ar.check_prerequisites(issue_id, raw_items) is None


def _raw_item_at(ws_db, issue_id, signal_type, key, ts):
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=ts, subject="s", from_actor="a@example.com",
        participants_json="[]", body_preview="b",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", (signal_type, row_id))
    return row_id


def test_detect_candidate_rules_empty_with_fewer_than_two_signal_types(ws_db):
    issue = _issue(ws_db)
    _raw_item_at(ws_db, issue, "signature_requested_docusign", "k1", 100.0)
    assert ar.detect_candidate_rules() == []


def test_detect_candidate_rules_finds_consistent_pattern_across_two_projects(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="A")
    proj_b = ws_db.create_project_with_new_id(name="B")
    issue_a = _issue(ws_db, "A1", project_id=proj_a)
    issue_b = _issue(ws_db, "B1", project_id=proj_b)
    _raw_item_at(ws_db, issue_a, "ariba_pr_fully_approved", "a1", 100.0)
    _raw_item_at(ws_db, issue_a, "signature_requested_docusign", "a2", 200.0)
    _raw_item_at(ws_db, issue_b, "ariba_pr_fully_approved", "b1", 300.0)
    _raw_item_at(ws_db, issue_b, "signature_requested_docusign", "b2", 400.0)

    candidates = ar.detect_candidate_rules()

    matches = [c for c in candidates if c["match_on"] == "project"
               and c["trigger_signal_type"] == "signature_requested_docusign"
               and c["requires_signal_type"] == "ariba_pr_fully_approved"]
    assert len(matches) == 1
    assert matches[0]["observed_count"] == 2


def test_detect_candidate_rules_rejects_one_exception(ws_db):
    """One project where the order is reversed must kill the candidate
    entirely - consistency has to be 100%, not "mostly"."""
    proj_a = ws_db.create_project_with_new_id(name="A")
    proj_b = ws_db.create_project_with_new_id(name="B")
    issue_a = _issue(ws_db, "A1", project_id=proj_a)
    issue_b = _issue(ws_db, "B1", project_id=proj_b)
    _raw_item_at(ws_db, issue_a, "ariba_pr_fully_approved", "a1", 100.0)
    _raw_item_at(ws_db, issue_a, "signature_requested_docusign", "a2", 200.0)
    # reversed order in project B
    _raw_item_at(ws_db, issue_b, "signature_requested_docusign", "b1", 300.0)
    _raw_item_at(ws_db, issue_b, "ariba_pr_fully_approved", "b2", 400.0)

    candidates = ar.detect_candidate_rules()

    matches = [c for c in candidates if c["trigger_signal_type"] == "signature_requested_docusign"
               and c["requires_signal_type"] == "ariba_pr_fully_approved" and c["match_on"] == "project"]
    assert matches == []


def test_detect_candidate_rules_requires_minimum_sample_size(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="A")
    issue_a = _issue(ws_db, "A1", project_id=proj_a)
    _raw_item_at(ws_db, issue_a, "ariba_pr_fully_approved", "a1", 100.0)
    _raw_item_at(ws_db, issue_a, "signature_requested_docusign", "a2", 200.0)

    candidates = ar.detect_candidate_rules()

    matches = [c for c in candidates if c["match_on"] == "project"]
    assert matches == []  # only one project - below MIN_SAMPLE_GROUPS


def test_detect_candidate_rules_skips_already_active_rule(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="A")
    proj_b = ws_db.create_project_with_new_id(name="B")
    issue_a = _issue(ws_db, "A1", project_id=proj_a)
    issue_b = _issue(ws_db, "B1", project_id=proj_b)
    _raw_item_at(ws_db, issue_a, "ariba_pr_fully_approved", "a1", 100.0)
    _raw_item_at(ws_db, issue_a, "signature_requested_docusign", "a2", 200.0)
    _raw_item_at(ws_db, issue_b, "ariba_pr_fully_approved", "b1", 300.0)
    _raw_item_at(ws_db, issue_b, "signature_requested_docusign", "b2", 400.0)
    ws_db.create_prerequisite_rule(trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved", match_on="project", reason="x", created_by="marc")

    candidates = ar.detect_candidate_rules()

    matches = [c for c in candidates if c["trigger_signal_type"] == "signature_requested_docusign"
               and c["requires_signal_type"] == "ariba_pr_fully_approved" and c["match_on"] == "project"]
    assert matches == []


def test_detect_candidate_rules_skips_already_rejected_suggestion(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="A")
    proj_b = ws_db.create_project_with_new_id(name="B")
    issue_a = _issue(ws_db, "A1", project_id=proj_a)
    issue_b = _issue(ws_db, "B1", project_id=proj_b)
    _raw_item_at(ws_db, issue_a, "ariba_pr_fully_approved", "a1", 100.0)
    _raw_item_at(ws_db, issue_a, "signature_requested_docusign", "a2", 200.0)
    _raw_item_at(ws_db, issue_b, "ariba_pr_fully_approved", "b1", 300.0)
    _raw_item_at(ws_db, issue_b, "signature_requested_docusign", "b2", 400.0)
    sid = ws_db.create_prerequisite_suggestion(origin="detected", trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved", match_on="project", reason="x", evidence="x",
        raw_explanation=None, proposed_by="system")
    ws_db.resolve_prerequisite_suggestion(sid, "rejected")

    candidates = ar.detect_candidate_rules()

    matches = [c for c in candidates if c["trigger_signal_type"] == "signature_requested_docusign"
               and c["requires_signal_type"] == "ariba_pr_fully_approved" and c["match_on"] == "project"]
    assert matches == []


def test_detect_candidate_rules_supplier_match(ws_db):
    conn = ws_db._connect()
    conn.execute("INSERT INTO parties (id, primary_email, display_name, affiliation, company, first_seen_ts, last_seen_ts) "
                 "VALUES ('p1','a@acme.com','A','external','Acme Corp', ?, ?)", (time.time(), time.time()))
    conn.execute("INSERT INTO parties (id, primary_email, display_name, affiliation, company, first_seen_ts, last_seen_ts) "
                 "VALUES ('p2','b@bms.com','B','external','BMS Corp', ?, ?)", (time.time(), time.time()))
    issue_a = _issue(ws_db, "A1")
    issue_b = _issue(ws_db, "B1")
    ws_db.link_party_to_issue(issue_a, "p1")
    ws_db.link_party_to_issue(issue_b, "p2")
    _raw_item_at(ws_db, issue_a, "ariba_pr_fully_approved", "a1", 100.0)
    _raw_item_at(ws_db, issue_a, "signature_requested_docusign", "a2", 200.0)
    _raw_item_at(ws_db, issue_b, "ariba_pr_fully_approved", "b1", 300.0)
    _raw_item_at(ws_db, issue_b, "signature_requested_docusign", "b2", 400.0)

    candidates = ar.detect_candidate_rules()

    matches = [c for c in candidates if c["match_on"] == "supplier"
               and c["trigger_signal_type"] == "signature_requested_docusign"
               and c["requires_signal_type"] == "ariba_pr_fully_approved"]
    assert len(matches) == 1


def test_detect_and_log_candidates_daily_if_due_logs_and_gates(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="A")
    proj_b = ws_db.create_project_with_new_id(name="B")
    issue_a = _issue(ws_db, "A1", project_id=proj_a)
    issue_b = _issue(ws_db, "B1", project_id=proj_b)
    _raw_item_at(ws_db, issue_a, "ariba_pr_fully_approved", "a1", 100.0)
    _raw_item_at(ws_db, issue_a, "signature_requested_docusign", "a2", 200.0)
    _raw_item_at(ws_db, issue_b, "ariba_pr_fully_approved", "b1", 300.0)
    _raw_item_at(ws_db, issue_b, "signature_requested_docusign", "b2", 400.0)

    now = time.time()
    result = ar.detect_and_log_candidates_daily_if_due(now=now)
    assert result is not None
    assert result["logged"] >= 1

    pending = ws_db.list_prerequisite_suggestions("pending")
    assert any(p["origin"] == "detected" for p in pending)

    second = ar.detect_and_log_candidates_daily_if_due(now=now)
    assert second is None  # same day - gated


def test_check_prerequisites_falls_back_to_generic_wording_without_reason(ws_db):
    issue_id = _issue(ws_db)
    _raw_item(ws_db, issue_id, "signature_requested_docusign", "k1")
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="", created_by="marc",
    )
    raw_items = ws_db.get_raw_items_for_issue(issue_id)

    result = ar.check_prerequisites(issue_id, raw_items)
    assert "ariba_pr_fully_approved" in result["warning"]


# --- check_prerequisites_all (task #49, 2026-08-04) ----------------------
# check_prerequisites() itself is now a thin wrapper over this - every test
# above already covers the "first match" behavior stays unchanged; these
# cover the actual new capability, per docs/design/ARISTOTLE_PER_ROW_GATING.md.

def test_check_prerequisites_all_empty_when_nothing_triggers(ws_db):
    issue_id = _issue(ws_db)
    assert ar.check_prerequisites_all(issue_id, []) == []


def test_check_prerequisites_all_tags_result_with_raw_item_id(ws_db):
    issue_id = _issue(ws_db)
    row_id = _raw_item(ws_db, issue_id, "signature_requested_docusign", "k1")
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved Ariba PO", created_by="marc",
    )
    raw_items = ws_db.get_raw_items_for_issue(issue_id)

    results = ar.check_prerequisites_all(issue_id, raw_items)

    assert len(results) == 1
    assert results[0]["raw_item_id"] == row_id
    assert "an approved Ariba PO" in results[0]["warning"]


def test_check_prerequisites_all_collects_every_unsatisfied_match_not_just_first(ws_db):
    """The real gap check_prerequisites_all exists to close: two DIFFERENT
    triggering signal_types on one issue, each gated by its own unsatisfied
    rule - the old first-match-wins check_prerequisites would only ever
    report one of these."""
    issue_id = _issue(ws_db)
    row_a = _raw_item(ws_db, issue_id, "signature_requested_docusign", "k1")
    row_b = _raw_item(ws_db, issue_id, "invoice_dispute_raised", "k2")
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved Ariba PO", created_by="marc",
    )
    ws_db.create_prerequisite_rule(
        trigger_signal_type="invoice_dispute_raised",
        requires_signal_type="invoice_reconciled",
        match_on="project", reason="a reconciled invoice", created_by="marc",
    )
    raw_items = ws_db.get_raw_items_for_issue(issue_id)

    results = ar.check_prerequisites_all(issue_id, raw_items)

    assert len(results) == 2
    by_raw_item = {r["raw_item_id"]: r for r in results}
    assert "an approved Ariba PO" in by_raw_item[row_a]["warning"]
    assert "a reconciled invoice" in by_raw_item[row_b]["warning"]


def test_check_prerequisites_all_two_raw_items_same_rule_both_reported(ws_db):
    """Two separate DocuSign requests on one issue, same unsatisfied rule -
    each gets its own entry, never deduped away (design doc's own edge
    case: this is correct, not something to collapse to one)."""
    issue_id = _issue(ws_db)
    row_a = _raw_item(ws_db, issue_id, "signature_requested_docusign", "k1")
    row_b = _raw_item(ws_db, issue_id, "signature_requested_docusign", "k2")
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved Ariba PO", created_by="marc",
    )
    raw_items = ws_db.get_raw_items_for_issue(issue_id)

    results = ar.check_prerequisites_all(issue_id, raw_items)

    # Only the FIRST raw_item of a given signal_type is ever checked - see
    # checked_signal_types in the implementation, unchanged from the
    # original check_prerequisites. Confirming this stays true here too
    # (not a task #49 regression) rather than asserting len == 2, which
    # this specific case never produces.
    assert len(results) == 1
    assert results[0]["raw_item_id"] == row_a


def test_check_prerequisites_all_omits_satisfied_rules(ws_db):
    issue_id = _issue(ws_db)
    _raw_item(ws_db, issue_id, "signature_requested_docusign", "k1")
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved Ariba PO", created_by="marc",
    )
    _raw_item(ws_db, issue_id, "ariba_pr_fully_approved", "k2")
    raw_items = ws_db.get_raw_items_for_issue(issue_id)

    assert ar.check_prerequisites_all(issue_id, raw_items) == []


def test_check_prerequisites_still_returns_first_match_from_all(ws_db):
    issue_id = _issue(ws_db)
    row_a = _raw_item(ws_db, issue_id, "signature_requested_docusign", "k1")
    _raw_item(ws_db, issue_id, "invoice_dispute_raised", "k2")
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved Ariba PO", created_by="marc",
    )
    ws_db.create_prerequisite_rule(
        trigger_signal_type="invoice_dispute_raised",
        requires_signal_type="invoice_reconciled",
        match_on="project", reason="a reconciled invoice", created_by="marc",
    )
    raw_items = ws_db.get_raw_items_for_issue(issue_id)

    result = ar.check_prerequisites(issue_id, raw_items)

    assert result["raw_item_id"] == row_a
    assert "an approved Ariba PO" in result["warning"]


# --- task #67: gate_board -----------------------------------------------

def test_gate_board_empty_when_nothing_exists(ws_db):
    board = ar.gate_board()
    assert board == {"active": [], "pending": [], "inactive": []}


def test_gate_board_counts_a_currently_gated_issue(ws_db):
    issue_id = _issue(ws_db)
    _raw_item(ws_db, issue_id, "signature_requested_docusign", "gb1")
    rule_id = ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign", requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved PO", created_by="marc",
    )

    board = ar.gate_board()

    assert len(board["active"]) == 1
    assert board["active"][0]["id"] == rule_id
    assert board["active"][0]["currently_gating"] == 1


def test_gate_board_active_rule_with_zero_gated_issues_is_still_listed(ws_db):
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign", requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="x", created_by="marc",
    )

    board = ar.gate_board()

    assert len(board["active"]) == 1
    assert board["active"][0]["currently_gating"] == 0


def test_gate_board_satisfied_prerequisite_does_not_count_as_gating(ws_db):
    project_id = ws_db.create_project_with_new_id(name="P", category="other")
    trigger_issue = _issue(ws_db, project_id=project_id)
    requires_issue = _issue(ws_db, title="req", project_id=project_id)
    _raw_item(ws_db, trigger_issue, "signature_requested_docusign", "gb2")
    _raw_item(ws_db, requires_issue, "ariba_pr_fully_approved", "gb3")
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign", requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="x", created_by="marc",
    )

    board = ar.gate_board()

    assert board["active"][0]["currently_gating"] == 0


def test_gate_board_sorts_active_rules_by_currently_gating_descending(ws_db):
    rule_a = ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign", requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="x", created_by="marc",
    )
    rule_b = ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested", requires_signal_type="ariba_pr_approval_needed",
        match_on="project", reason="y", created_by="marc",
    )
    for i in range(3):
        issue_id = _issue(ws_db, title=f"gated-{i}")
        _raw_item(ws_db, issue_id, "signature_requested", f"gb4-{i}")

    board = ar.gate_board()

    assert [r["id"] for r in board["active"]] == [rule_b, rule_a]
    assert board["active"][0]["currently_gating"] == 3
    assert board["active"][1]["currently_gating"] == 0


def test_gate_board_includes_pending_suggestions(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved", match_on="project",
        reason="x", evidence=None, raw_explanation="x", proposed_by="marc",
    )

    board = ar.gate_board()

    assert len(board["pending"]) == 1
    assert board["pending"][0]["id"] == sid


def test_gate_board_excludes_resolved_suggestions(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved", match_on="project",
        reason="x", evidence=None, raw_explanation="x", proposed_by="marc",
    )
    ws_db.resolve_prerequisite_suggestion(sid, "rejected")

    board = ar.gate_board()

    assert board["pending"] == []


def test_gate_board_lists_deactivated_rules_as_inactive_without_gating_count(ws_db):
    rule_id = ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign", requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="x", created_by="marc",
    )
    ws_db.set_prerequisite_rule_active(rule_id, False)

    board = ar.gate_board()

    assert board["active"] == []
    assert len(board["inactive"]) == 1
    assert board["inactive"][0]["id"] == rule_id


# --- check_project_link_prerequisite (task #319) --------------------------
# A second, real source of gating signal: object-to-object project_links
# dependencies (workgraph_store.py), not Aristotle's own signal-type rules.
# Deliberately returns the same {"warning", ...} shape/WARNING_PREFIX as
# check_prerequisites above so it plugs into the exact same has_unmet_
# prerequisite mechanism (workgraph_nba.py) without a separate flag.

def test_check_project_link_prerequisite_none_when_issue_has_no_project(ws_db):
    issue_id = _issue(ws_db)
    assert ar.check_project_link_prerequisite(issue_id) is None


def test_check_project_link_prerequisite_none_when_no_links(ws_db):
    proj = ws_db.create_project_with_new_id(name="Solo Deal")
    issue_id = _issue(ws_db, project_id=proj)
    assert ar.check_project_link_prerequisite(issue_id) is None


def test_check_project_link_prerequisite_fires_on_unresolved_depends_on(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="New H1 Deal")
    proj_b = ws_db.create_project_with_new_id(name="Old H1 Exit")
    issue_id = _issue(ws_db, project_id=proj_a)
    ws_db.create_project_link(from_project_id=proj_a, to_project_id=proj_b,
                               link_type="depends_on", reason="needs the old contract exited first")

    result = ar.check_project_link_prerequisite(issue_id)

    assert result is not None
    assert result["warning"].startswith("No confirmation seen yet")
    assert "Old H1 Exit" in result["warning"]


def test_check_project_link_prerequisite_resolved_when_target_project_done(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="New H1 Deal")
    proj_b = ws_db.create_project_with_new_id(name="Old H1 Exit")
    issue_id = _issue(ws_db, project_id=proj_a)
    ws_db.create_project_link(from_project_id=proj_a, to_project_id=proj_b,
                               link_type="depends_on", reason="needs the old contract exited first")
    ws_db.set_project_status(proj_b, "done")

    assert ar.check_project_link_prerequisite(issue_id) is None


def test_check_project_link_prerequisite_fires_on_unresolved_blocks_from_other_side(ws_db):
    """A 'blocks' link is directional the other way: from_project BLOCKS
    to_project, so the issue's own project being the TO side (not the FROM
    side, unlike depends_on) is what makes it the blocked one."""
    proj_a = ws_db.create_project_with_new_id(name="Vendor Migration")
    proj_b = ws_db.create_project_with_new_id(name="New Contract Signature")
    issue_id = _issue(ws_db, project_id=proj_b)
    ws_db.create_project_link(from_project_id=proj_a, to_project_id=proj_b,
                               link_type="blocks", reason="migration must finish first")

    result = ar.check_project_link_prerequisite(issue_id)

    assert result is not None
    assert "Vendor Migration" in result["warning"]


def test_check_project_link_prerequisite_ignores_unrelated_link_type(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="A")
    proj_b = ws_db.create_project_with_new_id(name="B")
    issue_id = _issue(ws_db, project_id=proj_a)
    ws_db.create_project_link(from_project_id=proj_a, to_project_id=proj_b,
                               link_type="related", reason="same vendor, adjacent topic")

    assert ar.check_project_link_prerequisite(issue_id) is None


def test_check_project_link_prerequisite_ignores_wrong_direction_depends_on(ws_db):
    """proj_a depends_on proj_b means proj_a is gated, not proj_b - an issue
    on proj_b (the target, not the dependent) must never be flagged."""
    proj_a = ws_db.create_project_with_new_id(name="A")
    proj_b = ws_db.create_project_with_new_id(name="B")
    issue_id = _issue(ws_db, project_id=proj_b)
    ws_db.create_project_link(from_project_id=proj_a, to_project_id=proj_b,
                               link_type="depends_on", reason="a depends on b")

    assert ar.check_project_link_prerequisite(issue_id) is None
