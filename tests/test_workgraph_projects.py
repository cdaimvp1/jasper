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

import json
import time

import pytest

import workgraph_projects as wp
import workgraph_signals


def _issue(ws_db, title, opened_at=None):
    iid = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    if opened_at is not None:
        conn = ws_db._connect()
        conn.execute("UPDATE issues SET opened_at = ? WHERE id = ?", (opened_at, iid))
        conn.close()
    return iid


def _raw_item(ws_db, issue_id, subject, key, from_actor="a@example.com"):
    """Task #83: reference_ids_for_issue now reads the persisted
    raw_items.pr_number field instead of re-scanning subject text live, so
    this helper populates it the same way workgraph_classify.classify_item
    really would - extracting from the given subject with the same shared
    regex - rather than the test relying on a live rescan that no longer
    happens."""
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject=subject, from_actor=from_actor, participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    m = workgraph_signals.REFERENCE_ID_RE.search(subject or "")
    if m:
        pr_number = m.group(0).upper()
        conn = ws_db._connect()
        conn.execute(
            "UPDATE raw_items SET pr_number = ?, pr_number_base = ? WHERE id = ?",
            (pr_number, workgraph_signals.reference_base(pr_number), rid),
        )
        conn.close()
    return rid


def _link_party(ws_db, issue_id, party_id, email, affiliation="external", company=None):
    ws_db.upsert_party(id=party_id, primary_email=email, display_name=party_id,
                        affiliation=affiliation, affiliation_confidence="H",
                        affiliation_source="domain", company=company)
    ws_db.link_party_to_issue(issue_id, party_id)


# --- reference_ids_for_issue --------------------------------------------

def test_reference_ids_extracts_pr_number(ws_db):
    iid = _issue(ws_db, "Requisition")
    _raw_item(ws_db, iid, "Approve PR1111865 - SAP RISE", "r1")
    assert wp.reference_ids_for_issue(iid) == {"PR1111865"}


def test_reference_ids_extracts_versioned_pr_number(ws_db):
    iid = _issue(ws_db, "Requisition")
    _raw_item(ws_db, iid, "PR416079-V33 - Tower X PO Request", "r2")
    assert wp.reference_ids_for_issue(iid) == {"PR416079-V33"}


def test_find_reference_id_collisions_flags_same_pr_on_ungrouped_issues(ws_db):
    """Enhancement idea panel #2: a real, visible signal - the same PR/PO
    base id on 2 issues that are NOT already in the same project, either
    because grouping hasn't caught up yet or because a v2.4 cannot_merge
    constraint deliberately blocked it."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, a, "Approve PR1111865 - SAP RISE", "coll1")
    _raw_item(ws_db, b, "Re: PR1111865 approval needed", "coll2")

    collisions = wp.find_reference_id_collisions_for_issue(a)

    assert len(collisions) == 1
    assert collisions[0]["issue_id"] == b
    assert collisions[0]["shared_reference_ids"] == ["PR1111865"]


def test_find_reference_id_collisions_excludes_same_project_issues(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    proj = ws_db.create_project_with_new_id(name="P", category="other")
    ws_db.assign_issue_to_project(a, proj)
    ws_db.assign_issue_to_project(b, proj)
    _raw_item(ws_db, a, "Approve PR2222222", "coll3")
    _raw_item(ws_db, b, "Re: PR2222222", "coll4")

    assert wp.find_reference_id_collisions_for_issue(a) == []


def test_find_reference_id_collisions_empty_when_no_reference(ws_db):
    a = _issue(ws_db, "A")
    assert wp.find_reference_id_collisions_for_issue(a) == []


# --- enhancement idea panel #14: reference-ID cross-check worker capability

def test_find_all_reference_id_collisions_flags_ungrouped_pair(ws_db):
    """The DB-wide sweep version of panel #2 - finds the same pair without
    the caller having to already know to check issue A."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, a, "Approve PR3333333 - SAP RISE", "sweep1")
    _raw_item(ws_db, b, "Re: PR3333333 approval needed", "sweep2")

    collisions = wp.find_all_reference_id_collisions()

    assert len(collisions) == 1
    pair = collisions[0]
    assert {pair["issue_a"], pair["issue_b"]} == {a, b}
    assert pair["shared_reference_ids"] == ["PR3333333"]


