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


def test_strong_signal_match_prefers_reference_over_party(ws_db):
    """Reference ID is checked FIRST - even when a shared external party
    would also match, the reference-id result (and its label) wins."""
    a = _issue(ws_db, "A")
    _raw_item(ws_db, a, "PR654321 approval needed", "r7")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "B")
    _raw_item(ws_db, b, "PR654321 approval needed again", "r8")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")

    kind, detail, sibling_id = wp._strong_signal_match(a, ws_db.get_issue(a))

    assert kind == "reference"
    assert detail == "PR654321"
    assert sibling_id == b


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
