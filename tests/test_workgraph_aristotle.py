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
