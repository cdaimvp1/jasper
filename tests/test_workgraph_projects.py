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


# --- reference_ids_for_issue --------------------------------------------

def test_reference_ids_extracts_pr_number(ws_db):
    iid = _issue(ws_db, "Requisition")
    _raw_item(ws_db, iid, "Approve PR1111865 - SAP RISE", "r1")
    assert wp.reference_ids_for_issue(iid) == {"PR1111865"}


def test_reference_ids_extracts_versioned_pr_number(ws_db):
    iid = _issue(ws_db, "Requisition")
    _raw_item(ws_db, iid, "PR416079-V33 - Tower X PO Request", "r2")
    assert wp.reference_ids_for_issue(iid) == {"PR416079-V33"}


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


def test_pairwise_score_single_signal_never_reaches_threshold(ws_db):
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B, unrelated subject entirely")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    snap_a = wp._issue_signal_snapshot(a, ws_db.get_issue(a))
    snap_b = wp._issue_signal_snapshot(b, ws_db.get_issue(b))
    score, signals = wp._pairwise_score(snap_a, snap_b)

    assert signals == ["company"]
    assert score == wp.SCORE_WEIGHTS["company"]
    assert score < wp.AUTO_MERGE_THRESHOLD


def test_pairwise_score_company_and_topic_combine_above_threshold(ws_db):
    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    snap_a = wp._issue_signal_snapshot(a, ws_db.get_issue(a))
    snap_b = wp._issue_signal_snapshot(b, ws_db.get_issue(b))
    score, signals = wp._pairwise_score(snap_a, snap_b)

    assert set(signals) == {"company", "topic"}
    assert score == wp.SCORE_WEIGHTS["company"] + wp.SCORE_WEIGHTS["topic"]
    assert score >= wp.AUTO_MERGE_THRESHOLD


def test_pairwise_score_category_other_never_contributes(ws_db):
    a = _issue(ws_db, "A")
    b = _issue(ws_db, "B")
    snap_a = wp._issue_signal_snapshot(a, ws_db.get_issue(a))
    snap_b = wp._issue_signal_snapshot(b, ws_db.get_issue(b))
    score, signals = wp._pairwise_score(snap_a, snap_b)
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

    snap_a = wp._issue_signal_snapshot(a, ws_db.get_issue(a))
    snap_b = wp._issue_signal_snapshot(b, ws_db.get_issue(b))
    score, signals = wp._pairwise_score(snap_a, snap_b)

    assert score == 0.0
    assert signals == []


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


def test_scored_grouping_decision_shared_party_plus_topic_is_auto_merge(ws_db):
    """A shared party COMBINED with a second corroborating signal (topic)
    still auto-merges - party contributes like company/topic/sender/category
    already did, it just can't clear AUTO_MERGE_THRESHOLD alone anymore."""
    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p_shared2", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p_shared2", "rep@acme.com", company="Acme")

    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert decision["verdict"] == "auto_merge"
    assert decision["sibling_id"] == b
    assert "party" in decision["matched_signals"]


def test_scored_grouping_decision_combined_weak_signals_auto_merge(ws_db):
    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert decision["verdict"] == "auto_merge"
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


def test_scored_grouping_decision_attaches_confidence_spine_fields_observe_only(ws_db):
    """Confidence spine v0 (2026-08-03): computed and attached for shadow-
    log/backtest review, but must NOT change the verdict here - this
    shadow-only model's decision is still the raw ordered score alone until
    a real backtest reviews the damped thresholds (same discipline already
    required before scored_model_enabled itself is ever flipped on)."""
    a = _issue(ws_db, "A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B, unrelated subject entirely")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    decision = wp.scored_grouping_decision(a, ws_db.get_issue(a))

    assert 0.0 <= decision["context_accuracy"] <= 1.0
    assert decision["effective_score"] == round(decision["score"] * decision["context_accuracy"], 6)
    assert decision["verdict"] == "suggest"  # unchanged by the spine - observe-only


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
    """Real behavior check: a pair scoring high under the scored model
    (shared company) only SUGGESTS under the live, narrowed (2026-07-31)
    ordered model - which is the point of the narrowing: shared company
    alone is no longer auto-merge-worthy in the path that's actually live
    while the flag is off, even though shadow_scored still reports what the
    scored model itself would have done. Confirms the flag really gates
    which model ACTS, not just which model is computed."""
    config = _isolate_config(monkeypatch, tmp_path)
    assert config.get("grouping", "scored_model_enabled") in (None, False)

    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "suggested"
    assert result["shadow_scored"]["verdict"] == "auto_merge"


def test_group_issue_flag_on_uses_scored_model(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "scored_model_enabled")

    a = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Action required: Approve the Requisition that BRIAN submitted again")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    result = wp.group_issue(a)

    assert result["action"] == "auto_merged"
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
    _pairwise_score signal (no shared internal sender) is dropped, not
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
