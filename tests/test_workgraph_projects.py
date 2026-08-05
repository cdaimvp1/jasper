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

import workgraph_lessons
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


def _give_real_context(ws_db, issue_id, category="rfp-sourcing"):
    """Confidence spine v1 (2026-08-03): scored_grouping_decision's verdict
    is now damped by real context_accuracy, not just the raw pairwise
    score - a 2-signal match with NO category, NO evidence, and NO
    reference (the bare _issue() default) genuinely deserves a lower
    effective score than the same match backed by real context. Tests
    whose actual point is "does a 2-signal combination auto-merge" need
    that real context to isolate what they're testing, same as production
    issues (which have a real category and real evidence) almost always
    do - a bare-fixture issue is the unrealistic case, not the norm."""
    ws_db.update_issue(issue_id, category=category)
    ws_db.add_evidence(issue_id=issue_id, type="email", summary="real evidence for this issue")


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


def test_veto_false_when_references_are_different_versions_of_the_same_requisition(ws_db):
    """The real bug (2026-07-31 fix): PR1140347-V2 and PR1140347-V3 both
    exist in production today as the SAME real requisition at different
    approval-cycle versions - the old exact-string match treated this as
    ACTIVELY CONTRADICTING evidence (worse than no reference at all), which
    could veto an otherwise-valid party/company/topic match."""
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR1140347-V2 approval needed", "v9")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, b, "PR1140347-V3 approval needed", "v10")
    assert wp._vetoed_by_reference_mismatch(a, b) is False


def test_shared_reference_id_matches_across_versions(ws_db):
    """Positive counterpart of the fix above - a version bump on the SAME
    requisition must now find its sibling, not just fail to veto it."""
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR1140347-V2 approval needed", "v11")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, b, "PR1140347-V3 approval needed", "v12")
    result = wp._shared_reference_id(a)
    assert result == ("PR1140347", b)


def test_reference_base_ids_for_issue_strips_version(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR416079-V33 approval needed", "v13")
    assert wp.reference_base_ids_for_issue(a) == {"PR416079"}
    # display set is untouched - still the full versioned string
    assert wp.reference_ids_for_issue(a) == {"PR416079-V33"}


# --- Part A1 (2026-07-30): matching reference ID as a positive signal ----

def test_shared_reference_id_finds_sibling_with_no_other_signal_shared(ws_db):
    """Real production case this closes: 3 separately-sent automated
    reminders about the same requisition, different sender each time, no
    Outlook reply-chain - share nothing except the reference number."""
    a = _issue(ws_db, "First notice")
    _raw_item(ws_db, a, "PR854779-V4 approval needed", "r1", from_actor="alice@example.com")
    b = _issue(ws_db, "Reminder")
    _raw_item(ws_db, b, "REMINDER: PR854779-V4 still needs approval", "r2", from_actor="bob@example.com")

    result = wp._shared_reference_id(a)

    # 2026-07-31: matches/returns the version-stripped BASE now (see
    # reference_base_ids_for_issue) - both raw_items happen to share the
    # exact same version here, but the function no longer relies on that;
    # a genuinely different version on each side (the whole point of the
    # fix) would still return this same base.
    assert result == ("PR854779", b)


def test_shared_reference_id_none_when_no_reference(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "nothing structured here", "r3")
    assert wp._shared_reference_id(a) is None


def test_shared_reference_id_none_when_reference_unique_to_this_issue(ws_db):
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR1111865 approval needed", "r4")
    assert wp._shared_reference_id(a) is None


def test_shared_reference_id_ignores_closed_sibling_issues(ws_db):
    a = _issue(ws_db, "Open one")
    _raw_item(ws_db, a, "PR777777 approval needed", "r5")
    b = ws_db.create_issue_with_new_id(title="Closed one", state="done", category="other")
    _raw_item(ws_db, b, "PR777777 approval needed", "r6")
    assert wp._shared_reference_id(a) is None


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


def test_strong_signal_match_prefers_reference_over_party(ws_db):
    """Reference ID is checked FIRST - even when a shared external party
    would also match, the reference-id result (and its label) wins."""
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR654321 approval needed", "r7")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, b, "PR654321 approval needed again", "r8")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")

    kind, detail, sibling_id, verdict = wp._strong_signal_match(a, ws_db.get_issue(a))

    assert kind == "reference"
    assert detail == "PR654321"
    assert sibling_id == b
    assert verdict == "merge"


def test_group_issue_merges_via_reference_id_alone(ws_db):
    """End-to-end: two issues sharing only a reference id (no party/
    company/topic overlap) actually auto-merge via group_issue()."""
    a = _issue(ws_db, "First notice about a totally different subject line")
    _raw_item(ws_db, a, "PR991122 approval needed", "r9", from_actor="alice@example.com")
    b = _issue(ws_db, "A completely unrelated-looking reminder subject")
    _raw_item(ws_db, b, "REMINDER on PR991122", "r10", from_actor="bob@example.com")

    result = wp.group_issue(a)

    assert result["action"] == "auto_merged"
    assert result["signal"] == "reference"
    assert ws_db.get_issue(a)["project_id"] == ws_db.get_issue(b)["project_id"]


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


def test_shared_real_company_without_reference_numbers_suggests_not_merges(ws_db):
    """No PR/PO present on either side, so the reference veto never applies -
    but shared company alone is narrowed (2026-07-31) to suggest-only, never
    auto-merge on its own: an exact company match only proves the same
    company is involved, not the same transaction."""
    a = _issue(ws_db, "Deal A")
    _raw_item(ws_db, a, "Let's discuss the renewal", "c3")
    _link_party(ws_db, a, "rep1", "rep1@acme.com", company="Acme")

    b = _issue(ws_db, "Deal B")
    _raw_item(ws_db, b, "Following up on the renewal", "c4")
    _link_party(ws_db, b, "rep2", "rep2@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "suggested"
    assert result["count"] == 1


# --- related-vs-same-project verdict (2026-07-31) -------------------------

def test_topic_keys_match_true_for_overlapping_titles(ws_db):
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    assert wp._topic_keys_match(a, b) is True


def test_topic_keys_match_false_for_unrelated_titles(ws_db):
    """The real marc-166/marc-063 shape: same counterparty, different
    transaction - titles don't meaningfully overlap."""
    a = _issue(ws_db, "Dragonfly 2.0 SOW's")
    b = _issue(ws_db, "H1/Lilly SOW Review")
    assert wp._topic_keys_match(a, b) is False


def test_topic_keys_match_false_when_titles_too_short(ws_db):
    a = _issue(ws_db, "Hi there")
    b = _issue(ws_db, "Hi there")
    assert wp._topic_keys_match(a, b) is False


def test_strong_signal_match_party_with_topic_overlap_verdicts_merge(ws_db):
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")

    kind, detail, sibling_id, verdict = wp._strong_signal_match(a, ws_db.get_issue(a))

    assert kind == "party"
    assert verdict == "merge"


def test_strong_signal_match_party_without_topic_overlap_verdicts_link(ws_db):
    """The real marc-166/marc-063 case: same external party (H1), causally
    connected (H1 helping exit one contract enables a new deal), but
    genuinely different transactions - must NOT be treated as merge-
    eligible."""
    a = _issue(ws_db, "Dragonfly 2.0 SOW's")
    _link_party(ws_db, a, "h1_contact", "rep@h1.com", company="H1")
    b = _issue(ws_db, "H1/Lilly SOW Review")
    _link_party(ws_db, b, "h1_contact", "rep@h1.com", company="H1")

    kind, detail, sibling_id, verdict = wp._strong_signal_match(a, ws_db.get_issue(a))

    assert kind == "party"
    assert verdict == "link"


def test_strong_signal_match_company_only_always_verdicts_link(ws_db):
    """Different people at the same company - even with matching topic
    text, a bare company match never proves the same transaction (the
    two-distinct-PwC-meeting-series real shape)."""
    a = _issue(ws_db, "PwC drop-in hours")
    _link_party(ws_db, a, "pwc_rep1", "rep1@pwc.com", company="PwC")
    b = _issue(ws_db, "PwC drop-in hours weekly session")
    _link_party(ws_db, b, "pwc_rep2", "rep2@pwc.com", company="PwC")

    kind, detail, sibling_id, verdict = wp._strong_signal_match(a, ws_db.get_issue(a))

    assert kind == "company"
    assert verdict == "link"


def test_group_issue_party_without_topic_overlap_creates_link_suggestion(ws_db):
    """End-to-end: group_issue() on the marc-166/marc-063 shape creates a
    'link' suggestion, not a 'merge' suggestion - and never auto-merges."""
    a = _issue(ws_db, "Dragonfly 2.0 SOW's")
    _link_party(ws_db, a, "h1_contact", "rep@h1.com", company="H1")
    b = _issue(ws_db, "H1/Lilly SOW Review")
    _link_party(ws_db, b, "h1_contact", "rep@h1.com", company="H1")

    result = wp.group_issue(a)

    assert result["action"] == "suggested"
    assert result["suggestion_kind"] == "link"
    suggestions = ws_db.list_project_suggestions(status="pending")
    assert len(suggestions) == 1
    assert suggestions[0]["suggestion_kind"] == "link"


def test_group_issue_party_with_topic_overlap_creates_merge_suggestion(ws_db):
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "suggested"
    assert "suggestion_kind" not in result  # unchanged default path, no explicit kind
    suggestions = ws_db.list_project_suggestions(status="pending")
    assert suggestions[0]["suggestion_kind"] == "merge"


def test_confirm_suggestion_merge_kind_merges_the_issues(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="merge")

    result = wp.confirm_suggestion(sid)

    assert result["action"] == "merged"
    assert ws_db.get_issue(a)["project_id"] == result["project_id"]
    assert ws_db.get_issue(b)["project_id"] == result["project_id"]
    assert ws_db.get_project_suggestion(sid)["status"] == "confirmed"


def test_confirm_suggestion_link_kind_creates_project_link_not_a_merge(ws_db):
    """The real point of this verdict tier: confirming must NOT merge the
    two issues into one project - it creates a link between whichever
    projects they end up in, standalone if neither had one yet."""
    a = _issue(ws_db, "Dragonfly 2.0 SOW's")
    b = _issue(ws_db, "H1/Lilly SOW Review")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="possibly related", suggestion_kind="link")

    result = wp.confirm_suggestion(sid)

    assert result["action"] == "linked"
    assert result["link_type"] == "related"
    a_project = ws_db.get_issue(a)["project_id"]
    b_project = ws_db.get_issue(b)["project_id"]
    assert a_project is not None and b_project is not None
    assert a_project != b_project  # NOT merged into the same project
    links = ws_db.list_project_links_for_project(a_project)
    assert len(links) == 1
    assert links[0]["link_type"] == "related"


