"""Regression tests for workgraph_projects.py's deterministic auto-
grouping - previously untested despite doing real, consequential
automatic data merging. Added alongside a real bug fix (task #81): a
disjoint PR/PO reference number now vetoes a strong-signal merge, closing
a confirmed real production bug where 71 issues spanning 56+ distinct
purchase requisitions had merged into one project (proj-015) purely
because their subjects shared the boilerplate phrase "Action required:
Approve the Requisition that [name] submitted" - the company-disjoint
veto never fired because the only external party on any of them is
Ariba's own no-reply sender, which is correctly excluded from company
identification, leaving both sides with an empty company set."""
from __future__ import annotations

import time

import workgraph_projects as wp


def _issue(ws_db, title, opened_at=None):
    iid = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    if opened_at is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE issues SET opened_at = ? WHERE id = ?", (opened_at, iid))
        conn.close()
    return iid


def _raw_item(ws_db, issue_id, subject, key, from_actor="a@example.com"):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject=subject, from_actor=from_actor, participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    return rid


def _link_party(ws_db, issue_id, party_id, email, affiliation="external", company=None):
    ws_db.upsert_party(id=party_id, primary_email=email, display_name=party_id,
                        affiliation=affiliation, affiliation_confidence="H",
                        affiliation_source="domain", company=company)
    ws_db.link_party_to_issue(issue_id, party_id)


# --- _reference_ids_for_issue --------------------------------------------

def test_reference_ids_extracts_pr_number(ws_db):
    iid = _issue(ws_db, "Requisition")
    _raw_item(ws_db, iid, "Approve PR1111865 - SAP RISE", "r1")
    assert wp._reference_ids_for_issue(iid) == {"PR1111865"}


def test_reference_ids_extracts_versioned_pr_number(ws_db):
    iid = _issue(ws_db, "Requisition")
    _raw_item(ws_db, iid, "PR416079-V33 - Tower X PO Request", "r2")
    assert wp._reference_ids_for_issue(iid) == {"PR416079-V33"}


def test_reference_ids_extracts_po_number(ws_db):
    iid = _issue(ws_db, "PO notice")
    _raw_item(ws_db, iid, "Your PO4200703817 has shipped", "r3")
    assert wp._reference_ids_for_issue(iid) == {"PO4200703817"}


def test_reference_ids_empty_when_none_present(ws_db):
    iid = _issue(ws_db, "Just a normal email")
    _raw_item(ws_db, iid, "Let's catch up next week", "r4")
    assert wp._reference_ids_for_issue(iid) == set()


def test_reference_ids_union_across_multiple_raw_items(ws_db):
    iid = _issue(ws_db, "Multi")
    _raw_item(ws_db, iid, "PR1000001 first mention", "r5")
    _raw_item(ws_db, iid, "Follow-up on PR1000002", "r6")
    assert wp._reference_ids_for_issue(iid) == {"PR1000001", "PR1000002"}


# --- _vetoed_by_reference_mismatch ---------------------------------------