def test_find_all_reference_id_collisions_excludes_same_project_pair(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    proj = ws_db.create_project_with_new_id(name="P", category="other")
    ws_db.assign_issue_to_project(a, proj)
    ws_db.assign_issue_to_project(b, proj)
    _raw_item(ws_db, a, "Approve PR4444444", "sweep3")
    _raw_item(ws_db, b, "Re: PR4444444", "sweep4")

    assert wp.find_all_reference_id_collisions() == []


def test_find_all_reference_id_collisions_excludes_closed_issues(ws_db):
    """A collision with a done/dismissed issue is still visible via panel
    #2's per-issue lookup (audit intent), but the DB-wide sweep behind the
    proactive alert deliberately skips it - nothing to act on right now."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    ws_db.update_issue(b, state="done")
    _raw_item(ws_db, a, "Approve PR5555555", "sweep5")
    _raw_item(ws_db, b, "Re: PR5555555", "sweep6")

    assert wp.find_all_reference_id_collisions() == []


def test_find_all_reference_id_collisions_empty_when_no_shared_reference(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, a, "Approve PR6666666", "sweep7")
    _raw_item(ws_db, b, "Approve PR7777777", "sweep8")

    assert wp.find_all_reference_id_collisions() == []


def test_find_all_reference_id_collisions_handles_three_way_collision(ws_db):
    """Same reference on 3 different issues must produce 3 distinct pairs
    (a-b, a-c, b-c), each carrying the shared reference - not just one."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    c = _issue(ws_db, "C")
    _raw_item(ws_db, a, "Approve PR8888888", "sweep9")
    _raw_item(ws_db, b, "Re: PR8888888", "sweep10")
    _raw_item(ws_db, c, "Fwd: PR8888888", "sweep11")

    collisions = wp.find_all_reference_id_collisions()

    pairs_found = {frozenset((p["issue_a"], p["issue_b"])) for p in collisions}
    assert pairs_found == {frozenset((a, b)), frozenset((a, c)), frozenset((b, c))}


def test_reference_ids_extracts_po_number(ws_db):
    iid = _issue(ws_db, "PO notice")
    _raw_item(ws_db, iid, "Your PO4200703817 has shipped", "r3")
    assert wp.reference_ids_for_issue(iid) == {"PO4200703817"}


def test_reference_ids_empty_when_none_present(ws_db):
    iid = _issue(ws_db, "Just a normal email")
    _raw_item(ws_db, iid, "Let's catch up next week", "r4")
    assert wp.reference_ids_for_issue(iid) == set()


def test_reference_ids_union_across_multiple_raw_items(ws_db):
    iid = _issue(ws_db, "Multi")
    _raw_item(ws_db, iid, "PR1000001 first mention", "r5")
    _raw_item(ws_db, iid, "Follow-up on PR1000002", "r6")
    assert wp.reference_ids_for_issue(iid) == {"PR1000001", "PR1000002"}




def test_reference_base_ids_for_issue_strips_version(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR416079-V33 approval needed", "v13")
    assert wp.reference_base_ids_for_issue(a) == {"PR416079"}
    # display set is untouched - still the full versioned string
    assert wp.reference_ids_for_issue(a) == {"PR416079-V33"}




# --- related_open_issues_by_reference (checklist rework, 2026-08-01) ------

def test_related_open_issues_by_reference_returns_all_siblings_not_just_first(ws_db):
    """Real production case: PR854779 split across 3 separately-sent
    reminders/issues. _shared_reference_id stops at the first sibling
    (a grouping decision only needs one); this display-oriented function
    must surface all of them."""
    a = _issue(ws_db, "First notice")
    _raw_item(ws_db, a, "PR854779-V4 approval needed", "rr1")
    b = _issue(ws_db, "Reminder 1")
    _raw_item(ws_db, b, "REMINDER: PR854779-V4 still needs approval", "rr2")
    c = _issue(ws_db, "Reminder 2")
    _raw_item(ws_db, c, "SECOND REMINDER: PR854779-V4 still needs approval", "rr3")

    related = wp.related_open_issues_by_reference(a)

    assert {r["issue_id"] for r in related} == {b, c}
    assert all(r["shared_reference"] == "PR854779" for r in related)
    assert {r["title"] for r in related} == {"Reminder 1", "Reminder 2"}


def test_related_open_issues_by_reference_empty_when_no_reference(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "nothing structured here", "rr4")
    assert wp.related_open_issues_by_reference(a) == []


def test_related_open_issues_by_reference_empty_when_reference_unique(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR2222222 approval needed", "rr5")
    assert wp.related_open_issues_by_reference(a) == []


def test_related_open_issues_by_reference_ignores_closed_siblings(ws_db):
    a = _issue(ws_db, "Open one")
    _raw_item(ws_db, a, "PR3333333 approval needed", "rr6")
    b = ws_db.create_issue_with_new_id(title="Closed one", state="done", category="other")
    _raw_item(ws_db, b, "PR3333333 approval needed", "rr7")
    assert wp.related_open_issues_by_reference(a) == []


def test_related_open_issues_by_reference_dedupes_sibling_seen_via_two_references(ws_db):
    """A sibling sharing TWO of this issue's reference bases must appear
    once, not twice."""
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR4444444 approval needed", "rr8")
    _raw_item(ws_db, a, "PR5555555 approval needed", "rr9")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, b, "PR4444444 approval needed", "rr10")
    _raw_item(ws_db, b, "PR5555555 approval needed", "rr11")

    related = wp.related_open_issues_by_reference(a)

    assert len(related) == 1
    assert related[0]["issue_id"] == b


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








def test_merge_issues_creates_new_project_when_neither_has_one(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    result = wp.merge_issues(a, b, reason_label="test")
    assert result["status"] == "merged"
    project_id = result["project_id"]
    assert ws_db.get_issue(a)["project_id"] == project_id
    assert ws_db.get_issue(b)["project_id"] == project_id


def test_merge_issues_joins_the_existing_project_when_one_side_has_one(ws_db):
    a = _issue(ws_db, "A")
    existing = ws_db.create_project_with_new_id(name="Existing", category="other")
    ws_db.assign_issue_to_project(a, existing)
    b = _issue(ws_db, "B")

    result = wp.merge_issues(a, b, reason_label="test")

    assert result == {"status": "merged", "project_id": existing}
    assert ws_db.get_issue(b)["project_id"] == existing


def test_merge_issues_singleton_loser_still_auto_merges(ws_db):
    """2026-07-31 (step 5): a loser project whose only member is the issue
    being merged itself stays low-risk and still auto-merges."""
    proj_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    a = _issue(ws_db, "A")
    ws_db.assign_issue_to_project(a, proj_a)

    proj_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    b = _issue(ws_db, "B")
    ws_db.assign_issue_to_project(b, proj_b)

    result = wp.merge_issues(a, b, reason_label="test singleton")

    assert result == {"status": "merged", "project_id": proj_a}
    assert ws_db.get_issue(b)["project_id"] == proj_a
    assert ws_db.get_project(proj_b)["status"] == "archived"


def test_merge_issues_established_loser_defers_to_reconciliation(ws_db):
    """2026-07-31 (step 5, mandatory reconciliation): merge_issues() used
    to silently reassign every member of the losing project - now, when the
    loser has any REAL member beyond the issue being merged, it refuses to
    auto-collapse an established project and returns a 'deferred' status
    instead. (2026-08-07: this used to also defer to a 'merge_projects'
    pending_project_suggestions row - dropped along with that whole retired
    review queue, since nothing ever read the id back; workgraph_pipeline2.
    process_new_item's own "try the next candidate" handling of a deferred
    result needs nothing more than the status itself.)"""
    proj_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    a = _issue(ws_db, "A")
    ws_db.assign_issue_to_project(a, proj_a)

    proj_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    b = _issue(ws_db, "B")
    ws_db.assign_issue_to_project(b, proj_b)
    other_member_of_b = _issue(ws_db, "Other member of B's project")
    ws_db.assign_issue_to_project(other_member_of_b, proj_b)

    result = wp.merge_issues(a, b, reason_label="test collision")

    assert result == {"status": "deferred", "winner_project_id": proj_a, "loser_project_id": proj_b}
    # NOTHING actually merged - real projects must not be silently collapsed.
    assert ws_db.get_issue(a)["project_id"] == proj_a
    assert ws_db.get_issue(b)["project_id"] == proj_b
    assert ws_db.get_issue(other_member_of_b)["project_id"] == proj_b
    assert ws_db.get_project(proj_b)["status"] != "archived"


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


# --- Part A2 (2026-07-30): weighted multi-signal scoring model -----------



def _matched_points_pair(a, b):
    """Task #184 (2026-08-04) helper: builds both sides' cached signatures +
    topic keys and runs them through _matched_data_points, the same
    sequence scored_grouping_decision/backtest_scored_model now use -
    retired _score_pair/_pairwise_score_from_signature (the weighted-score
    model this replaces) called for this directly; keeping ONE real
    matching path is the whole point of the signature model (see
    compute_work_object_signature's docstring)."""
    issue_a, issue_b = wp.ws.get_issue(a), wp.ws.get_issue(b)
    sig_a = wp.get_or_compute_work_object_signature(a, issue_a)
    sig_b = wp.get_or_compute_work_object_signature(b, issue_b)
    topic_a = wp._topic_key_for_signature(issue_a, sig_a)
    topic_b = wp._topic_key_for_signature(issue_b, sig_b)
    return wp._matched_data_points(a, sig_a, topic_a, b, sig_b, topic_b)


def test_matched_data_points_single_point_is_not_enough(ws_db):
    """Marc's count-based rule (task #184): one matched data point (a
    shared supplier alone here) is real evidence of SOMETHING, but never
    enough on its own - the 2-or-more floor lives in the caller
    (scored_grouping_decision), this function just reports what matched."""
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B, unrelated subject entirely")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    points = _matched_points_pair(a, b)

    assert points == ["supplier"]
    assert len(points) < 2


def test_matched_data_points_supplier_and_subject_entity_combine_to_two_points(ws_db):
    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    points = _matched_points_pair(a, b)

    assert set(points) == {"supplier", "subject_entity"}
    assert len(points) >= 2


# --- "cross_mention" point type (task #335, per #324's design doc) -------
# Closes the real, confirmed gap: a prime/subcontractor pair with two
# DIFFERENT company names only ever earns one structured point ("supplier"
# doesn't fire when the two sides' companies are disjoint) - the real
# Scriptly PV1 / Scriptly-Sodalis bridge / direct Sodalis MSA case
# (docs/design/CANDIDATE_DETECTION_BROADENING.md) never cleared the
# 2-point gate for exactly this reason, even though the bridge item's own
# text plainly named the relationship ("the existing Scriptly subcontract").

def test_matched_data_points_cross_mention_fires_on_disjoint_suppliers(ws_db):
    """The exact gap this closes: companies are disjoint (no "supplier"
    point), but one side's own text names the OTHER side's company next
    to real relationship language - reported as its own point, fully
    auditable (the literal company + keyword are embedded in the string)."""
    a = _issue(ws_db, "Scriptly PV1 work order")
    _link_party(ws_db, a, "p1", "rep@scriptly.example", company="Scriptly")
    b = _issue(ws_db, "Sodalis MSA change order")
    _link_party(ws_db, b, "p2", "rep@sodalis.example", company="Sodalis")
    _raw_item_with_body(
        ws_db, b, "Sodalis MSA change order", "cm1",
        body_preview="This change order continues the existing Scriptly subcontract arrangement.",
        from_actor="rep@sodalis.example",
    )

    points = _matched_points_pair(a, b)

    assert "supplier" not in points  # disjoint companies - confirms this isn't just riding along
    assert "cross_mention:scriptly (subcontract)" in points


def test_matched_data_points_cross_mention_requires_relationship_keyword(ws_db):
    """A bare company mention with no nearby relationship language must
    NOT fire - this is the guard against treating any coincidental mention
    of a known vendor's name as evidence of a real relationship."""
    a = _issue(ws_db, "Scriptly PV1 work order")
    _link_party(ws_db, a, "p1", "rep@scriptly.example", company="Scriptly")
    b = _issue(ws_db, "Sodalis MSA change order")
    _link_party(ws_db, b, "p2", "rep@sodalis.example", company="Sodalis")
    _raw_item_with_body(
        ws_db, b, "Sodalis MSA change order", "cm2",
        body_preview="Scriptly are a completely unrelated vendor we also happen to use.",
        from_actor="rep@sodalis.example",
    )

    points = _matched_points_pair(a, b)

    assert not any(p.startswith("cross_mention:") for p in points)


def test_matched_data_points_cross_mention_checks_both_directions(ws_db):
    """The mention can live on EITHER side's text - not just the side
    whose own company is being searched for."""
    a = _issue(ws_db, "Scriptly PV1 work order")
    _raw_item_with_body(
        ws_db, a, "Scriptly PV1 work order", "cm3",
        body_preview="This work order is issued as a flow-down under the direct Sodalis MSA.",
        from_actor="rep@scriptly.example",
    )
    _link_party(ws_db, a, "p1", "rep@scriptly.example", company="Scriptly")
    b = _issue(ws_db, "Sodalis MSA change order")
    _link_party(ws_db, b, "p2", "rep@sodalis.example", company="Sodalis")

    points = _matched_points_pair(a, b)

    assert "cross_mention:sodalis (flow-down)" in points


def test_matched_data_points_category_is_never_a_point_type(ws_db):
    """Category is dropped entirely as a point type (per _matched_data_
    points' own docstring) - an internal taxonomy tag, not extracted
    evidence, per Marc's own list of real data points. Two issues sharing
    nothing but the same category value must match on nothing at all."""
    a = _issue(ws_db, "A")
    ws_db.update_issue(a, category="other")
    b = _issue(ws_db, "B")
    ws_db.update_issue(b, category="other")

    assert _matched_points_pair(a, b) == []


def test_matched_data_points_disjoint_reference_no_longer_vetoes_other_points(ws_db):
    """Retracted 2026-08-05 (Marc's direct correction, live on the
    Authenticx case): a disjoint reference used to veto EVERYTHING - now
    it just means "reference" itself isn't one of the counted points;
    other real matched points (here: supplier + subject_entity) still
    count normally, surfacing this pair as a real candidate for curator/
    human review rather than blocking it outright."""
    a = _issue(ws_db, "Action required: Approve the Requisition that X submitted")
    _raw_item(ws_db, a, "PR111111", "pa1")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that X submitted")
    _raw_item(ws_db, b, "PR222222", "pa2")
    _link_party(ws_db, b, "p2", "rep2@acme.com", company="Acme")

    points = _matched_points_pair(a, b)

    assert "reference" not in points
    assert "supplier" in points
    assert "subject_entity" in points


def test_matched_data_points_cannot_merge_constraint_vetoes_everything(ws_db):
    """Section 12.7's real veto: a durable cannot_merge (v2.4) must return
    an empty match list outright, same absolute-override treatment as a
    disjoint reference ID - closing the real gap where scored_grouping_
    decision's auto_merge path bypassed create_project_suggestion (and
    therefore v2.4's own check) by calling merge_issues_txn directly."""
    a = _issue(ws_db, "Action required: Approve the Requisition that X submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that X submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")
    ws_db.create_identity_constraint("cannot_merge", a, b, "confirmed separate", actor="marc")

    assert _matched_points_pair(a, b) == []


# --- real labeled "Supplier:" body field folded into "supplier" point -----
# (2026-08-05, Marc's direct design ask, live on the Authenticx case, then
# generalized same day per his direct correction to work for any automated
# system, not just Ariba): a system-routed communication's only sender is
# correctly excluded from party/company matching by is_automated_sender, so
# two different Authenticx PRs used to share NO supplier signal at all -
# real vendor or not. _system_party_for_work_object now reads the real
# counterparty out of each linked raw_item's own body text, and
# _matched_data_points folds it into the SAME "supplier" point as a
# tracked party's company, normalized so casing/corporate-suffix
# differences don't matter.

def _raw_item_with_body(ws_db, issue_id, subject, key, body_preview, from_actor="no-reply@ansmtp.ariba.com"):
    """Same real pr_number persistence _raw_item above uses, plus a real
    body_preview (resolve_item_text falls back to it when raw_ref is
    unset, exactly the pre-task-#43 shape) so the Ariba supplier extractor
    has real body text to read."""
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject=subject, from_actor=from_actor,
        participants_json="[]", body_preview=body_preview,
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    m = workgraph_signals.REFERENCE_ID_RE.search(subject or "")
    if m:
        pr_number = m.group(0).upper()
        conn = ws_db._connect()
        conn.execute(
            "UPDATE raw_items SET pr_number = ?, pr_number_base = ? WHERE id = ?",
            (pr_number, workgraph_signals.reference_base(pr_number), rid),
        )
        conn.close()
    return rid


def test_compute_work_object_signature_reads_real_ariba_supplier_from_body(ws_db):
    a = _issue(ws_db, "Action required: Approve the Requisition that ALICIA MORRIS submitted  - PR854779-V4 - Conversational AI ($1,938,100.00 USD)")
    _raw_item_with_body(
        ws_db, a, "PR854779-V4", "pa1",
        "Description Authenticx will enable Eli Lilly to analyze call recordings. "
        "Supplier AUTHENTICX INC Qty 1.00 Unit Power Unit Price $1,575,600.00 USD",
    )

    sig = wp.compute_work_object_signature(a, ws_db.get_issue(a))

    assert sig["positive_vocabulary"]["system_party"] == "AUTHENTICX INC"
    assert sig["external_orgs"] == []  # is_automated_sender excludes the Ariba sender - no party signal at all


def test_matched_data_points_ariba_supplier_alone_creates_supplier_point(ws_db):
    """Two different, otherwise-unrelated Authenticx PRs (different PR#,
    different requester) now share a real supplier point purely from each
    one's own body field - the exact gap that kept them from ever becoming
    candidates for the same project."""
    a = _issue(ws_db, "Action required: Approve the Requisition that ALICIA MORRIS submitted  - PR854779-V4 - Conversational AI ($1,938,100.00 USD)")
    _raw_item_with_body(ws_db, a, "PR854779-V4", "pa1", "Supplier AUTHENTICX INC Qty 1.00")
    b = _issue(ws_db, "Action required: Approve the Requisition that CLAUDIA HERNANDEZ submitted  - PR1175200 - Omvoh Olumiant Ebglyss ($500,000.00 USD)")
    _raw_item_with_body(ws_db, b, "PR1175200", "pb1", "Supplier AUTHENTICX INC Qty 1.00")

    points = _matched_points_pair(a, b)

    assert "reference" not in points  # different real PR#s, no shared reference
    assert "supplier" in points


def test_matched_data_points_ariba_supplier_normalizes_against_tracked_party_company(ws_db):
    """The Ariba field's formal "AUTHENTICX INC" and a tracked party's own
    "Authenticx" company name must compare as the same real vendor."""
    a = _issue(ws_db, "Action required: Approve the Requisition that X submitted")
    _raw_item_with_body(ws_db, a, "PR1", "pa1", "Supplier AUTHENTICX INC Qty 1.00")
    b = _issue(ws_db, "Cameron Hilt - Authenticx relationship check-in")
    _link_party(ws_db, b, "cameron", "cameron.hilt@authenticx.com", company="Authenticx")

    points = _matched_points_pair(a, b)

    assert "supplier" in points


def test_matched_data_points_no_supplier_point_when_suppliers_differ(ws_db):
    a = _issue(ws_db, "Action required: Approve the Requisition that X submitted")
    _raw_item_with_body(ws_db, a, "PR1", "pa1", "Supplier AUTHENTICX INC Qty 1.00")
    b = _issue(ws_db, "Action required: Approve the Requisition that Y submitted")
    _raw_item_with_body(ws_db, b, "PR2", "pb1", "Supplier WORKDAY INC Qty 1.00")

    assert "supplier" not in _matched_points_pair(a, b)


def test_compute_work_object_signature_malformed_body_never_names_ariba_itself_as_supplier(ws_db):
    """Defensive floor - if a body somehow put the transport system's own
    name in the Supplier field, it must never surface as a real vendor
    signal (Marc's own words: 'not ariba/sap')."""
    a = _issue(ws_db, "Some malformed requisition notification")
    _raw_item_with_body(ws_db, a, "PR1", "pa1", "Supplier Ariba Qty 1.00")

    sig = wp.compute_work_object_signature(a, ws_db.get_issue(a))

    assert (sig["positive_vocabulary"] or {}).get("system_party") is None


# --- work_object_signatures caching (Section 12.7) ------------------------

def test_get_or_compute_work_object_signature_caches_the_result(ws_db):
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    assert ws_db.get_work_object_signature(a) is None  # nothing cached yet

    sig = wp.get_or_compute_work_object_signature(a, ws_db.get_issue(a))

    assert sig["external_orgs"] == ["acme"]
    cached = ws_db.get_work_object_signature(a)
    assert cached is not None
    assert json.loads(cached["external_orgs"]) == ["acme"]


# --- signature cache schema-version invalidation (2026-08-05, real live --
# bug: 355/361 real issues had a cached row from before ariba_supplier
# existed, and every one was trusted forever since nothing about a DATA
# write ever touched them) ---------------------------------------------

def test_get_or_compute_work_object_signature_stale_schema_version_recomputes(ws_db):
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    # Simulate a pre-fix cached row (schema_version defaults to 0, same as
    # every real row that predates this column existing at all).
    ws_db.upsert_work_object_signature(
        a, definitive_ids_json="[]", accepted_lineages_json="[]", containers_json="[]",
        external_orgs_json='["stale value"]', participant_roles_json="[]",
        active_period_start=None, active_period_end=None,
        positive_vocabulary_json=None, negative_vocabulary_json=None, cannot_link_ids_json="[]",
    )
    cached_before = ws_db.get_work_object_signature(a)
    assert cached_before["schema_version"] == 0

    sig = wp.get_or_compute_work_object_signature(a, ws_db.get_issue(a))

    assert sig["external_orgs"] == ["acme"]  # real recompute, not the stale cached value
    cached_after = ws_db.get_work_object_signature(a)
    assert cached_after["schema_version"] == wp._SIGNATURE_SCHEMA_VERSION


def test_get_or_compute_work_object_signature_current_version_is_a_real_cache_hit(ws_db, monkeypatch):
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    wp.get_or_compute_work_object_signature(a, ws_db.get_issue(a))  # populates cache at current version

    calls = []
    real_compute = wp.compute_work_object_signature
    monkeypatch.setattr(wp, "compute_work_object_signature",
                         lambda *a_, **kw: (calls.append(1), real_compute(*a_, **kw))[1])

    wp.get_or_compute_work_object_signature(a, ws_db.get_issue(a))

    assert calls == []  # a real cache hit never calls compute_work_object_signature again


def test_compute_work_object_signature_reports_real_accepted_lineages(ws_db):
    """Section 12.5/12.7 coordination: accepted_lineages started as an
    honest [] before artifact_lineages existed - now that it does, this
    must be real, populated data, not left behind as a stale placeholder."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    ws_db.create_attachment(
        entity_type="issue", entity_id=a, kind="upload", filename="f1.pdf",
        stored_path="p1.pdf", content_type=None, size_bytes=10, sha256_hex="hlineage", uploaded_by="marc",
    )
    ws_db.create_attachment(
        entity_type="issue", entity_id=b, kind="upload", filename="f2.pdf",
        stored_path="p2.pdf", content_type=None, size_bytes=10, sha256_hex="hlineage", uploaded_by="marc",
    )

    sig = wp.compute_work_object_signature(a, ws_db.get_issue(a))

    assert sig["accepted_lineages"] == ["lineage-hlineage"]


def test_link_party_to_issue_invalidates_cached_signature(ws_db):
    a = _issue(ws_db, "A")
    wp.get_or_compute_work_object_signature(a, ws_db.get_issue(a))
    assert ws_db.get_work_object_signature(a) is not None

    ws_db.link_party_to_issue(a, "p1")

    assert ws_db.get_work_object_signature(a) is None  # invalidated, not stale




def test_merge_issue_into_invalidates_both_cached_signatures(ws_db):
    winner = _issue(ws_db, "Winner")
    loser = _issue(ws_db, "Loser")
    wp.get_or_compute_work_object_signature(winner, ws_db.get_issue(winner))
    wp.get_or_compute_work_object_signature(loser, ws_db.get_issue(loser))

    ws_db.merge_issue_into(loser, winner, reason="test", actor="marc")

    assert ws_db.get_work_object_signature(winner) is None
    assert ws_db.get_work_object_signature(loser) is None


# --- aggregate_parties_for_project (project-detail redesign, 2026-07-31) --

def _project_with_issues(ws_db, *issue_ids):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    for iid in issue_ids:
        ws_db.assign_issue_to_project(iid, pid)
    return pid


def test_aggregate_parties_dedupes_across_member_issues(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    _link_party(ws_db, a, "shared", "rep@acme.com", company="Acme")
    _link_party(ws_db, b, "shared", "rep@acme.com", company="Acme")
    pid = _project_with_issues(ws_db, a, b)

    parties = wp.aggregate_parties_for_project(pid)

    assert len(parties) == 1
    assert parties[0]["id"] == "shared"
    assert parties[0]["issue_count"] == 2


def test_aggregate_parties_marks_most_linked_as_primary(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    c = _issue(ws_db, "C")
    _link_party(ws_db, a, "frequent", "freq@acme.com", company="Acme")
    _link_party(ws_db, b, "frequent", "freq@acme.com", company="Acme")
    _link_party(ws_db, c, "rare", "rare@acme.com", company="Acme")
    pid = _project_with_issues(ws_db, a, b, c)

    parties = wp.aggregate_parties_for_project(pid)
    by_id = {p["id"]: p for p in parties}

    assert by_id["frequent"]["issue_count"] == 2
    assert by_id["frequent"]["is_primary"] is True
    assert by_id["rare"]["is_primary"] is False


def test_aggregate_parties_marks_primary_separately_per_affiliation(ws_db):
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "ext1", "rep@acme.com", company="Acme")
    ws_db.upsert_party(id="int1", primary_email="colleague@lilly.com", display_name="Colleague",
                        affiliation="internal", affiliation_confidence="H", affiliation_source="domain", company=None)
    ws_db.link_party_to_issue(a, "int1")
    pid = _project_with_issues(ws_db, a)

    parties = wp.aggregate_parties_for_project(pid)
    by_id = {p["id"]: p for p in parties}

    assert by_id["ext1"]["is_primary"] is True
    assert by_id["int1"]["is_primary"] is True


def test_aggregate_parties_empty_project_returns_empty_list(ws_db):
    pid = ws_db.create_project_with_new_id(name="Empty", category="other")
    assert wp.aggregate_parties_for_project(pid) == []


# --- content-extracted matched data points (task #169/#170, then #184, ----
# 2026-08-04) - ariba_descriptor -> "product_service", ariba_requester ->
# folded into "stakeholder", value_amount -> "amount", accepted_lineages ->
# "document". Added to _matched_data_points so "supplier + one other real
# data point" (Marc's stated rule) actually has enough combinable points for
# Ariba's automated notifications specifically - is_automated_sender already
# excludes the notification address itself from party/company matching, so
# without these, two different Ariba requisitions (or two versions of the
# same one) looked identical to the signature.

def _sig(**overrides):
    base = {
        "definitive_ids": [], "accepted_lineages": [], "containers": [],
        "external_orgs": [], "participant_roles": [], "active_period_start": None,
        "active_period_end": None, "positive_vocabulary": None, "negative_vocabulary": None,
        "cannot_link_ids": [],
    }
    base.update(overrides)
    return base


def test_matched_data_points_ariba_descriptor_and_requester_together_is_two_points():
    a = _sig(positive_vocabulary={"ariba_requester": "Thomas Turner", "ariba_descriptor": "Workday HCM SaaS", "value_amount": None})
    b = _sig(positive_vocabulary={"ariba_requester": "Thomas Turner", "ariba_descriptor": "Workday HCM SaaS", "value_amount": None})
    points = wp._matched_data_points("a", a, "", "b", b, "")
    assert "product_service" in points
    assert "stakeholder" in points
    assert len(points) >= 2


def test_matched_data_points_product_service_alone_is_one_point():
    a = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": "Workday HCM SaaS", "value_amount": None})
    b = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": "Workday HCM SaaS", "value_amount": None})
    points = wp._matched_data_points("a", a, "", "b", b, "")
    assert points == ["product_service"]


def test_matched_data_points_product_service_plus_amount_is_two_points():
    a = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": "Workday HCM SaaS", "value_amount": 53702143.0})
    b = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": "Workday HCM SaaS", "value_amount": 53702143.0})
    points = wp._matched_data_points("a", a, "", "b", b, "")
    assert "amount" in points
    assert "product_service" in points
    assert len(points) >= 2


def test_matched_data_points_amount_requires_close_match_not_exact():
    """A 1% tolerance is real-world tolerant (rounding, currency
    conversion noise) without being so loose two unrelated deals coincide."""
    a = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": None, "value_amount": 100000.0})
    b = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": None, "value_amount": 100500.0})
    points = wp._matched_data_points("a", a, "", "b", b, "")
    assert "amount" in points

    c = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": None, "value_amount": 150000.0})
    points2 = wp._matched_data_points("a", a, "", "c", c, "")
    assert "amount" not in points2


def test_matched_data_points_document_lineage_overlap_matches():
    a = _sig(accepted_lineages=["lineage-abc123"])
    b = _sig(accepted_lineages=["lineage-abc123", "lineage-def456"])
    points = wp._matched_data_points("a", a, "", "b", b, "")
    assert "document" in points


def test_matched_data_points_no_points_when_vocab_empty():
    a = _sig()
    b = _sig()
    assert wp._matched_data_points("a", a, "", "b", b, "") == []


# (task #169/#170, 2026-08-04, Marc's direct design ask). The OLD
# scored_grouping_decision excluded any candidate already in a DIFFERENT
# project than the issue being scored - meaning an ungrouped item could
# NEVER join an existing, already-established project via this path, and a
# real chain (A-B share 2 points, B-C share 2 DIFFERENT points) could never
# be discovered once B had a project. Now searches the whole corpus and
# tracks the best match PER distinct project, so it can also detect when an
# item bridges two already-separate, already-established projects.









def test_split_issue_from_project_detaches_and_resets_membership(ws_db):
    """Task #178: the safety valve Marc asked for alongside the more
    aggressive matching model. Splitting an issue back out of a project
    it's confirmed-merged into should leave it standalone again, exactly
    like an issue that never matched anything."""
    p1 = ws_db.create_project_with_new_id(name="Project one", category="other")
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    ws_db.assign_issue_to_project(a, p1)
    ws_db.assign_issue_to_project(b, p1)
    ws_db.confirm_work_object_membership(a)
    ws_db.confirm_work_object_membership(b)

    result = wp.split_issue_from_project(a, reason="wrong merge, different suppliers")

    assert result["action"] == "split"
    assert result["old_project_id"] == p1
    assert ws_db.get_issue(a)["project_id"] is None
    assert ws_db.get_work_object_membership_exposure(a)["membership_state"] == "provisional"
    # b, left behind, keeps its own confirmed state and project untouched.
    assert ws_db.get_issue(b)["project_id"] == p1
    assert ws_db.get_work_object_membership_exposure(b)["membership_state"] == "confirmed"


def test_split_issue_from_project_vetoes_re_merge_with_former_members(ws_db):
    """The detach alone isn't the safety valve - without a durable veto, the
    very next classify/grouping cycle would just re-match the same
    signature and merge it right back in. This checks the REAL consumer
    (compute_work_object_signature's cannot_link_ids, same field
    _matched_data_points' veto reads) not just that a row got written."""
    p1 = ws_db.create_project_with_new_id(name="Project one", category="other")
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    ws_db.assign_issue_to_project(a, p1)
    ws_db.assign_issue_to_project(b, p1)

    result = wp.split_issue_from_project(a)

    assert result["constraints_created"] == [b]
    assert ws_db.find_identity_constraint("cannot_merge", a, b) is not None
    sig = wp.compute_work_object_signature(a)
    assert b in sig["cannot_link_ids"]


def test_split_issue_from_project_is_a_noop_on_an_ungrouped_issue(ws_db):
    a = _issue(ws_db, "A")
    result = wp.split_issue_from_project(a)
    assert result["action"] == "not_grouped"


def test_split_issue_from_project_skips_existing_constraints(ws_db):
    """Re-splitting (or splitting after a prior manual constraint already
    exists between this pair) shouldn't duplicate the identity_constraints
    row - find_identity_constraint is checked before create, same pattern
    reject_suggestion already uses."""
    p1 = ws_db.create_project_with_new_id(name="Project one", category="other")
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    ws_db.assign_issue_to_project(a, p1)
    ws_db.assign_issue_to_project(b, p1)
    ws_db.create_identity_constraint("cannot_merge", a, b, reason="pre-existing", actor="marc")

    result = wp.split_issue_from_project(a)

    assert result["constraints_created"] == []


def _cluster(ws_db, title):
    return ws_db.create_cluster_with_new_id(title=title, category="other")


# --- Corrected pipeline Phase D (2026-08-05): curator's real issue-------
# extraction step (extract_issue_from_project) ------------------------------

def _claim_on(ws_db, work_object_id, text, claim_type="ask", raw_item_id=None):
    if raw_item_id is None:
        raw_item_id = _raw_item(ws_db, work_object_id, text, f"claim-src-{time.time()}")
    return ws_db.insert_claim(
        issue_id=work_object_id, raw_item_id=raw_item_id, claim_type=claim_type,
        text=text, author="counterparty", author_basis="direction",
    )


def test_extract_issue_from_project_creates_a_real_issue_and_moves_cited_claims(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    cid = _cluster(ws_db, "Recurring meeting cluster")
    ws_db.assign_issue_to_project(cid, pid)
    rid = _raw_item(ws_db, cid, "Weekly sync notes", "extract1")
    claim_id = _claim_on(ws_db, cid, "please send updated pricing", raw_item_id=rid)

    result = wp.extract_issue_from_project(pid, title="Pricing negotiation", category="financial", claim_ids=[claim_id])

    new_issue_id = result["issue_id"]
    assert result["claims_moved"] == 1
    assert result["evidence_added"] == 1
    new_issue = ws_db.get_issue(new_issue_id)
    assert new_issue is not None
    assert new_issue["project_id"] == pid
    assert new_issue["title"] == "Pricing negotiation"
    moved_claim = ws_db.get_claim(claim_id)
    assert moved_claim["issue_id"] == new_issue_id
    # the cluster it came from still exists, untouched, still a cluster -
    # this is a claim-level move, never a whole-cluster promotion.
    assert ws_db.get_cluster(cid) is not None


def test_extract_issue_from_project_only_moves_cited_claims_not_the_whole_cluster(ws_db):
    """A single cluster can carry the material for more than one real
    issue (Marc's own Authenticz example) - extracting one issue must
    leave an uncited claim on the SAME cluster exactly where it was."""
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    cid = _cluster(ws_db, "Recurring meeting cluster")
    ws_db.assign_issue_to_project(cid, pid)
    pricing_claim = _claim_on(ws_db, cid, "please send updated pricing")
    onboarding_claim = _claim_on(ws_db, cid, "confirm onboarding scope")

    result = wp.extract_issue_from_project(pid, title="Pricing negotiation", claim_ids=[pricing_claim])

    assert ws_db.get_claim(pricing_claim)["issue_id"] == result["issue_id"]
    assert ws_db.get_claim(onboarding_claim)["issue_id"] == cid


def test_extract_issue_from_project_rejects_unknown_project(ws_db):
    with pytest.raises(ValueError):
        wp.extract_issue_from_project("proj-does-not-exist", title="X", claim_ids=[1])


def test_extract_issue_from_project_rejects_claim_not_belonging_to_project(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    other_cluster = _cluster(ws_db, "Unrelated cluster")
    stray_claim = _claim_on(ws_db, other_cluster, "unrelated ask")

    with pytest.raises(ValueError):
        wp.extract_issue_from_project(pid, title="X", claim_ids=[stray_claim])


def test_extract_issue_from_project_rejects_unknown_claim_id(ws_db):
    pid = ws_db.create_project_with_new_id(name="P", category="other")
    with pytest.raises(ValueError):
        wp.extract_issue_from_project(pid, title="X", claim_ids=[999999])


# --- task #367: compose-mode subject matching ------------------------------

def test_find_project_ids_by_subject_fragment_matches_a_real_open_project(ws_db):
    ws_db.create_project_with_new_id(name="Veeva CRM press release renewal", category="other")

    result = wp.find_project_ids_by_subject_fragment("RE: Veeva CRM press release renewal - final terms")

    assert len(result) == 1


def test_find_project_ids_by_subject_fragment_returns_empty_for_no_match(ws_db):
    ws_db.create_project_with_new_id(name="Veeva CRM press release renewal", category="other")

    assert wp.find_project_ids_by_subject_fragment("Totally unrelated lunch plans") == []


def test_find_project_ids_by_subject_fragment_returns_empty_for_short_subject(ws_db):
    ws_db.create_project_with_new_id(name="Veeva CRM press release renewal", category="other")

    assert wp.find_project_ids_by_subject_fragment("hi") == []


def test_find_project_ids_by_subject_fragment_excludes_closed_projects(ws_db):
    pid = ws_db.create_project_with_new_id(name="Veeva CRM press release renewal", category="other")
    ws_db.set_project_status(pid, "archived")

    assert wp.find_project_ids_by_subject_fragment("Veeva CRM press release renewal - final terms") == []