def test_confirm_suggestion_link_kind_respects_upgraded_link_type(ws_db):
    a = _issue(ws_db, "Dragonfly 2.0 SOW's")
    b = _issue(ws_db, "H1/Lilly SOW Review")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="H1 helping exit Dragonfly", suggestion_kind="link")

    result = wp.confirm_suggestion(sid, link_type="enables")

    assert result["link_type"] == "enables"


def test_confirm_suggestion_link_kind_reuses_existing_projects(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="Existing A", category="other")
    a = _issue(ws_db, "A")
    ws_db.assign_issue_to_project(a, proj_a)
    b = _issue(ws_db, "B")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="link")

    result = wp.confirm_suggestion(sid)

    assert result["from_project_id"] == proj_a  # a's EXISTING project reused, not a new one
    assert ws_db.get_issue(b)["project_id"] == result["to_project_id"]


def test_confirm_suggestion_merge_kind_marks_both_issues_confirmed(ws_db):
    """Section 12.8: a real human/curator confirm event - unlike the raw
    auto-merge threshold path, which leaves membership_state at its
    'provisional' default - marks BOTH sides confirmed, not just the
    winner."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="merge")

    wp.confirm_suggestion(sid)

    assert ws_db.get_work_object_membership_exposure(a)["membership_state"] == "confirmed"
    assert ws_db.get_work_object_membership_exposure(b)["membership_state"] == "confirmed"


def test_reject_suggestion_link_kind_does_not_record_a_lesson(ws_db):
    """A link-suggestion rejection must NOT feed Total Recall's merge
    precedent bucket - that's a different question (same project or not)
    than the one a link candidate is actually asking."""
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET category = 'contract' WHERE id = ?", (a,))
    conn.close()
    b = _issue(ws_db, "B")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="link")

    wp.reject_suggestion(sid)

    key = workgraph_lessons.situation_key_for_issue(ws_db.get_issue(a))
    assert key is not None
    assert ws_db.get_lesson_by_situation(key, "rejected") is None


def test_reject_suggestion_merge_kind_writes_cannot_merge_constraint(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="merge")

    wp.reject_suggestion(sid)

    constraint = ws_db.find_identity_constraint("cannot_merge", a, b)
    assert constraint is not None
    assert constraint["created_by"] == "marc"


def test_reject_suggestion_link_kind_writes_cannot_link_constraint(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="link")

    wp.reject_suggestion(sid)

    assert ws_db.find_identity_constraint("cannot_link", a, b) is not None
    assert ws_db.find_identity_constraint("cannot_merge", a, b) is None  # wrong kind, not written


def test_rejected_pair_cannot_resurface_a_new_suggestion(ws_db):
    """The real bug this closes: today a rejected suggestion just expires
    and the same pair can resurface a brand-new one from fresh evidence -
    once rejected, create_project_suggestion for the SAME pair/kind must
    come back None forever, not a new pending row."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="merge")
    wp.reject_suggestion(sid)

    blocked = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="fresh evidence", suggestion_kind="merge")
    assert blocked is None
    # order-independence: (b, a) is the same pair
    blocked2 = ws_db.create_project_suggestion(issue_id_a=b, issue_id_b=a, reason="fresh evidence", suggestion_kind="merge")
    assert blocked2 is None