def test_veto_true_when_references_disjoint(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR1111865", "v1")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, b, "PR1193376", "v2")
    assert wp._vetoed_by_reference_mismatch(a, b) is True


def test_veto_false_when_references_match(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR1111865 approval needed", "v3")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, b, "Re: PR1111865 approval needed", "v4")
    assert wp._vetoed_by_reference_mismatch(a, b) is False


def test_veto_false_when_only_one_side_has_a_reference(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR1111865", "v5")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, b, "no reference here at all", "v6")
    assert wp._vetoed_by_reference_mismatch(a, b) is False


def test_veto_false_when_neither_side_has_a_reference(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "nothing structured here", "v7")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, b, "nor here either", "v8")
    assert wp._vetoed_by_reference_mismatch(a, b) is False


# --- group_issue: the real bug, reproduced and fixed ---------------------

_BOILERPLATE = "Action required: Approve the Requisition that {name} submitted - {pr}"


def test_boilerplate_subject_with_different_pr_numbers_does_not_merge(ws_db):
    """The exact real production bug (proj-015): two Ariba requisition-
    approval emails share nothing but the boilerplate subject template and
    the automated no-reply sender - different PR numbers must veto the
    merge that _shared_topic_key would otherwise produce."""
    a = _issue(ws_db, "Approve PR1111865")
    _raw_item(ws_db, a, _BOILERPLATE.format(name="BRIAN LAUGHLIN", pr="PR1111865"), "b1")
    _link_party(ws_db, a, "ariba", "no-reply@ansmtp.ariba.com")

    b = _issue(ws_db, "Approve PR1193376")
    _raw_item(ws_db, b, _BOILERPLATE.format(name="THOMAS TURNER", pr="PR1193376"), "b2")
    _link_party(ws_db, b, "ariba", "no-reply@ansmtp.ariba.com")

    result = wp.group_issue(a)

    assert result["action"] == "no_match"
    assert ws_db.get_issue(a)["project_id"] is None
    assert ws_db.get_issue(b)["project_id"] is None


def test_boilerplate_subject_with_same_pr_number_still_merges(ws_db):
    """The fix must not over-correct - two emails about the SAME
    requisition (e.g. an approval chain) should still merge on the
    matching subject core."""
    a = _issue(ws_db, "Approve PR1111865")
    _raw_item(ws_db, a, _BOILERPLATE.format(name="BRIAN LAUGHLIN", pr="PR1111865"), "b3")
    _link_party(ws_db, a, "ariba", "no-reply@ansmtp.ariba.com")

    b = _issue(ws_db, "Approve PR1111865 again")
    _raw_item(ws_db, b, _BOILERPLATE.format(name="SOMEONE ELSE", pr="PR1111865"), "b4")
    _link_party(ws_db, b, "ariba", "no-reply@ansmtp.ariba.com")

    result = wp.group_issue(a)

    assert result["action"] == "auto_merged"
    assert ws_db.get_issue(a)["project_id"] == ws_db.get_issue(b)["project_id"]


def test_shared_real_company_with_different_pr_numbers_does_not_merge(ws_db):
    """The veto applies to the shared-company strong signal too, not just
    the subject-text one - two threads with the same real supplier but
    different requisitions are still different transactions."""
    a = _issue(ws_db, "Deal A")
    _raw_item(ws_db, a, "PR1000001 - order details", "c1")
    _link_party(ws_db, a, "rep1", "rep1@acme.com", company="Acme")

    b = _issue(ws_db, "Deal B")
    _raw_item(ws_db, b, "PR1000002 - different order", "c2")
    _link_party(ws_db, b, "rep2", "rep2@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "no_match"


def test_shared_real_company_without_reference_numbers_still_merges(ws_db):
    """No PR/PO present on either side - the veto never applies, and the
    existing shared-company behavior is unchanged."""
    a = _issue(ws_db, "Deal A")
    _raw_item(ws_db, a, "Let's discuss the renewal", "c3")
    _link_party(ws_db, a, "rep1", "rep1@acme.com", company="Acme")

    b = _issue(ws_db, "Deal B")
    _raw_item(ws_db, b, "Following up on the renewal", "c4")
    _link_party(ws_db, b, "rep2", "rep2@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "auto_merged"


def test_weak_signal_candidates_exclude_disjoint_reference_pair(ws_db):
    now = time.time()
    a = _issue(ws_db, "Contract review A", opened_at=now)
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET category = 'contract' WHERE id = ?", (a,))
    _raw_item(ws_db, a, "PR1000001", "w1")

    b = _issue(ws_db, "Contract review B", opened_at=now)
    conn.execute("UPDATE issues SET category = 'contract' WHERE id = ?", (b,))
    conn.close()
    _raw_item(ws_db, b, "PR1000002", "w2")

    issue_a = ws_db.get_issue(a)
    candidates = wp._weak_signal_candidates(issue_a)

    assert all(c["id"] != b for c in candidates)


# --- hardening pass #2 fixes ----------------------------------------------

def _link_party(ws_db, issue_id, party_id, email, *, company=None, first_seen_ts=None):
    ws_db.upsert_party(id=party_id, primary_email=email, display_name=party_id,
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company=company)
    ws_db.link_party_to_issue(issue_id, party_id)
    if first_seen_ts is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE parties SET first_seen_ts = ? WHERE id = ?", (first_seen_ts, party_id))
        conn.close()


def test_shared_topic_key_searches_beyond_default_200_limit(ws_db, monkeypatch):
    """Regression for hardening pass #2: ws.list_issues' 200-row default
    silently capped this search on the real, larger dataset."""
    seen_limits = []
    real_list_issues = ws_db.list_issues

    def spy(*args, **kwargs):
        seen_limits.append(kwargs.get("limit"))
        return real_list_issues(*args, **kwargs)

    monkeypatch.setattr(ws_db, "list_issues", spy)
    iid = _issue(ws_db, "A reasonably long and distinctive subject core here")
    _link_party(ws_db, iid, "p1", "rep@acme.com")

    wp._shared_topic_key(ws_db.get_issue(iid))

    assert seen_limits and all((limit or 0) > 200 for limit in seen_limits)


def test_weak_signal_candidates_searches_beyond_default_200_limit(ws_db, monkeypatch):
    seen_limits = []
    real_list_issues = ws_db.list_issues

    def spy(*args, **kwargs):
        seen_limits.append(kwargs.get("limit"))
        return real_list_issues(*args, **kwargs)

    monkeypatch.setattr(ws_db, "list_issues", spy)
    iid = _issue(ws_db, "Some contract issue")
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET category = 'contract' WHERE id = ?", (iid,))
    conn.close()

    wp._weak_signal_candidates(ws_db.get_issue(iid))

    assert seen_limits and all((limit or 0) > 200 for limit in seen_limits)


def test_weak_signal_candidates_exclude_issue_already_in_any_project(ws_db):
    """Hardening pass #2: a candidate that already belongs to a DIFFERENT
    real project used to slip through (only the "already the exact same
    project" case was excluded) - this is what let a later confirm
    silently detach it via merge_issues. Now excluded at generation time
    regardless of which project it's already in."""
    now = time.time()
    other_project = ws_db.create_project_with_new_id(name="Existing project", category="contract")
    already_grouped = _issue(ws_db, "Already elsewhere", opened_at=now)
    ws_db.assign_issue_to_project(already_grouped, other_project)
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET category = 'contract' WHERE id = ?", (already_grouped,))
    conn.close()

    ungrouped = _issue(ws_db, "Ungrouped contract issue", opened_at=now)
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET category = 'contract' WHERE id = ?", (ungrouped,))
    conn.close()

    candidates = wp._weak_signal_candidates(ws_db.get_issue(ungrouped))

    assert all(c["id"] != already_grouped for c in candidates)


def test_merge_issues_creates_new_project_when_neither_has_one(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    project_id = wp.merge_issues(a, b, reason_label="test")
    assert ws_db.get_issue(a)["project_id"] == project_id
    assert ws_db.get_issue(b)["project_id"] == project_id


def test_merge_issues_joins_the_existing_project_when_one_side_has_one(ws_db):
    a = _issue(ws_db, "A")
    existing = ws_db.create_project_with_new_id(name="Existing", category="other")
    ws_db.assign_issue_to_project(a, existing)
    b = _issue(ws_db, "B")

    project_id = wp.merge_issues(a, b, reason_label="test")

    assert project_id == existing
    assert ws_db.get_issue(b)["project_id"] == existing


def test_merge_issues_collision_moves_every_member_and_archives_the_loser(ws_db):
    """The real bug hardening pass #2 found: merge_issues used to only
    ever consult issue_a's project, silently detaching issue_b out of
    whatever project IT was already in with no trace and no cleanup.
    Now: every member of the losing project moves to the winner, and the
    emptied loser is archived, not left active and misleading."""
    proj_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    a = _issue(ws_db, "A")
    ws_db.assign_issue_to_project(a, proj_a)

    proj_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    b = _issue(ws_db, "B")
    ws_db.assign_issue_to_project(b, proj_b)
    other_member_of_b = _issue(ws_db, "Other member of B's project")
    ws_db.assign_issue_to_project(other_member_of_b, proj_b)

    winner = wp.merge_issues(a, b, reason_label="test collision")

    assert winner == proj_a
    assert ws_db.get_issue(a)["project_id"] == proj_a
    assert ws_db.get_issue(b)["project_id"] == proj_a
    assert ws_db.get_issue(other_member_of_b)["project_id"] == proj_a, \
        "the OTHER member of the losing project must move too, not just b"
    assert ws_db.get_project(proj_b)["status"] == "archived"


def test_project_name_for_deterministic_tie_break_by_first_seen(ws_db):
    """Hardening pass #2: list_parties_for_issue has no ORDER BY, so
    picking a bare first match was non-deterministic when an issue has
    more than one identifiable external company. first_seen_ts ascending
    is a real, stable tie-break."""
    iid = _issue(ws_db, "Multi-party issue")
    _link_party(ws_db, iid, "later", "later@later.com", company="LaterCo", first_seen_ts=200.0)
    _link_party(ws_db, iid, "earlier", "earlier@earlier.com", company="EarlierCo", first_seen_ts=100.0)
    parties = ws_db.list_parties_for_issue(iid)

    name = wp._project_name_for(ws_db.get_issue(iid), "other", parties)

    assert name.startswith("EarlierCo")