def test_rejected_merge_does_not_block_a_link_suggestion_for_the_same_pair(ws_db):
    """cannot_merge and cannot_link are different questions about the same
    pair - rejecting one must not silently veto the other."""
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="merge")
    wp.reject_suggestion(sid)

    link_sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="link")
    assert link_sid is not None


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
    auto-collapse an established project and defers to a 'merge_projects'
    suggestion instead."""
    proj_a = ws_db.create_project_with_new_id(name="Project A", category="other")
    a = _issue(ws_db, "A")
    ws_db.assign_issue_to_project(a, proj_a)

    proj_b = ws_db.create_project_with_new_id(name="Project B", category="other")
    b = _issue(ws_db, "B")
    ws_db.assign_issue_to_project(b, proj_b)
    other_member_of_b = _issue(ws_db, "Other member of B's project")
    ws_db.assign_issue_to_project(other_member_of_b, proj_b)

    result = wp.merge_issues(a, b, reason_label="test collision")

    assert result["status"] == "deferred"
    assert result["winner_project_id"] == proj_a
    assert result["loser_project_id"] == proj_b
    # NOTHING actually merged - real projects must not be silently collapsed.
    assert ws_db.get_issue(a)["project_id"] == proj_a
    assert ws_db.get_issue(b)["project_id"] == proj_b
    assert ws_db.get_issue(other_member_of_b)["project_id"] == proj_b
    assert ws_db.get_project(proj_b)["status"] != "archived"
    sugg = ws_db.get_project_suggestion(result["suggestion_id"])
    assert sugg["suggestion_kind"] == "merge_projects"


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


# --- backfill_regroup_by_reference (Part A1) ------------------------------

def test_backfill_regroup_by_reference_merges_the_real_split_pattern(ws_db):
    """The real production case this fixes: 3 separate issues sharing only
    a reference ID (different sender each time, no other shared signal)."""
    a = _issue(ws_db, "First notice")
    _raw_item(ws_db, a, "PR854779-V4 approval needed", "bf1", from_actor="alice@example.com")
    b = _issue(ws_db, "Second notice, unrelated-looking subject")
    _raw_item(ws_db, b, "REMINDER: PR854779-V4 still needs approval", "bf2", from_actor="bob@example.com")
    c = _issue(ws_db, "Third notice, also unrelated-looking")
    _raw_item(ws_db, c, "please approve PR854779-V4 today", "bf3", from_actor="carol@example.com")

    result = wp.backfill_regroup_by_reference()

    assert result["checked"] == 3
    a_proj = ws_db.get_issue(a)["project_id"]
    assert a_proj is not None
    assert ws_db.get_issue(b)["project_id"] == a_proj
    assert ws_db.get_issue(c)["project_id"] == a_proj


def test_backfill_regroup_by_reference_skips_issues_with_no_reference(ws_db):
    _issue(ws_db, "No reference at all")
    result = wp.backfill_regroup_by_reference()
    assert result["checked"] == 0


def test_backfill_regroup_by_reference_is_safe_to_rerun(ws_db):
    a = _issue(ws_db, "First")
    _raw_item(ws_db, a, "PR333444 approval needed", "bf4")
    b = _issue(ws_db, "Second")
    _raw_item(ws_db, b, "PR333444 approval needed again", "bf5")

    first = wp.backfill_regroup_by_reference()
    second = wp.backfill_regroup_by_reference()

    assert first["auto_merged"] == 1
    assert second["auto_merged"] == 0
    assert second["already_grouped"] == 2


# --- find_relationship_links_for_grouped_issues (2026-08-01) -------------
# Real gap: group_issue()'s "if issue.get('project_id'): return
# already_grouped" means an already-grouped issue is never re-checked for a
# genuine cross-project RELATIONSHIP (the "link" verdict) - confirmed live
# on a real Workday deal spanning 2 different projects sharing one company.

def test_find_relationship_links_suggests_shared_company_across_projects(ws_db):
    a = _issue(ws_db, "Workday Early Renewal Order Form")
    _link_party(ws_db, a, "dan-workday", "dan@workday.com", company="Workday")
    ws_db.assign_issue_to_project(a, "proj-a")

    b = _issue(ws_db, "Workday HCM SaaS renewal")
    _link_party(ws_db, b, "someone-else-workday", "someone@workday.com", company="Workday")
    ws_db.assign_issue_to_project(b, "proj-b")

    result = wp.find_relationship_links_for_grouped_issues()

    assert result["checked"] == 2
    assert result["suggested"] >= 1
    suggestions = ws_db.list_project_suggestions(status="pending")
    matching = [s for s in suggestions if s["suggestion_kind"] == "link"
                and {s["issue_id_a"], s["issue_id_b"]} == {a, b}]
    assert len(matching) == 1


def test_find_relationship_links_skips_pair_already_in_same_project(ws_db):
    a = _issue(ws_db, "First")
    _link_party(ws_db, a, "dan-workday2", "dan2@workday.com", company="Workday")
    ws_db.assign_issue_to_project(a, "proj-same")

    b = _issue(ws_db, "Second")
    _link_party(ws_db, b, "dan-workday2", "dan2@workday.com", company="Workday")
    ws_db.assign_issue_to_project(b, "proj-same")

    result = wp.find_relationship_links_for_grouped_issues()

    assert result["suggested"] == 0


def test_find_relationship_links_vetoed_by_disjoint_reference(ws_db):
    """Same real-transaction-vs-different-transaction distinction
    _strong_signal_match itself already enforces - two different purchase
    requisitions with the same vendor contact are NOT the same relationship
    suggestion just because the company matches."""
    a = _issue(ws_db, "First requisition")
    _raw_item(ws_db, a, "Approve PR100001", "rl1", from_actor="dan3@workday.com")
    _link_party(ws_db, a, "dan-workday3", "dan3@workday.com", company="Workday")
    ws_db.assign_issue_to_project(a, "proj-c")

    b = _issue(ws_db, "Second, different requisition")
    _raw_item(ws_db, b, "Approve PR999999", "rl2", from_actor="dan3@workday.com")
    _link_party(ws_db, b, "dan-workday3", "dan3@workday.com", company="Workday")
    ws_db.assign_issue_to_project(b, "proj-d")

    result = wp.find_relationship_links_for_grouped_issues()

    assert result["suggested"] == 0


def test_find_relationship_links_ungrouped_sibling_still_suggested(ws_db):
    """Only the issue BEING checked needs a project already - its sibling
    doesn't (the real Workday case: marc-014 had no project of its own
    at all)."""
    a = _issue(ws_db, "Grouped issue")
    _link_party(ws_db, a, "dan-workday4", "dan4@workday.com", company="Workday")
    ws_db.assign_issue_to_project(a, "proj-e")

    b = _issue(ws_db, "Ungrouped sibling")
    _link_party(ws_db, b, "dan-workday4", "dan4@workday.com", company="Workday")

    result = wp.find_relationship_links_for_grouped_issues()

    assert result["suggested"] == 1
    assert ws_db.get_issue(b)["project_id"] is None, "must suggest, never assign, a project"


def test_find_relationship_links_is_idempotent(ws_db):
    a = _issue(ws_db, "First")
    _link_party(ws_db, a, "dan-workday5", "dan5@workday.com", company="Workday")
    ws_db.assign_issue_to_project(a, "proj-f")
    b = _issue(ws_db, "Second")
    _link_party(ws_db, b, "dan-workday5", "dan5@workday.com", company="Workday")
    ws_db.assign_issue_to_project(b, "proj-g")

    first = wp.find_relationship_links_for_grouped_issues()
    second = wp.find_relationship_links_for_grouped_issues()

    assert first["suggested"] >= 1
    assert second["suggested"] == 0
    suggestions = [s for s in ws_db.list_project_suggestions(status="pending")
                   if {s["issue_id_a"], s["issue_id_b"]} == {a, b}]
    assert len(suggestions) == 1


def test_find_relationship_links_tries_every_party_not_just_the_first(ws_db):
    """Real bug found same-day, live: an issue with several external
    parties whose FIRST party's first sibling already shares its project
    must still try its OTHER parties, not give up - the exact real case
    that exposed this (marc-325/marc-280, both real Bodman/Hubert outside
    counsel on the same uMSA-amendment negotiation) reproduced here with
    synthetic data of the identical shape."""
    same_project_sibling = _issue(ws_db, "Already in the same project")
    _link_party(ws_db, same_project_sibling, "dan-x", "dan@workday.com", company="Workday")

    anchor = _issue(ws_db, "Anchor issue, several external parties")
    _link_party(ws_db, anchor, "dan-x", "dan@workday.com", company="Workday")  # same party as above
    _link_party(ws_db, anchor, "counsel-x", "counsel@lawfirm.com", company="Lawfirm")
    ws_db.assign_issue_to_project(same_project_sibling, "proj-shared")
    ws_db.assign_issue_to_project(anchor, "proj-shared")

    genuinely_new_sibling = _issue(ws_db, "Different project, shares counsel")
    _link_party(ws_db, genuinely_new_sibling, "counsel-x", "counsel@lawfirm.com", company="Lawfirm")
    ws_db.assign_issue_to_project(genuinely_new_sibling, "proj-other")

    result = wp.find_relationship_links_for_grouped_issues()

    assert result["suggested"] >= 1
    suggestions = ws_db.list_project_suggestions(status="pending")
    matching = [s for s in suggestions if {s["issue_id_a"], s["issue_id_b"]} == {anchor, genuinely_new_sibling}]
    assert len(matching) == 1, "must reach the SECOND party (counsel) after the first party's match turned out to be same-project"


# --- Part A2 (2026-07-30): weighted multi-signal scoring model -----------

def _isolate_config(monkeypatch, tmp_path):
    """Same isolation pattern as test_retention.py's retention_env fixture -
    config.SETTINGS_PATH is bound at import time, not per-test."""
    import config
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_cache", {})
    monkeypatch.setattr(config, "_cache_mtime", 0.0)
    return config


def _score_pair(a, b):
    """Section 12.7 helper: builds both sides' cached signatures + topic
    keys and scores them, the same sequence scored_grouping_decision/
    backtest_scored_model now use - retired _pairwise_score/
    _issue_signal_snapshot (Section 9's flat model) called for this
    directly; keeping ONE real scoring path is the whole point of the
    signature model (see compute_work_object_signature's docstring)."""
    issue_a, issue_b = wp.ws.get_issue(a), wp.ws.get_issue(b)
    sig_a = wp.get_or_compute_work_object_signature(a, issue_a)
    sig_b = wp.get_or_compute_work_object_signature(b, issue_b)
    topic_a = wp._topic_key_for_signature(issue_a, sig_a)
    topic_b = wp._topic_key_for_signature(issue_b, sig_b)
    return wp._pairwise_score_from_signature(
        a, sig_a, topic_a, issue_a.get("category"), b, sig_b, topic_b, issue_b.get("category"),
    )


def test_pairwise_score_single_signal_never_reaches_threshold(ws_db):
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B, unrelated subject entirely")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    score, signals = _score_pair(a, b)

    assert signals == ["company"]
    assert score == wp.SCORE_WEIGHTS["company"]
    assert score < wp.AUTO_MERGE_THRESHOLD


def test_pairwise_score_company_and_topic_combine_above_threshold(ws_db):
    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    score, signals = _score_pair(a, b)

    assert set(signals) == {"company", "topic"}
    assert score == wp.SCORE_WEIGHTS["company"] + wp.SCORE_WEIGHTS["topic"]
    assert score >= wp.AUTO_MERGE_THRESHOLD


def test_pairwise_score_category_other_never_contributes(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    score, signals = _score_pair(a, b)
    assert "category" not in signals
    assert score == 0.0


def test_pairwise_score_disjoint_reference_vetoes_everything(ws_db):
    """Even a strong combined score must be zeroed by a disjoint reference
    ID - the absolute override carries over unchanged from the ordered
    model."""
    a = _issue(ws_db, "Action required: Approve the Requisition that X submitted")
    _raw_item(ws_db, a, "PR111111", "pa1")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that X submitted")
    _raw_item(ws_db, b, "PR222222", "pa2")
    _link_party(ws_db, b, "p2", "rep2@acme.com", company="Acme")

    score, signals = _score_pair(a, b)

    assert score == 0.0
    assert signals == []


def test_pairwise_score_cannot_merge_constraint_vetoes_everything(ws_db):
    """Section 12.7's real new veto: a durable cannot_merge (v2.4) must
    zero the score outright, same absolute-override treatment as a
    disjoint reference ID - closing the real gap where scored_grouping_
    decision's auto_merge path bypassed create_project_suggestion (and
    therefore v2.4's own check) by calling merge_issues_txn directly."""
    a = _issue(ws_db, "Action required: Approve the Requisition that X submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that X submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")
    ws_db.create_identity_constraint("cannot_merge", a, b, "confirmed separate", actor="marc")

    score, signals = _score_pair(a, b)

    assert score == 0.0
    assert signals == []


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


def test_reject_suggestion_invalidates_cached_signatures_for_both_sides(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    wp.get_or_compute_work_object_signature(a, ws_db.get_issue(a))
    wp.get_or_compute_work_object_signature(b, ws_db.get_issue(b))
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="merge")

    wp.reject_suggestion(sid)

    assert ws_db.get_work_object_signature(a) is None
    assert ws_db.get_work_object_signature(b) is None


def test_merge_issue_into_invalidates_both_cached_signatures(ws_db):
    winner = _issue(ws_db, "Winner")
    loser = _issue(ws_db, "Loser")
    wp.get_or_compute_work_object_signature(winner, ws_db.get_issue(winner))
    wp.get_or_compute_work_object_signature(loser, ws_db.get_issue(loser))

    ws_db.merge_issue_into(loser, winner, reason="test", actor="marc")

    assert ws_db.get_work_object_signature(winner) is None
    assert ws_db.get_work_object_signature(loser) is None


def test_scored_grouping_decision_reference_match_is_auto_merge(ws_db):
    a = _issue(ws_db, "First notice")
    _raw_item(ws_db, a, "PR333222 approval needed", "sg1")
    b = _issue(ws_db, "Totally different subject")
    _raw_item(ws_db, b, "REMINDER PR333222", "sg2")

    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert decision["verdict"] == "auto_merge"
    assert decision["score"] == 1.0
    assert decision["sibling_id"] == b
    assert decision["matched_signals"] == ["reference"]


def test_scored_grouping_decision_shared_party_alone_is_suggest_not_auto_merge(ws_db):
    """Phase 0 fix (D4): a standalone _shared_external_party -> auto_merge(1.0)
    branch used to live in scored_grouping_decision - the exact hazard the
    live grouping path (_strong_signal_match) was already narrowed off of on
    2026-07-31 (see this module's docstring). A shared party alone must now
    only ever SUGGEST, never auto-merge, on otherwise unrelated topics."""
    # company left unset on both sides so ONLY the party signal fires -
    # otherwise a shared party who also shares a company would double up
    # both signals and auto-merge for a different reason than this test
    # means to isolate.
    a = _issue(ws_db, "Renewal negotiation")
    _link_party(ws_db, a, "p_shared", "rep@acme.com")
    b = _issue(ws_db, "Completely unrelated support escalation")
    _link_party(ws_db, b, "p_shared", "rep@acme.com")

    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert decision["verdict"] == "suggest"
    assert decision["sibling_id"] == b
    assert decision["matched_signals"] == ["party"]


def test_scored_grouping_decision_party_plus_topic_raw_score_composition_merge(ws_db):
    """Marc's detailed correction (2026-08-04) to the plain count-based rule
    this replaces: signals are weighted strong/medium/weak, not counted
    flatly - party+topic is 2 MEDIUM anchors ("named counterparty" +
    "specific product/service" in his own framing), which clears his
    stated bar ("2 medium anchors") - so the composition classification is
    "merge". party+SENDER (a medium + a weak anchor) does NOT clear it on
    its own - see test_suggestion_kind_for_scored_signals_weak_anchors_
    need_two below.

    The verdict itself still comes back "suggest" here, NOT "auto_merge" -
    a SEPARATE, still-open issue found while fixing this: effective_score
    = raw_score * context_accuracy (workgraph_confidence.py), and
    context_accuracy's referential_resolution component is 0.0 whenever
    this issue has no captured PR/PO reference of its own (unrelated to
    whether party+topic actually match), which single-handedly caps
    effective_score below 0.65 for almost any non-reference-based match,
    composition notwithstanding. That's flagged back to Marc as its own
    question, not silently patched here - this test only locks in the
    composition half of the fix, which is settled."""
    a = _issue(ws_db, "Workday HCM SaaS renewal negotiation")
    _link_party(ws_db, a, "p_shared", "rep@acme.com")

    b = _issue(ws_db, "Workday HCM SaaS renewal negotiation follow-up")
    _link_party(ws_db, b, "p_shared", "rep@acme.com")

    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert decision["score"] == 0.9
    assert set(decision["matched_signals"]) == {"party", "topic"}
    assert wp._suggestion_kind_for_scored_signals(decision["matched_signals"]) == "merge"
    assert decision["verdict"] == "suggest"


def test_scored_grouping_decision_two_signals_without_a_real_anchor_only_suggests(ws_db):
    """Confidence spine v1 (2026-08-03): a 2-signal heuristic match
    (party+topic, raw 0.90 - bumped from 0.80 task #169/#170, 2026-08-04,
    party/topic weights 0.40->0.45 - isolated from company by leaving it
    unset on both sides) now correctly stays at "suggest," never
    "auto_merge" -
    without ANY real structural anchor (a reference), referential_
    resolution is 0 on this pair regardless of how rich the surrounding
    context is, capping effective_score below AUTO_MERGE_THRESHOLD. This
    is the intended, more conservative behavior: docs/design/CONFIDENCE_
    AND_IDENTITY_REDESIGN.md Section 3.3's own bucket rules reserve
    Automatic for real anchors - two heuristic signals alone were never
    supposed to be enough, the original scored model just hadn't been
    checked against that rule until this backtest did."""
    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p_shared2", "rep@acme.com")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p_shared2", "rep@acme.com")

    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert decision["verdict"] == "suggest"
    assert decision["score"] == 0.9  # the raw score is still high...
    assert decision["effective_score"] < wp.AUTO_MERGE_THRESHOLD  # ...but the damped one decides
    assert "party" in decision["matched_signals"]


def test_scored_grouping_decision_real_context_raises_effective_score_even_when_it_cant_cross_alone(ws_db):
    """_give_real_context (category + evidence) measurably raises
    effective_score over the identical thin-context match - real context
    is worth something, it just isn't a substitute for a real anchor when
    deciding Automatic vs One-touch."""
    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com")
    _give_real_context(ws_db, a)
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com")
    thin = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, thin, "p3", "third@acme.com")
    thin_b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, thin_b, "p4", "fourth@acme.com")

    rich_decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))
    thin_decision = wp.scored_grouping_decision(thin, ws_db.get_issue(thin))

    assert rich_decision["verdict"] == thin_decision["verdict"] == "suggest"
    assert rich_decision["effective_score"] > thin_decision["effective_score"]


def test_scored_grouping_decision_combined_weak_signals_still_only_suggests_without_anchor(ws_db):
    """company+topic (raw 0.80, no party/reference at all) - same shape,
    same reasoning as the party+topic test above."""
    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert decision["verdict"] == "suggest"
    assert decision["sibling_id"] == b
    assert set(decision["matched_signals"]) == {"company", "topic"}


def test_scored_grouping_decision_single_weak_signal_is_suggest_not_merge(ws_db):
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B, unrelated subject entirely")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert decision["verdict"] == "suggest"
    assert decision["sibling_id"] == b


def test_scored_grouping_decision_attaches_confidence_spine_fields(ws_db):
    """Confidence spine v1 (2026-08-03): context_accuracy/effective_score
    are real now (they decide the verdict, not just observational fields -
    see the "still only suggests without anchor" tests above for a case
    where damping actually changes the bucket). A single weak signal
    (party alone, raw 0.40) stays "suggest" here regardless - it was
    already below AUTO_MERGE_THRESHOLD undamped, so this case doesn't by
    itself prove damping is active; it just confirms the fields are
    computed and internally consistent."""
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B, unrelated subject entirely")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert 0.0 <= decision["context_accuracy"] <= 1.0
    assert decision["effective_score"] == round(decision["score"] * decision["context_accuracy"], 6)
    assert decision["verdict"] == "suggest"


def test_scored_grouping_decision_uses_real_anchors_when_backfilled(ws_db):
    """Confidence spine v1: once identity_anchors exist for the issue,
    context_accuracy is computed from them, not the match_kind shim."""
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B, unrelated subject entirely")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    without_anchors = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    ws_db.create_identity_anchor(anchor_type="party", normalized_value="p1", anchor_strength="exact",
                                  exclusive=False, issue_id=a)
    with_anchors = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert with_anchors["context_accuracy"] > without_anchors["context_accuracy"]
    assert with_anchors["verdict"] == without_anchors["verdict"] == "suggest"


def test_scored_grouping_decision_nothing_shared_is_no_match(ws_db):
    a = _issue(ws_db, "A")
    _issue(ws_db, "Completely unrelated")
    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))
    assert decision["verdict"] == "no_match"
    assert decision["sibling_id"] is None


def test_group_issue_shadow_scored_always_attached_regardless_of_flag(ws_db):
    a = _issue(ws_db, "First notice")
    _raw_item(ws_db, a, "PR444555 approval needed", "sg3")
    b = _issue(ws_db, "Totally different subject")
    _raw_item(ws_db, b, "REMINDER PR444555", "sg4")

    result = wp.group_issue(a)

    assert "shadow_scored" in result
    assert result["shadow_scored"]["verdict"] == "auto_merge"


def test_group_issue_flag_off_uses_ordered_model_not_scored_model(ws_db, monkeypatch, tmp_path):
    """Real behavior check: with the flag off, group_issue's own ORDERED
    model (_strong_signal_match) resolves this shared-company pair on its
    own terms ("company", a link suggestion) - never touching the scored
    model's verdict, even though shadow_scored is still computed and
    logged for comparison. Confidence spine v1 note: post-damping, the
    scored model's OWN verdict for this same pair is also "suggest" now
    (no real anchor - see test_scored_grouping_decision_combined_weak_
    signals_still_only_suggests_without_anchor), so the two models agree
    on the outcome here; what this test actually confirms is that the flag
    gates which model's REASONING drove it - "company" (ordered), not
    "scored" - not a wider outcome gap that no longer exists post-damping."""
    config = _isolate_config(monkeypatch, tmp_path)
    assert config.get("grouping", "scored_model_enabled") in (None, False)

    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "suggested"
    assert result["signal"] == "company"
    assert result["shadow_scored"]["verdict"] == "suggest"  # still computed/logged either way


def test_group_issue_flag_on_uses_scored_model(ws_db, monkeypatch, tmp_path):
    """Same pair as above, flag ON: the SCORED model's own path handles
    it (signal="scored") instead of the ordered model's "company" - the
    real thing the flag gates. Post-damping, both land on "suggested" for
    this no-anchor pair (see the module-level note above)."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "suggested"
    assert result["signal"] == "scored"


def test_group_issue_flag_on_single_weak_signal_creates_suggestion_not_merge(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B, unrelated subject entirely")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "suggested"
    assert ws_db.get_issue(a)["project_id"] is None


# --- 2026-08-04 (Marc's second, more detailed correction, superseding the ---
# --- plain count-based rule this replaces): "every signal should not count --
# --- equally... one extremely strong identifier may be enough... two weak --
# --- matches... should not generate a serious candidate." Tiered anchors: --
# --- strong (any 1 alone), medium (need 2, or 1 + 2 weak), weak (support ---
# --- only, never alone or in pairs of just weak). ---------------------

def test_suggestion_kind_for_scored_signals_one_strong_anchor_is_enough():
    assert wp._suggestion_kind_for_scored_signals(["attachment"]) == "merge"


def test_suggestion_kind_for_scored_signals_two_medium_anchors_merge():
    """His own examples of medium anchors: supplier (company), named
    counterparty (party), specific product/service (ariba_descriptor),
    specific dollar amount (amount)."""
    assert wp._suggestion_kind_for_scored_signals(["party", "topic"]) == "merge"
    assert wp._suggestion_kind_for_scored_signals(["company", "amount"]) == "merge"
    assert wp._suggestion_kind_for_scored_signals(["ariba_descriptor", "ariba_requester"]) == "merge"


def test_suggestion_kind_for_scored_signals_medium_plus_two_weak_merges():
    assert wp._suggestion_kind_for_scored_signals(["party", "sender", "category"]) == "merge"


def test_suggestion_kind_for_scored_signals_medium_plus_one_weak_is_link():
    """A medium anchor with only ONE weak signal alongside it (not two)
    doesn't clear his stated bar - his own example shape (sender alone
    paired with just one company/party match) stays a link, the exact
    task #81 lesson (party+sender was the original real bug's shape)."""
    assert wp._suggestion_kind_for_scored_signals(["party", "sender"]) == "link"
    assert wp._suggestion_kind_for_scored_signals(["company", "sender"]) == "link"


def test_suggestion_kind_for_scored_signals_two_weak_anchors_alone_is_link():
    """Weak anchors are 'useful only as supporting evidence' - his own
    words - never sufficient by themselves, even two of them with no
    medium/strong backing at all."""
    assert wp._suggestion_kind_for_scored_signals(["sender", "category"]) == "link"


def test_suggestion_kind_for_scored_signals_exactly_one_signal_is_link():
    """Exactly one data point is never enough to merge on its own - the
    task #81 lesson (a single shared party/company/topic proves a
    relationship, not the same transaction) - still surfaced as a link for
    a human to judge, never silently dropped."""
    assert wp._suggestion_kind_for_scored_signals(["party"]) == "link"
    assert wp._suggestion_kind_for_scored_signals(["company"]) == "link"
    assert wp._suggestion_kind_for_scored_signals(["topic"]) == "link"
    assert wp._suggestion_kind_for_scored_signals(["sender"]) == "link"


def test_suggestion_kind_for_scored_signals_no_signals_is_none():
    assert wp._suggestion_kind_for_scored_signals([]) is None


def test_group_issue_flag_on_same_supplier_and_topic_now_suggests_merge_kind(ws_db, monkeypatch, tmp_path):
    """Marc's corrected rule applied end-to-end: company + topic is 2
    MEDIUM anchors (his own framing: "supplier" + "specific product/
    service"), and the suggestion it produces is now correctly kind=
    'merge' (confirming it actually merges the two issues) rather than
    the old hierarchy's blanket 'link' for anything paired with company
    that wasn't one of four hand-picked "precise" signals.

    Doesn't reach action='auto_merged' here - a separate, still-open issue
    (see test_scored_grouping_decision_party_plus_topic_raw_score_
    composition_merge's docstring): confidence damping via
    referential_resolution caps effective_score below AUTO_MERGE_THRESHOLD
    for any pair with no captured reference ID, composition aside. What
    Marc's fix DOES change today: the suggestion Marc/curator actually
    sees is now correctly labeled 'merge', not 'link' - confirming it will
    really merge the two issues, not just record a relationship."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    a = _issue(ws_db, "Workday HCM SaaS renewal negotiation")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Workday HCM SaaS renewal negotiation follow-up")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "suggested"
    assert result["suggestion_kind"] == "merge"
    pending = ws_db.list_project_suggestions(status="pending")
    assert len(pending) == 1
    assert pending[0]["suggestion_kind"] == "merge"


def test_group_issue_flag_on_shared_definitive_reference_still_auto_merges(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    a = _issue(ws_db, "First notice")
    _raw_item(ws_db, a, "PR9112233 approval needed", "regr-ref-a")
    b = _issue(ws_db, "Totally different subject")
    _raw_item(ws_db, b, "REMINDER PR9112233", "regr-ref-b")

    result = wp.group_issue(a)

    assert result["action"] == "auto_merged"
    assert ws_db.get_issue(a)["project_id"] is not None
    assert ws_db.get_issue(a)["project_id"] == ws_db.get_issue(b)["project_id"]


def test_group_issue_flag_on_disjoint_definitive_references_vetoes_match(ws_db, monkeypatch, tmp_path):
    """Two issues at the same company but with DIFFERENT, disjoint PR
    numbers must not merge OR link - a_ids/b_ids both non-empty and
    disjoint zeroes the score outright inside _pairwise_score_from_
    signature, unchanged by this fix."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    a = _issue(ws_db, "Approve PR1000111")
    _raw_item(ws_db, a, "Approve PR1000111", "regr-vet-a")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Approve PR2000222")
    _raw_item(ws_db, b, "Approve PR2000222", "regr-vet-b")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "no_match"
    assert ws_db.list_project_suggestions(status="pending") == []


def test_group_issue_flag_on_cannot_link_constraint_blocks_suggestion(ws_db, monkeypatch, tmp_path):
    """A durable cannot_link constraint (v2.4) is caught even earlier than
    create_project_suggestion's own veto check: _pairwise_score_from_
    signature zeroes this pair's score to 0.0 outright (compute_work_
    object_signature's cannot_link_ids), so it never even becomes the
    scored model's best_sibling candidate - confirmed via no_match, not a
    create-then-drop."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")
    ws_db.create_identity_constraint("cannot_link", a, b, "confirmed separate", actor="marc")

    result = wp.group_issue(a)

    assert result["action"] == "no_match"
    assert ws_db.list_project_suggestions(status="pending") == []


def test_group_issue_flag_on_no_duplicate_suggestion_on_replay(ws_db, monkeypatch, tmp_path):
    """group_issue() is documented idempotent - calling it twice for the
    same still-ungrouped pair must not create a second pending 'link' row."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    wp.group_issue(a)
    wp.group_issue(a)

    assert len(ws_db.list_project_suggestions(status="pending")) == 1


# --- replay_scored_merge_link_regression -----------------------------------

def test_replay_repairs_a_wrongly_classified_merge_suggestion_into_link(ws_db):
    """Simulates the actual pre-fix bug: a pending 'merge' suggestion
    exists (as create_project_suggestion's old no-suggestion_kind call
    would have created it) for a pair whose live signals are really
    company-alone - the replay must expire the wrong row and create the
    correct 'link' suggestion in its place. Subjects deliberately don't
    share a topic-key match (unlike the sibling tests below) so the ONLY
    live signal is company - under Marc's 2026-08-04 count-based rule,
    exactly one matching data point is still never enough to merge on its
    own, the one case this replay still needs to catch."""
    a = _issue(ws_db, "Quarterly business review notes")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Invoice discrepancy follow-up")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")
    bad_sid = ws_db.create_project_suggestion(
        issue_id_a=a, issue_id_b=b,
        reason="scored signal (company, score=0.45)", suggestion_kind="merge",
    )

    result = wp.replay_scored_merge_link_regression(since_ts=0.0)

    assert result["repaired"] == 1
    assert ws_db.get_project_suggestion(bad_sid)["status"] == "expired"
    pending = ws_db.list_project_suggestions(status="pending")
    assert len(pending) == 1
    assert pending[0]["suggestion_kind"] == "link"


def test_replay_leaves_correctly_classified_merge_suggestions_alone(ws_db):
    a = _issue(ws_db, "First notice")
    _raw_item(ws_db, a, "PR7223344 approval needed", "regr-replay-ref-a")
    b = _issue(ws_db, "REMINDER PR7223344")
    _raw_item(ws_db, b, "REMINDER PR7223344", "regr-replay-ref-b")
    good_sid = ws_db.create_project_suggestion(
        issue_id_a=a, issue_id_b=b,
        reason="scored signal (topic, score=0.4)", suggestion_kind="merge",
    )

    result = wp.replay_scored_merge_link_regression(since_ts=0.0)

    assert result["repaired"] == 0
    assert ws_db.get_project_suggestion(good_sid)["status"] == "pending"


def test_replay_ignores_suggestions_before_since_ts(ws_db):
    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")
    old_sid = ws_db.create_project_suggestion(
        issue_id_a=a, issue_id_b=b,
        reason="scored signal (company,topic, score=0.8)", suggestion_kind="merge",
    )

    result = wp.replay_scored_merge_link_regression(since_ts=time.time() + 1000)

    assert result["repaired"] == 0
    assert ws_db.get_project_suggestion(old_sid)["status"] == "pending"


def test_replay_is_idempotent(ws_db):
    a = _issue(ws_db, "Quarterly business review notes")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Invoice discrepancy follow-up")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")
    ws_db.create_project_suggestion(
        issue_id_a=a, issue_id_b=b,
        reason="scored signal (company, score=0.45)", suggestion_kind="merge",
    )

    first = wp.replay_scored_merge_link_regression(since_ts=0.0)
    second = wp.replay_scored_merge_link_regression(since_ts=0.0)

    assert first["repaired"] == 1
    assert second["repaired"] == 0  # already-repaired pair has no pending 'merge' row left to touch
    assert len(ws_db.list_project_suggestions(status="pending")) == 1


def test_group_issue_same_category_flood_off_by_default_creates_no_suggestion(ws_db):
    """Phase 0 fix (D2): same-category-proximity candidate generation is
    OFF by default. A bare same-category pair with no other signal must not
    create a suggestion at all - this is the fix for the 2,004-row flood."""
    a = _issue(ws_db, "A")
    ws_db.update_issue(a, category="rfp-sourcing")
    b = _issue(ws_db, "B, totally unrelated")
    ws_db.update_issue(b, category="rfp-sourcing")

    result = wp.group_issue(a)

    assert result["action"] == "no_match"


def test_group_issue_same_category_flag_on_without_corroboration_creates_no_suggestion(ws_db, monkeypatch, tmp_path):
    """Even with the flag on, a bare category match with no OTHER
    signature-scored signal (no shared internal sender) is dropped, not
    suggested - corroboration is required, the flag alone isn't enough."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "same_category_proximity_suggestions_enabled")

    a = _issue(ws_db, "A")
    ws_db.update_issue(a, category="rfp-sourcing")
    b = _issue(ws_db, "B, totally unrelated")
    ws_db.update_issue(b, category="rfp-sourcing")

    result = wp.group_issue(a)

    assert result["action"] == "no_match"


def test_group_issue_same_category_flag_on_with_corroboration_creates_one_suggestion(ws_db, monkeypatch, tmp_path):
    """With the flag on AND a shared internal sender corroborating the
    category match, exactly one suggestion is created for the single
    best-scoring candidate - never one per candidate (the actual flood fix,
    even with the flag on)."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "same_category_proximity_suggestions_enabled")

    def _internal(issue_id, party_id):
        ws_db.upsert_party(id=party_id, primary_email="me@lilly.com", display_name="Me",
                            affiliation="internal", affiliation_confidence="H", affiliation_source="domain", company=None)
        ws_db.link_party_to_issue(issue_id, party_id)

    a = _issue(ws_db, "A")
    ws_db.update_issue(a, category="rfp-sourcing")
    _internal(a, "int-shared")
    b = _issue(ws_db, "B, totally unrelated")
    ws_db.update_issue(b, category="rfp-sourcing")
    _internal(b, "int-shared")
    c = _issue(ws_db, "C, also unrelated")
    ws_db.update_issue(c, category="rfp-sourcing")
    _internal(c, "int-shared")

    result = wp.group_issue(a)

    assert result["action"] == "suggested"
    assert result["count"] == 1


def test_run_suggestion_expiry_daily_if_due_expires_old_and_gates_second_call(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    sid = ws_db.create_project_suggestion(issue_id_a=a, issue_id_b=b, reason="test", suggestion_kind="merge")
    conn = ws_db._connect()
    conn.execute("UPDATE pending_project_suggestions SET created_ts = ? WHERE id = ?",
                 (time.time() - 30 * 86400, sid))
    conn.close()

    first = wp.run_suggestion_expiry_daily_if_due()
    assert first == {"expired": 1, "ttl_days": 21}
    assert ws_db.get_project_suggestion(sid)["status"] == "expired"

    second = wp.run_suggestion_expiry_daily_if_due()
    assert second is None


# --- backtest_scored_model (Part A2's required gate) ----------------------

def test_backtest_scored_model_is_read_only(ws_db):
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B, unrelated subject entirely")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    wp.backtest_scored_model()

    assert ws_db.get_issue(a)["project_id"] is None
    assert ws_db.get_issue(b)["project_id"] is None


def test_backtest_scored_model_flags_different_project_pair_scoring_above_threshold(ws_db):
    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    result = wp.backtest_scored_model()

    hits = result["different_project_pairs_at_or_above_threshold"]
    assert any({h["a"], h["b"]} == {a, b} for h in hits)


def test_backtest_scored_model_actual_verdict_reflects_tiered_anchor_rule(ws_db):
    """actual_verdict, restored (2026-08-04) after briefly looking redundant
    under the short-lived plain count-based rule: Marc's tiered-anchor
    correction means raw score crossing AUTO_MERGE_THRESHOLD does NOT
    always imply a real merge candidate again - party+sender reaches 0.65
    (0.45+0.20) but is medium+weak, one weak signal short of his stated
    bar, so actual_verdict must say "suggest" even though it's in the
    at-or-above-threshold list. party+topic (medium+medium, 0.90) is a
    genuine merge candidate for comparison."""
    a = _issue(ws_db, "Renewal negotiation")
    _link_party(ws_db, a, "p_shared", "rep@acme.com")
    ws_db.upsert_party(id="int-shared", primary_email="me@lilly.com", display_name="Me",
                        affiliation="internal", affiliation_confidence="H", affiliation_source="domain", company=None)
    ws_db.link_party_to_issue(a, "int-shared")
    b = _issue(ws_db, "A different real request, same two people")
    _link_party(ws_db, b, "p_shared", "rep@acme.com")
    ws_db.link_party_to_issue(b, "int-shared")

    c = _issue(ws_db, "Workday HCM SaaS renewal negotiation")
    _link_party(ws_db, c, "p_shared2", "rep2@acme.com")
    d = _issue(ws_db, "Workday HCM SaaS renewal negotiation follow-up")
    _link_party(ws_db, d, "p_shared2", "rep2@acme.com")

    result = wp.backtest_scored_model()
    hits = {frozenset({h["a"], h["b"]}): h for h in result["different_project_pairs_at_or_above_threshold"]}

    assert hits[frozenset({a, b})]["actual_verdict"] == "suggest"
    assert hits[frozenset({c, d})]["actual_verdict"] == "merge"


def test_backtest_scored_model_task81_boilerplate_case_stays_a_veto(ws_db):
    """Regression fixture: the real task #81 bug (boilerplate subject +
    Ariba's own no-reply sender defeating company identification) must
    NOT become a false positive under the scored model either - the
    disjoint-reference veto still applies."""
    a = _issue(ws_db, "Approve PR1111865")
    _raw_item(ws_db, a, _BOILERPLATE.format(name="BRIAN LAUGHLIN", pr="PR1111865"), "bt1")
    _link_party(ws_db, a, "ariba1", "no-reply@ansmtp.ariba.com")
    b = _issue(ws_db, "Approve PR1193376")
    _raw_item(ws_db, b, _BOILERPLATE.format(name="THOMAS TURNER", pr="PR1193376"), "bt2")
    _link_party(ws_db, b, "ariba2", "no-reply@ansmtp.ariba.com")

    result = wp.backtest_scored_model()

    hits = result["different_project_pairs_at_or_above_threshold"]
    assert not any({h["a"], h["b"]} == {a, b} for h in hits)


# --- run_retroactive_scored_reprocess (task #180, 2026-08-04, Marc's direct ---
# request: "once the build is complete, you need to go back over everything
# in the db with this new process") --------------------------------------

def test_run_retroactive_scored_reprocess_apply_requires_flag_on(ws_db, monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):
        wp.run_retroactive_scored_reprocess(apply=True)


def test_run_retroactive_scored_reprocess_dry_run_reports_without_mutating(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    a = _ariba_issue(ws_db, "JANE DOE", "PR991001", "Workday HCM SaaS", 75000.00)
    b = _ariba_issue(ws_db, "JANE DOE", "PR991001", "Workday HCM SaaS", 75000.00)

    result = wp.run_retroactive_scored_reprocess(apply=False)

    assert result["apply"] is False
    assert ws_db.get_issue(a)["project_id"] is None
    assert ws_db.get_issue(b)["project_id"] is None
    assert any(e["issue_id"] in (a, b) for e in result["auto_merged"])


def test_run_retroactive_scored_reprocess_apply_reconnects_already_grouped_issues(ws_db, monkeypatch, tmp_path):
    """The real point of this function: group_issue() can never reconsider
    an issue that already has a project_id (its own early return), so a
    clean reference-ID match between two issues ALREADY sitting in
    different, established projects would never get reconnected any other
    way."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    p1 = ws_db.create_project_with_new_id(name="P1", category="other")
    p2 = ws_db.create_project_with_new_id(name="P2", category="other")
    a = _issue(ws_db, "Approve PR775533")
    _raw_item(ws_db, a, "Approve PR775533", "rr1")
    ws_db.assign_issue_to_project(a, p1)
    b = _issue(ws_db, "RE: Approve PR775533")
    _raw_item(ws_db, b, "RE: Approve PR775533", "rr2")
    ws_db.assign_issue_to_project(b, p2)

    result = wp.run_retroactive_scored_reprocess(apply=True)

    assert ws_db.get_issue(a)["project_id"] == ws_db.get_issue(b)["project_id"]
    assert any(e["issue_id"] in (a, b) for e in result["auto_merged"])


def test_run_retroactive_scored_reprocess_dry_run_detects_a_real_bridge(ws_db, monkeypatch, tmp_path):
    """A dry run (apply=False, the required first pass against the real
    corpus) reads every issue against the SAME untouched snapshot, so a
    genuine 3-way bridge (b connects to both p1 and p2, each via its own
    independently-qualifying member) is correctly detected regardless of
    processing order - unlike apply=True, where an issue processed earlier
    in the same pass (a, being older) can legitimately claim b for its own
    project first, resolving the ambiguity as a direct merge rather than
    ever needing to ask "which one, human?" - not a bug, just a different
    (still safety-netted) outcome specific to a live, order-dependent
    apply pass. This test is scoped to what the dry run itself can
    guarantee: correct bridge STRUCTURE on a stable snapshot."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    p1 = ws_db.create_project_with_new_id(name="Project one", category="other")
    a = _ariba_issue(ws_db, "JANE DOE", "PR991001", "Workday HCM SaaS", 50000.00)
    ws_db.assign_issue_to_project(a, p1)
    p2 = ws_db.create_project_with_new_id(name="Project two", category="other")
    c = _ariba_issue(ws_db, "BOB SMITH", "PR991002", "Workday HCM SaaS", 100050.00)
    ws_db.assign_issue_to_project(c, p2)
    b = _ariba_issue(ws_db, "JANE DOE", None, "Workday HCM SaaS", 100050.00)

    result = wp.run_retroactive_scored_reprocess(apply=False)

    entry = next((e for e in result["bridged"] if e["issue_id"] == b), None)
    assert entry is not None
    assert {br["project_id"] for br in entry["bridges"]} == {p1, p2}
    # Dry run - genuinely nothing written.
    assert ws_db.get_issue(a)["project_id"] == p1
    assert ws_db.get_issue(b)["project_id"] is None
    assert ws_db.get_issue(c)["project_id"] == p2
    assert ws_db.list_project_suggestions(status="pending") == []


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


# --- backfill_identity_constraints_from_historical_rejections (task #156) --

def _rejected_suggestion(ws_db, issue_id_a, issue_id_b, reason, resolved_ts, *,
                          suggestion_kind="merge", created_ts=None):
    """Direct SQL insert so the test can control resolved_ts precisely
    (the timestamp-clustering signature the backfill's own selectivity
    filter looks for) - the real create_project_suggestion/
    resolve_project_suggestion path always uses wall-clock time.time(),
    which can't reproduce a fixture with an exact past cluster."""
    conn = ws_db._connect()
    cur = conn.execute(
        """INSERT INTO pending_project_suggestions
           (issue_id_a, issue_id_b, reason, status, created_ts, resolved_ts, suggestion_kind)
           VALUES (?, ?, ?, 'rejected', ?, ?, ?)""",
        (issue_id_a, issue_id_b, reason, created_ts or (resolved_ts - 3600), resolved_ts, suggestion_kind),
    )
    conn.close()
    return cur.lastrowid


def test_backfill_dry_run_reports_eligible_row_without_writing(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    _rejected_suggestion(ws_db, a, b, "possibly related (not necessarily same project) - shared external party 'party-x'",
                          resolved_ts=1000000.0, suggestion_kind="link")

    result = wp.backfill_identity_constraints_from_historical_rejections(apply=False)

    assert result["eligible_explicit_rejects"] == 1
    assert result["new_constraints"] == 1
    assert result["applied"] is False
    assert ws_db.find_identity_constraint("cannot_link", a, b) is None


def test_backfill_apply_creates_the_constraint(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    _rejected_suggestion(ws_db, a, b, "possibly related (not necessarily same project) - shared external party 'party-x'",
                          resolved_ts=1000000.0, suggestion_kind="link")

    result = wp.backfill_identity_constraints_from_historical_rejections(apply=True)

    assert result["new_constraints"] == 1
    assert result["applied"] is True
    constraint = ws_db.find_identity_constraint("cannot_link", a, b)
    assert constraint is not None
    assert "suggestion" in constraint["reason"]


def test_backfill_excludes_boilerplate_weak_signal_reason(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    _rejected_suggestion(ws_db, a, b, "same category ('contract') within 45d, no shared external contact found",
                          resolved_ts=1000000.0, suggestion_kind="merge")

    result = wp.backfill_identity_constraints_from_historical_rejections(apply=True)

    assert result["eligible_explicit_rejects"] == 0
    assert result["bulk_cleanup_artifact_rejects"] == 1
    assert ws_db.find_identity_constraint("cannot_merge", a, b) is None


def test_backfill_excludes_clustered_bulk_rejections(ws_db):
    """A burst of rows all resolving within the same rounded second is a
    scripted bulk pass, not one-by-one review - even with specific,
    non-boilerplate-looking reason text."""
    same_second = 2000000.0
    pairs = []
    for i in range(5):
        a = _issue(ws_db, f"A{i}")
        b = _issue(ws_db, f"B{i}")
        pairs.append((a, b))
        _rejected_suggestion(ws_db, a, b, f"scored signal (topic, score=0.{i})",
                              resolved_ts=same_second + i * 0.1, suggestion_kind="link")

    result = wp.backfill_identity_constraints_from_historical_rejections(apply=True)

    assert result["eligible_explicit_rejects"] == 0
    assert result["bulk_cleanup_artifact_rejects"] == 5
    for a, b in pairs:
        assert ws_db.find_identity_constraint("cannot_link", a, b) is None


def test_backfill_skips_pairs_already_constrained(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    ws_db.create_identity_constraint("cannot_merge", a, b, reason="prior", actor="marc")
    _rejected_suggestion(ws_db, a, b, "possibly related (not necessarily same project) - strong signal: shared external party",
                          resolved_ts=1000000.0, suggestion_kind="merge")

    result = wp.backfill_identity_constraints_from_historical_rejections(apply=True)

    assert result["eligible_explicit_rejects"] == 1
    assert result["already_constrained"] == 1
    assert result["new_constraints"] == 0


def test_backfill_skips_non_merge_link_suggestion_kind(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    _rejected_suggestion(ws_db, a, b, "collision between two established projects",
                          resolved_ts=1000000.0, suggestion_kind="merge_projects")

    result = wp.backfill_identity_constraints_from_historical_rejections(apply=True)

    assert result["non_constraint_kind_rejects"] == 1
    assert result["eligible_explicit_rejects"] == 0


def test_backfill_apply_twice_is_idempotent(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    _rejected_suggestion(ws_db, a, b, "possibly related (not necessarily same project) - shared external party 'party-x'",
                          resolved_ts=1000000.0, suggestion_kind="link")

    first = wp.backfill_identity_constraints_from_historical_rejections(apply=True)
    second = wp.backfill_identity_constraints_from_historical_rejections(apply=True)

    assert first["new_constraints"] == 1
    assert second["new_constraints"] == 0
    assert second["already_constrained"] == 1


# --- new content-extracted pairwise signals (task #169/#170, 2026-08-04) ----
# ariba_descriptor/ariba_requester/amount/attachment, added to _pairwise_
# score_from_signature so "supplier + one other real data point" (Marc's
# stated rule) actually has enough combinable signals to clear
# AUTO_MERGE_THRESHOLD for Ariba's automated notifications specifically -
# is_automated_sender already excludes the notification address itself from
# party/company matching, so without these, two different Ariba requisitions
# (or two versions of the same one) looked identical to the signature.

def _sig(**overrides):
    base = {
        "definitive_ids": [], "accepted_lineages": [], "containers": [],
        "external_orgs": [], "participant_roles": [], "active_period_start": None,
        "active_period_end": None, "positive_vocabulary": None, "negative_vocabulary": None,
        "cannot_link_ids": [],
    }
    base.update(overrides)
    return base


def test_pairwise_score_ariba_descriptor_and_requester_together_merges():
    a = _sig(positive_vocabulary={"ariba_requester": "Thomas Turner", "ariba_descriptor": "Workday HCM SaaS", "value_amount": None})
    b = _sig(positive_vocabulary={"ariba_requester": "Thomas Turner", "ariba_descriptor": "Workday HCM SaaS", "value_amount": None})
    score, signals = wp._pairwise_score_from_signature("a", a, "", None, "b", b, "", None)
    assert "ariba_descriptor" in signals
    assert "ariba_requester" in signals
    assert score >= wp.AUTO_MERGE_THRESHOLD


def test_pairwise_score_ariba_descriptor_alone_does_not_reach_threshold():
    a = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": "Workday HCM SaaS", "value_amount": None})
    b = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": "Workday HCM SaaS", "value_amount": None})
    score, signals = wp._pairwise_score_from_signature("a", a, "", None, "b", b, "", None)
    assert signals == ["ariba_descriptor"]
    assert score < wp.AUTO_MERGE_THRESHOLD


def test_pairwise_score_descriptor_plus_amount_merges():
    a = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": "Workday HCM SaaS", "value_amount": 53702143.0})
    b = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": "Workday HCM SaaS", "value_amount": 53702143.0})
    score, signals = wp._pairwise_score_from_signature("a", a, "", None, "b", b, "", None)
    assert "amount" in signals
    assert score >= wp.AUTO_MERGE_THRESHOLD


def test_pairwise_score_amount_requires_close_match_not_exact():
    """A 1% tolerance is real-world tolerant (rounding, currency
    conversion noise) without being so loose two unrelated deals coincide."""
    a = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": None, "value_amount": 100000.0})
    b = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": None, "value_amount": 100500.0})
    score, signals = wp._pairwise_score_from_signature("a", a, "", None, "b", b, "", None)
    assert "amount" in signals

    c = _sig(positive_vocabulary={"ariba_requester": None, "ariba_descriptor": None, "value_amount": 150000.0})
    score2, signals2 = wp._pairwise_score_from_signature("a", a, "", None, "c", c, "", None)
    assert "amount" not in signals2


def test_pairwise_score_attachment_lineage_overlap_matches():
    a = _sig(accepted_lineages=["lineage-abc123"])
    b = _sig(accepted_lineages=["lineage-abc123", "lineage-def456"])
    score, signals = wp._pairwise_score_from_signature("a", a, "", None, "b", b, "", None)
    assert "attachment" in signals


def test_pairwise_score_no_new_signals_when_vocab_empty():
    a = _sig()
    b = _sig()
    score, signals = wp._pairwise_score_from_signature("a", a, "", None, "b", b, "", None)
    assert score == 0.0
    assert signals == []


def test_suggestion_kind_company_alone_stays_link():
    assert wp._suggestion_kind_for_scored_signals(["company"]) == "link"


def test_suggestion_kind_company_plus_medium_anchor_merges():
    """Marc's tiered-anchor correction (2026-08-04, superseding the
    plain-count rule this replaces same day): company is a MEDIUM anchor,
    so it merges when paired with another MEDIUM anchor (2 medium anchors
    clears his stated bar) - but NOT when paired with only a WEAK one
    (sender), which needs a second weak signal alongside it instead.

    company+topic specifically is flagged, not silently assumed settled:
    it's the exact shape of the real task #81 incident (two DIFFERENT
    Ariba reps' DIFFERENT requisitions at the same company, sharing only
    boilerplate phrasing, no reference number captured on either side to
    trigger the disjoint-reference veto) - 71 issues wrongly merged into
    one project. Whether that specific combination should still be an
    exception to the general rule is an open question raised back to
    Marc, not decided here - this test locks in the code's CURRENT
    behavior (merge, since topic is medium-tier) so a future change shows
    up as a deliberate diff."""
    assert wp._suggestion_kind_for_scored_signals(["company", "amount"]) == "merge"
    assert wp._suggestion_kind_for_scored_signals(["company", "ariba_descriptor"]) == "merge"
    assert wp._suggestion_kind_for_scored_signals(["company", "topic"]) == "merge"
    assert wp._suggestion_kind_for_scored_signals(["company", "sender"]) == "link"


def test_suggestion_kind_combined_new_signals_merge():
    """This function is a pure signal-name-set -> kind mapping - it doesn't
    itself know whether the underlying score crossed AUTO_MERGE_THRESHOLD
    (that's SCORE_WEIGHTS' job; amount=0.25 alone never reaches 0.65, so
    scored_grouping_decision never calls this with signals=['amount']
    alone in practice)."""
    assert wp._suggestion_kind_for_scored_signals(["ariba_descriptor", "ariba_requester"]) == "merge"
    assert wp._suggestion_kind_for_scored_signals([]) is None


def test_suggestion_kind_party_and_topic_semantics_unchanged():
    assert wp._suggestion_kind_for_scored_signals(["party"]) == "link"
    assert wp._suggestion_kind_for_scored_signals(["party", "topic"]) == "merge"
    assert wp._suggestion_kind_for_scored_signals(["topic"]) == "link"


# --- connected-components candidate search + bridge detection --------------
# (task #169/#170, 2026-08-04, Marc's direct design ask). The OLD
# scored_grouping_decision excluded any candidate already in a DIFFERENT
# project than the issue being scored - meaning an ungrouped item could
# NEVER join an existing, already-established project via this path, and a
# real chain (A-B share 2 points, B-C share 2 DIFFERENT points) could never
# be discovered once B had a project. Now searches the whole corpus and
# tracks the best match PER distinct project, so it can also detect when an
# item bridges two already-separate, already-established projects.

def _ariba_issue(ws_db, requester, pr_number, descriptor, amount, *, category="financial"):
    """Real Ariba requisition-approval shape (see workgraph_signals.
    extract_ariba_requisition_fields) - requester/descriptor come from the
    issue title, amount comes from value_amount_for_issue's own raw_items
    scan, so a real raw_item with the dollar figure in its subject is
    needed too, not just the issue title. pr_number=None omits the PR
    segment entirely (a real Ariba shape too - some notifications reference
    a supplier/requester without yet having an assigned PR#) - needed for
    bridge tests, since two issues with DIFFERENT real PR numbers are
    correctly vetoed to 0 by the disjoint-reference-id check regardless of
    any other signal (each real PR# is its own true transaction) - bridging
    across distinct requisitions has to go through an item that doesn't
    itself carry a conflicting PR#."""
    pr_segment = f"{pr_number} - " if pr_number else ""
    title = f"Action required: Approve the Requisition that {requester} submitted  - {pr_segment}{descriptor} (${amount:,.2f} USD)"
    iid = _issue(ws_db, title)
    ws_db.update_issue(iid, category=category)
    _raw_item(ws_db, iid, title, f"key-{iid}")
    return iid


def test_scored_grouping_decision_can_now_join_an_existing_established_project(ws_db):
    p1 = ws_db.create_project_with_new_id(name="Existing project", category="other")
    member = _ariba_issue(ws_db, "JANE DOE", "PR991001", "Workday HCM SaaS", 75000.00)
    ws_db.assign_issue_to_project(member, p1)

    new_issue = _ariba_issue(ws_db, "JANE DOE", None, "Workday HCM SaaS", 75050.00)

    decision = wp.scored_grouping_decision(new_issue, ws_db.get_issue(new_issue))

    # ariba_requester+ariba_descriptor clears threshold - the exact combo
    # the OLD candidate-exclusion made structurally impossible to even
    # consider once `member` already had a project.
    assert decision["sibling_id"] == member
    assert p1 in decision["bridged_projects"]


def test_scored_grouping_decision_detects_a_real_bridge_between_two_projects(ws_db):
    """Marc's exact example: a new item can share 2+ points with a member
    of project P1 AND 2+ (possibly DIFFERENT) points with a member of
    project P2 - a real bridge between two already-established groups, not
    a clean single-project match. Must be flagged for real judgment, not
    silently resolved to whichever scored highest."""
    p1 = ws_db.create_project_with_new_id(name="Project one", category="other")
    a = _ariba_issue(ws_db, "JANE DOE", "PR991001", "Workday HCM SaaS", 50000.00)
    ws_db.assign_issue_to_project(a, p1)

    p2 = ws_db.create_project_with_new_id(name="Project two", category="other")
    c = _ariba_issue(ws_db, "BOB SMITH", "PR991002", "Workday HCM SaaS", 100050.00)
    ws_db.assign_issue_to_project(c, p2)

    # b bridges both: shares (requester+descriptor) with a, shares
    # (descriptor+amount) with c - two DIFFERENT signal pairs, against two
    # DIFFERENT already-established projects.
    b = _ariba_issue(ws_db, "JANE DOE", None, "Workday HCM SaaS", 100050.00)

    decision = wp.scored_grouping_decision(b, ws_db.get_issue(b))

    assert decision["verdict"] == "bridge"
    assert set(decision["bridged_projects"].keys()) == {p1, p2}


def test_group_issue_bridge_creates_a_suggestion_per_bridged_project(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    p1 = ws_db.create_project_with_new_id(name="Project one", category="other")
    a = _ariba_issue(ws_db, "JANE DOE", "PR991001", "Workday HCM SaaS", 50000.00)
    ws_db.assign_issue_to_project(a, p1)

    p2 = ws_db.create_project_with_new_id(name="Project two", category="other")
    c = _ariba_issue(ws_db, "BOB SMITH", "PR991002", "Workday HCM SaaS", 100050.00)
    ws_db.assign_issue_to_project(c, p2)

    b = _ariba_issue(ws_db, "JANE DOE", None, "Workday HCM SaaS", 100050.00)

    result = wp.group_issue(b)

    assert result["action"] == "bridge_suggested"
    assert result["count"] == 2
    bridged_project_ids = {entry["project_id"] for entry in result["bridges"]}
    assert bridged_project_ids == {p1, p2}
    pending = ws_db.list_project_suggestions(status="pending")
    assert len(pending) == 2


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
    very next classify/grouping cycle would just re-score the same
    signature and merge it right back in. This checks the REAL consumer
    (compute_work_object_signature's cannot_link_ids, same field
    _pairwise_score_from_signature's veto reads) not just that a row got
    written."""
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


def _close_issue_at(ws_db, issue_id, state, updated_at):
    """Directly backdates updated_at after the state change - update_issue's
    own touch_updated_at=True would otherwise stamp 'now', making it
    impossible to construct a closed issue that's OLDER than the grace
    period (same reasoning as the top-of-file _issue helper's opened_at
    patch)."""
    ws_db.update_issue(issue_id, state=state)
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (updated_at, issue_id))
    conn.close()


def test_candidate_pool_includes_open_issues_regardless_of_age(ws_db):
    """Task #177: an OPEN issue stays in scope no matter how old - only
    CLOSED issues age out. opened_at (not updated_at) is what's ancient
    here; the issue is still 'active', which is what should matter."""
    old_open = _issue(ws_db, "Ancient but still active", opened_at=time.time() - 400 * 86400)

    pool_ids = {i["id"] for i in wp._candidate_pool()}

    assert old_open in pool_ids


def test_candidate_pool_excludes_closed_issues_past_the_default_grace_period(ws_db):
    a = _issue(ws_db, "Closed long ago")
    _close_issue_at(ws_db, a, "done", time.time() - (wp.GROUPING_LOOKBACK_GRACE_DAYS + 5) * 86400)

    pool_ids = {i["id"] for i in wp._candidate_pool()}

    assert a not in pool_ids


def test_candidate_pool_includes_closed_issues_within_the_default_grace_period(ws_db):
    a = _issue(ws_db, "Closed recently")
    _close_issue_at(ws_db, a, "done", time.time() - (wp.GROUPING_LOOKBACK_GRACE_DAYS - 5) * 86400)

    pool_ids = {i["id"] for i in wp._candidate_pool()}

    assert a in pool_ids


def test_candidate_pool_lookback_days_override_widens_the_window(ws_db):
    """Marc's explicit follow-up: 'I do want to be able to use a worker via
    chat to look back further if necessary' - a caller-supplied
    lookback_days should surface something the default window would have
    missed."""
    a = _issue(ws_db, "Closed a year ago")
    _close_issue_at(ws_db, a, "done", time.time() - 300 * 86400)

    default_pool_ids = {i["id"] for i in wp._candidate_pool()}
    wide_pool_ids = {i["id"] for i in wp._candidate_pool(lookback_days=365)}

    assert a not in default_pool_ids
    assert a in wide_pool_ids


def test_scored_grouping_decision_lookback_days_surfaces_an_old_closed_match(ws_db):
    """End-to-end: the same real match (2+ Ariba signals) is invisible to
    the default-scoped decision once the sibling is closed-and-old, but
    found again when a caller explicitly asks to look back further."""
    old_sibling = _ariba_issue(ws_db, "JANE DOE", "PR991001", "Workday HCM SaaS", 75000.00)
    _close_issue_at(ws_db, old_sibling, "done", time.time() - 300 * 86400)
    new_issue = _ariba_issue(ws_db, "JANE DOE", None, "Workday HCM SaaS", 75050.00)

    default_decision = wp.scored_grouping_decision(new_issue, ws_db.get_issue(new_issue))
    wide_decision = wp.scored_grouping_decision(new_issue, ws_db.get_issue(new_issue), lookback_days=365)

    assert default_decision["verdict"] == "no_match"
    assert wide_decision["sibling_id"] == old_sibling
