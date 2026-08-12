"""Regression corpus for workgraph_pipeline2.py's grouping/candidate-judgment
logic (task #333) - tricky, real-world-shaped cases that go beyond the
mechanical unit tests in test_workgraph_pipeline2.py. That file proves each
piece (find_candidates, judge_candidates, process_new_item, run_project_
extraction) does what its own docstring says in isolation; this file's job is
to pin down the SCENARIOS that have actually bitten (or nearly bitten) this
pipeline in practice - forwarded mail, subject drift, attachment-only
identity, chained matches across separate comparative candidate sets, and the
recovery mechanisms for when grouping gets it wrong in either direction.

Deliberately incremental (per task #333's own framing): this is a first real
batch, not an attempt at exhaustive coverage. Add to it as new tricky cases
are found in production rather than trying to anticipate everything here.

Same "no live `claude` calls" discipline as test_workgraph_pipeline2.py -
subprocess.Popen is monkeypatched throughout; the deterministic candidate-
detection and orchestration logic runs for real, only the LLM judgment
itself is a controlled fake. Company/person names below are all invented
for these fixtures (the Scriptly/Sodalis pairing carries over from the
already-anonymized real case that motivated the related_different_project
verdict in the first place - see workgraph_pipeline2.judge_candidates' own
docstring and test_workgraph_pipeline2.py's existing coverage of that exact
pair)."""
from __future__ import annotations

import subprocess

import pytest

import workgraph_pipeline2 as p2
import workgraph_projects as wp
import workgraph_lessons


# --- shared fixtures/helpers (same shapes as test_workgraph_pipeline2.py) --

def _issue(ws_db, title, category="other"):
    return ws_db.create_issue_with_new_id(title=title, category=category, state="active")


def _link_party(ws_db, issue_id, party_id, email, *, company=None):
    ws_db.upsert_party(id=party_id, primary_email=email, display_name=party_id,
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company=company)
    ws_db.link_party_to_issue(issue_id, party_id)


def _raw_item(ws_db, issue_id, subject, key, body_preview=None):
    import time
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject=subject, from_actor="a@example.com",
        participants_json="[]", body_preview=body_preview or subject,
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    return rid


def _classify_with_reference(ws_db, raw_item_id, pr_number_base):
    """Stamps a raw_item with a PR/PO reference the same way real
    classification does (workgraph_classify.py), the only way
    reference_base_ids_for_issue's title/body-derived half ever sees a
    value in these fixtures - see its own docstring for why this column,
    not a live regex over body text, is what compute_work_object_
    signature's definitive_ids reads for a non-attachment reference."""
    ws_db.classify_raw_item(
        raw_item_id, item_class="OTHER", direction="inbound", direction_inferred=True,
        topic="other", topic_inferred=True, sentiment="neutral", sentiment_inferred=True,
        anomaly_flag=False, pr_number=pr_number_base, pr_number_base=pr_number_base,
    )


def _attach_reference_document(ws_db, issue_id, filename, reference_id):
    """An attachment scoped directly to the issue whose extracted_text
    carries a real PR/PO-shaped reference - the exact mechanism
    reference_base_ids_for_issue's 2026-08-06 fix added (a signed CR PDF
    carrying a reference nowhere present in any email body/subject on the
    same issue). sha256_hex=None deliberately - these fixtures don't care
    about the duplicate-attachment/artifact-lineage path, only about the
    reference number showing up in extracted_text."""
    ws_db.create_attachment(
        entity_type="issue", entity_id=issue_id, kind="reference", filename=filename,
        stored_path=f"/fake/{filename}", content_type="application/pdf", size_bytes=1024,
        sha256_hex=None, uploaded_by="test",
        extracted_text=f"Change order reference {reference_id} confirmed and countersigned.",
    )


class _FakeProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.args = ["claude"]
        self.pid = 12345
        self.sent_input = None

    def communicate(self, input=None, timeout=None):
        self.sent_input = input
        return self.stdout, self.stderr


def _mock_claude(monkeypatch, stdout: str):
    def fake_popen(*a, **kw):
        return _FakeProcess(stdout)
    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)


def _mock_claude_choosing_marker(monkeypatch, marker: str, verdict: str):
    """For a single process_new_item call that judges MULTIPLE candidates
    in ONE comparative pass (2026-08-11 rewrite, review point #8) - real
    production shape whenever an item has more than one genuine 2+-point
    candidate. `marker` is a short substring unique to exactly one
    candidate's own CANDIDATE block (so it appears in that candidate's
    block only, never elsewhere in the prompt); the fake finds which
    numbered CANDIDATE block actually contains it and responds "MATCH:
    <that number>\\nVERDICT: <verdict>" - the comparative call can only
    ever choose ONE candidate, unlike the old per-candidate independent
    calls this replaced."""
    import re

    class _MarkerProcess(_FakeProcess):
        def __init__(self):
            super().__init__("")

        def communicate(self, input=None, timeout=None):
            self.sent_input = input
            text = input or ""
            parts = re.split(r"CANDIDATE (\d+) \(already tracked", text)
            for i in range(1, len(parts), 2):
                num = parts[i]
                block = parts[i + 1] if i + 1 < len(parts) else ""
                if marker in block:
                    return f"MATCH: {num}\nVERDICT: {verdict}\n", ""
            return "MATCH: NONE\n", ""

    def fake_popen(*a, **kw):
        return _MarkerProcess()
    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)


# ===========================================================================
# 1. Subcontractor/prime relationship - related_different_project, extended
# ===========================================================================
# test_workgraph_pipeline2.py already covers the base Scriptly/Sodalis case
# (one candidate, verdict related_different_project, item falls through to
# its own new project). The real gap: production items routinely have
# MULTIPLE candidates at once. Under the comparative-judgment redesign
# (review point #8, 2026-08-11), the model sees every candidate in one call
# and picks the single best match - the case below confirms it correctly
# picks the genuine duplicate over the subcontractor candidate, and that
# the non-chosen candidate gets no bookkeeping this call.

def test_comparative_call_picks_the_real_match_over_a_related_different_project_candidate(ws_db, isolated_paths, monkeypatch):
    """New item has two 2+-point candidates: one is a genuine duplicate of
    the SAME underlying deal (thread drift - same vendor, same named
    contact, just a later message), the other is a related subcontractor
    engagement under a different prime relationship (the Scriptly/Sodalis
    shape). Rewritten 2026-08-11 for the comparative-judgment redesign
    (review point #8): ONE call now sees both candidates at once and must
    choose the single best match, rather than two independent pairwise
    calls each free to reach their own verdict. Confirms the genuine
    duplicate is correctly merged, and - the direct behavior change from
    the old design - the OTHER (non-chosen) candidate gets no relationship
    write and no other effect from THIS call; that signal only fires on a
    call where the subcontractor candidate is itself the one chosen (see
    test_process_new_item_related_different_project_writes_relationship_
    signal in test_workgraph_pipeline2.py for that case in isolation)."""
    n = _issue(ws_db, "Renewal terms for the Northwind Analytics subscription")
    _link_party(ws_db, n, "party_kim", "kim@northwind-analytics.example", company="Northwind Analytics")

    dup = _issue(ws_db, "Northwind Analytics renewal - follow-up on pricing")
    _link_party(ws_db, dup, "party_kim", "kim@northwind-analytics.example", company="Northwind Analytics")
    _raw_item(ws_db, dup, "Northwind Analytics renewal - follow-up on pricing", "dup-1",
              body_preview="Following up on pricing for the renewal.")
    project_dup = ws_db.create_project_with_new_id(name="Northwind Analytics renewal", category="other")
    ws_db.assign_issue_to_project(dup, project_dup)

    sub = _issue(ws_db, "Halyard Data Services subcontract under Northwind Analytics MSA")
    _link_party(ws_db, sub, "party_kim", "kim@northwind-analytics.example", company="Northwind Analytics")
    _raw_item(ws_db, sub, "Halyard Data Services subcontract under Northwind Analytics MSA", "sub-1",
              body_preview="Halyard Data Services will handle the subcontracted work.")
    project_sub = ws_db.create_project_with_new_id(name="Halyard Data Services subcontract", category="other")
    ws_db.assign_issue_to_project(sub, project_sub)

    _mock_claude_choosing_marker(monkeypatch, "follow-up on pricing", "same_project")

    result = p2.process_new_item(n)

    assert result["action"] == "merged"
    assert result["project_id"] == project_dup
    # The subcontractor candidate was not chosen this call - no
    # relationship write, and its own project stays untouched.
    assert ws_db.list_work_object_relationships_by_type("rejected") == []
    assert ws_db.get_issue(sub)["project_id"] == project_sub


def test_related_different_project_is_not_a_permanent_veto_unlike_a_manual_split(ws_db, isolated_paths, monkeypatch):
    """The whole point of relationship_type='rejected' being a SEPARATE
    table from identity_constraints (cannot_merge/cannot_link) is that it's
    a soft, informational signal - not a durable block. A prime/sub pair
    correctly kept apart today must still be re-examinable tomorrow (e.g.
    once real evidence they've actually consolidated shows up), unlike a
    human's explicit split_issue_from_project call (see that mechanism's
    own regression case below), which IS meant to be durable."""
    n = _issue(ws_db, "New Scriptly work order")
    _link_party(ws_db, n, "party_pat", "pat@scriptly-sodalis.example", company="Scriptly")
    b = _issue(ws_db, "Existing Sodalis MSA")
    _link_party(ws_db, b, "party_pat", "pat@scriptly-sodalis.example", company="Scriptly")
    project_b = ws_db.create_project_with_new_id(name="Sodalis MSA project", category="other")
    ws_db.assign_issue_to_project(b, project_b)
    _mock_claude(monkeypatch, "MATCH: 1\nVERDICT: related_different_project\n")

    result = p2.process_new_item(n)
    assert result["action"] == "new_project"

    # Unlike a cannot_merge constraint, the relationship write leaves the
    # pair fully eligible to be found as a candidate again on a later pass.
    candidates_again = p2.find_candidates(n)
    assert any(c["candidate_id"] == b for c in candidates_again)


# ===========================================================================
# 2. Forwarded mail and subject-line drift within a "thread"
# ===========================================================================
# Step 2 (exact thread/message-id matching, outside this pipeline) already
# handles a forward that STAYS inside the same Outlook conversationId. The
# tricky case for pipeline2 is a forward that breaks threading - a genuinely
# new thread_key/stable_key, same underlying deal - and a thread whose
# subject line changes entirely partway through (e.g. after a decision is
# made), where title-based topic matching can no longer carry the pair at
# all and other data points have to do the work instead.

def test_forwarded_mail_with_broken_threading_still_matches_on_company_and_subject_core(ws_db, isolated_paths, monkeypatch):
    """Real-world shape: someone forwards an email to a new external
    recipient. Outlook can mint a new conversationId for the forwarded
    copy (breaking step 2's exact thread-key match), the sender is now a
    completely different internal person, but the FW: subject and the
    original vendor are still recognizably the same deal. normalize_
    topic_key strips the FW:/Re:/[tag] noise, so the subject core still
    overlaps - combined with the shared vendor company (different
    individual contacts, so this isn't just "same sender"), that's a real
    2-point candidate."""
    original = _issue(ws_db, "Quarterly Vendor Pricing Renewal Terms Review")
    _link_party(ws_db, original, "party_orig_contact", "sales@brightline-vendor.example",
                company="Brightline Vendor Group")

    forwarded = _issue(ws_db, "FW: Quarterly Vendor Pricing Renewal Terms Review")
    _link_party(ws_db, forwarded, "party_new_recipient", "ap@brightline-vendor.example",
                company="Brightline Vendor Group")

    candidates = p2.find_candidates(original)
    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] == forwarded
    assert "supplier" in candidates[0]["matched_signals"]
    assert "subject_entity" in candidates[0]["matched_signals"]

    _mock_claude(monkeypatch, "MATCH: 1\nVERDICT: same_project\n")
    result = p2.process_new_item(original)
    assert result["action"] == "merged"


def test_subject_line_change_across_a_thread_still_matches_on_reference_and_vendor(ws_db, isolated_paths, monkeypatch):
    """A thread's own subject can drift completely - a request that opens
    as "please confirm receipt of the PO" and closes as "signed - thank
    you!" shares no recognizable topic core at all. This must still be
    caught by whatever OTHER real data points survive the drift: the same
    vendor company (via two different individual contacts, so this isn't
    a same-sender shortcut) and the shared PO reference number. Confirms
    the 2+-point gate doesn't quietly depend on the title staying stable."""
    early = _issue(ws_db, "Please confirm receipt of the attached purchase order")
    _link_party(ws_db, early, "party_procurement_contact", "procurement@fenmark-supply.example",
                company="Fenmark Supply Co")
    early_rid = _raw_item(ws_db, early, "Please confirm receipt of the attached purchase order", "early-1",
                           body_preview="Please confirm receipt of PO reference below.")
    _classify_with_reference(ws_db, early_rid, "PO772400")

    closing = _issue(ws_db, "Signed - thank you, we're all set here!")
    _link_party(ws_db, closing, "party_signer_contact", "signer@fenmark-supply.example",
                company="Fenmark Supply Co")
    closing_rid = _raw_item(ws_db, closing, "Signed - thank you, we're all set here!", "closing-1",
                             body_preview="Countersigned copy attached, thanks for closing this out.")
    _classify_with_reference(ws_db, closing_rid, "PO772400")

    candidates = p2.find_candidates(early)
    assert len(candidates) == 1
    assert set(candidates[0]["matched_signals"]) >= {"supplier", "reference"}
    assert "subject_entity" not in candidates[0]["matched_signals"]

    _mock_claude(monkeypatch, "MATCH: 1\nVERDICT: same_project\n")
    result = p2.process_new_item(early)
    assert result["action"] == "merged"


# ===========================================================================
# 3. Attachment-driven identity
# ===========================================================================
# Real motivating case (workgraph_projects.reference_base_ids_for_issue's own
# 2026-08-06 fix): a signed Change Request PDF carries the only copy of a
# reference number that appears NOWHERE in any linked email's subject or
# body. full_text_for_work_object (this pipeline's own read for judge_
# candidate) and reference_base_ids_for_issue (the deterministic matching
# signal) both scan attachment extracted_text for exactly this reason.

def test_attachment_only_reference_plus_vendor_becomes_a_real_candidate(ws_db, isolated_paths, monkeypatch):
    """Two emails with different subjects and different individual senders
    at the same vendor - the kind of pair a plain subject/sender compare
    would never connect. What actually ties them together is a change-
    order reference number that exists ONLY inside each side's attached
    PDF, never in either email's own text. This is the positive case: the
    attachment-sourced reference plus the shared vendor company (2 points)
    is enough to surface it, without needing the two people or subjects to
    match at all."""
    a = _issue(ws_db, "Question about onboarding timeline")
    _link_party(ws_db, a, "party_a_contact", "alex@kinship-vendor.example", company="Kinship Vendor Solutions")
    _attach_reference_document(ws_db, a, "signed_change_order_a.pdf", "PR388213")

    b = _issue(ws_db, "Following up on invoice discrepancy")
    _link_party(ws_db, b, "party_b_contact", "bailey@kinship-vendor.example", company="Kinship Vendor Solutions")
    _attach_reference_document(ws_db, b, "signed_change_order_b.pdf", "PR388213")

    candidates = p2.find_candidates(a)
    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] == b
    assert set(candidates[0]["matched_signals"]) >= {"reference", "supplier"}

    _mock_claude(monkeypatch, "MATCH: 1\nVERDICT: same_project\n")
    result = p2.process_new_item(a)
    assert result["action"] == "merged"
    # judge_candidates must have actually been given the attachment text,
    # not just the email bodies - full_text_for_work_object's own real job.


def test_attachment_only_reference_with_nothing_else_shared_stays_below_the_gate(ws_db, isolated_paths):
    """Boundary/negative twin of the case above: a shared reference number
    is real evidence, but ONE point is still not a candidate on its own -
    same 2+-point discipline that already applies to a shared PR/PO number
    surfaced from body text (see workgraph_projects._matched_data_points'
    own docstring on the Authenticx case). This guards against a future
    change accidentally treating "reference" as sufficient by itself just
    because it happens to come from an attachment rather than a title."""
    a = _issue(ws_db, "Unrelated topic entirely - onboarding question")
    _link_party(ws_db, a, "party_a_only", "alex@vendor-one.example", company="Vendor One Inc")
    _attach_reference_document(ws_db, a, "co_a.pdf", "PR551900")

    b = _issue(ws_db, "A completely different unrelated matter")
    _link_party(ws_db, b, "party_b_only", "bailey@vendor-two.example", company="Vendor Two LLC")
    _attach_reference_document(ws_db, b, "co_b.pdf", "PR551900")

    assert p2.find_candidates(a) == []


# ===========================================================================
# 4. Chained candidate matching (A-B via one signal pair, B-C via another)
# ===========================================================================

def test_chained_matches_transitively_join_via_the_middle_item_not_a_direct_link(ws_db, isolated_paths, monkeypatch):
    """A and C share nothing directly - different vendor, different named
    contact, no common reference or amount. B, however, shares one signal
    pair with A (vendor company + a named contact) and a COMPLETELY
    different signal pair with C (a PO reference + a dollar amount). Once
    A and B are grouped into a project, C should still find its way into
    that same project via B - process_new_item's target-project lookup
    keys off the CANDIDATE's own already-established project, not a fresh
    requirement that the new item match every existing member directly.
    This is exactly the "does the pipeline correctly chain" question from
    the design doc's own tricky-case list - confirmed here to chain
    correctly, not a bug report."""
    a = _issue(ws_db, "Meridian Health Analytics access request")
    _link_party(ws_db, a, "party_amy", "amy.chen@meridian-health.example", company="Meridian Health Analytics")

    b = _issue(ws_db, "Meridian follow-up on the same access request")
    _link_party(ws_db, b, "party_amy", "amy.chen@meridian-health.example", company="Meridian Health Analytics")
    b_rid = _raw_item(ws_db, b, "PO confirmation attached", "chain-b-1",
                       body_preview="Total contract value: $250,000. See PO reference below.")
    _classify_with_reference(ws_db, b_rid, "PO419900")

    c = _issue(ws_db, "Separate procurement matter, different vendor on its face")
    c_rid = _raw_item(ws_db, c, "Separate procurement matter", "chain-c-1",
                       body_preview="Total contract value: $250,000. PO reference confirmed.")
    _classify_with_reference(ws_db, c_rid, "PO419900")

    # Confirm the premise first: A and C genuinely share nothing directly -
    # only B bridges them.
    assert p2.find_candidates(c) == [{"candidate_id": b, "matched_signals": ["reference", "amount"]}] or \
        {cand["candidate_id"] for cand in p2.find_candidates(c)} == {b}

    _mock_claude(monkeypatch, "MATCH: 1\nVERDICT: same_project\n")
    first = p2.process_new_item(a)
    assert first["action"] == "merged"
    project_id = first["project_id"]
    assert ws_db.get_issue(b)["project_id"] == project_id

    second = p2.process_new_item(c)
    assert second["action"] == "merged"
    assert second["project_id"] == project_id
    assert ws_db.get_issue(a)["project_id"] == ws_db.get_issue(c)["project_id"]


# ===========================================================================
# 5. False-merge recovery - the split safety valve, exercised through the
#    pipeline this task cares about, not just at the workgraph_projects
#    unit-test level (test_workgraph_projects.py already covers split_
#    issue_from_project's own detach/constraint mechanics directly).
# ===========================================================================

def test_split_issue_from_project_is_exercised_and_prevents_pipeline2_from_re_merging(ws_db, isolated_paths, monkeypatch):
    """A same_project verdict merges two issues that, on later human
    review, turn out to be wrong (a coincidental shared contact at a big
    vendor, not actually the same deal). Marc's own explicit ask when this
    grouping model was built: 'you'd need to be able to split them out
    again... but still.' This confirms the full loop: process_new_item
    merges -> a human calls split_issue_from_project to reverse it ->
    process_new_item, run again exactly as the next scheduled sweep would,
    does NOT drift the same two items back together - the cannot_link veto
    in _matched_data_points removes the sibling from find_candidates
    entirely, before judge_candidates (the LLM) is ever consulted again."""
    a = _issue(ws_db, "Renewal question for a large shared vendor account")
    _link_party(ws_db, a, "party_shared", "contact@omniforge-vendor.example", company="Omniforge Vendor Corp")
    b = _issue(ws_db, "Unrelated renewal question, same big vendor account")
    _link_party(ws_db, b, "party_shared", "contact@omniforge-vendor.example", company="Omniforge Vendor Corp")

    _mock_claude(monkeypatch, "MATCH: 1\nVERDICT: same_project\n")
    first = p2.process_new_item(a)
    assert first["action"] == "merged"
    old_project_id = first["project_id"]

    split_result = wp.split_issue_from_project(a, reason="confirmed wrong - coincidental shared contact only")
    assert split_result["action"] == "split"
    assert ws_db.get_issue(a)["project_id"] is None
    assert ws_db.get_issue(b)["project_id"] == old_project_id

    # The sibling must no longer even be a candidate - not "a candidate
    # that the LLM happens to reject again."
    assert p2.find_candidates(a) == []

    # Re-running the real pipeline entry point (what the next scheduled
    # sweep would do, since a's project_id is NULL again) must land a in a
    # brand-new project, never back in the old one - even though the mock
    # would still say "yes" if it were ever asked.
    second = p2.process_new_item(a)
    assert second["action"] == "new_project"
    assert second["project_id"] != old_project_id
    assert ws_db.get_issue(b)["project_id"] == old_project_id


# ===========================================================================
# 6. False-split boundary - what "no permanent veto" does NOT cover.
# ===========================================================================

def test_no_permanent_veto_only_protects_not_yet_grouped_items_not_already_grouped_ones(ws_db, isolated_paths, monkeypatch):
    """process_new_item's 'no permanent veto on a non-match' guarantee
    (an unrelated verdict never blocks a LATER re-match) only actually
    helps while the item still has project_id IS NULL. The moment a
    genuinely-unrelated verdict falls through to 'new_project', the item
    gets its OWN project_id - and process_new_item's very first check
    ('already_grouped' -> noop) means it will never be reconsidered by
    this function again, no matter how much new corroborating evidence
    shows up later (a shared reference surfacing on a later message, for
    instance). This isn't a bug - it's documented behavior
    (test_process_new_item_already_grouped_is_a_noop already covers the
    mechanism in isolation) - but it IS a real, easy-to-miss boundary: a
    genuine false split, once an item has fallen through to its own
    project, can only be repaired by an explicit external action (a human
    or curator-level merge_issues call, or a dedicated reconciliation
    sweep), never by process_new_item simply being run again."""
    a = _issue(ws_db, "Renewal notice", category="contract")
    _link_party(ws_db, a, "party_x", "rep@driftwood-vendor.example", company="Driftwood Vendor Inc")
    b = _issue(ws_db, "Renewal notice v2", category="contract")
    _link_party(ws_db, b, "party_x", "rep@driftwood-vendor.example", company="Driftwood Vendor Inc")
    _mock_claude(monkeypatch, "MATCH: NONE\n")

    first = p2.process_new_item(a)
    assert first["action"] == "new_project"
    a_project_id = ws_db.get_issue(a)["project_id"]

    # New, strong corroborating evidence arrives after the fact - e.g. a
    # later attachment proving these really are the same deal. Even with
    # a mock that would now say "yes," re-running process_new_item on the
    # ALREADY-fallen-through item cannot pick this up.
    _mock_claude(monkeypatch, "MATCH: 1\nVERDICT: same_project\n")
    second = p2.process_new_item(a)

    assert second == {"work_object_id": a, "action": "already_grouped", "project_id": a_project_id}
    assert ws_db.get_issue(a)["project_id"] != ws_db.get_issue(b)["project_id"]
    # The real repair path for this shape is a direct merge_issues call
    # (curator/human-level), which is unaffected by process_new_item's own
    # already_grouped short-circuit:
    repair = wp.merge_issues(a, b, reason_label="manual repair: confirmed same deal after all")
    assert repair["status"] == "merged"
    assert ws_db.get_issue(a)["project_id"] == ws_db.get_issue(b)["project_id"]


# ===========================================================================
# 7. Ambiguous/uncertain outcome vs. a confident pick among several
#    candidates, one of which is a related_different_project shape.
# ===========================================================================

def test_uncertain_outcome_among_several_plausible_candidates_writes_no_relationship_signal(ws_db, isolated_paths, monkeypatch):
    """Rewritten 2026-08-11 for the comparative-judgment redesign (review
    point #8): a genuinely ambiguous read is now the model itself saying
    "uncertain" over the whole candidate set in one call, not two
    independent same_project verdicts on different existing projects
    colliding after the fact. With three real candidates in play - two
    already-established, DIFFERENT projects that could each plausibly be
    the match, and a third, wholly separate related-but-different-project
    subcontractor candidate - an "uncertain" read means NO candidate was
    chosen at all, so unlike the old design, no relationship bookkeeping
    fires either: the two mechanisms (merge/park decision, relationship
    write) are no longer independent per call, since both now depend on
    the SAME single chosen candidate."""
    a = _issue(ws_db, "Requested approval for Cascade Robotics services")
    _link_party(ws_db, a, "shared_party", "rep@cascade-robotics.example", company="Cascade Robotics")
    b = _issue(ws_db, "REVIEW REQUESTED: Cascade Robotics services quote")
    _link_party(ws_db, b, "shared_party", "rep@cascade-robotics.example", company="Cascade Robotics")
    project_x = ws_db.create_project_with_new_id(name="Project X", category="other")
    ws_db.assign_issue_to_project(b, project_x)

    c = _issue(ws_db, "Please review Cascade Robotics services terms")
    _link_party(ws_db, c, "shared_party", "rep@cascade-robotics.example", company="Cascade Robotics")
    project_y = ws_db.create_project_with_new_id(name="Project Y", category="other")
    ws_db.assign_issue_to_project(c, project_y)

    d = _issue(ws_db, "Northgate Fabrication subcontract under Cascade Robotics")
    _link_party(ws_db, d, "shared_party", "rep@cascade-robotics.example", company="Cascade Robotics")
    _raw_item(ws_db, d, "Northgate Fabrication subcontract under Cascade Robotics", "d-1",
              body_preview="Northgate Fabrication is a separate subcontractor engagement.")
    project_z = ws_db.create_project_with_new_id(name="Project Z (subcontractor)", category="other")
    ws_db.assign_issue_to_project(d, project_z)

    _mock_claude(monkeypatch, "MATCH: UNCERTAIN\n")

    result = p2.process_new_item(a)

    assert result["action"] == "ambiguous"
    assert ws_db.get_issue(a)["project_id"] is None
    assert ws_db.list_work_object_relationships_by_type("rejected") == []


def test_comparative_call_can_choose_the_subcontractor_candidate_and_write_its_relationship(ws_db, isolated_paths, monkeypatch):
    """Same three-candidate shape as the uncertain case above, but here the
    model confidently chooses the subcontractor candidate as the best
    match (e.g. because the other two are themselves too ambiguous to
    even mention) - the relationship write fires for exactly that chosen
    candidate, and the two other, non-chosen candidates' projects are
    left untouched."""
    a = _issue(ws_db, "Requested approval for Cascade Robotics services")
    _link_party(ws_db, a, "shared_party", "rep@cascade-robotics.example", company="Cascade Robotics")
    b = _issue(ws_db, "REVIEW REQUESTED: Cascade Robotics services quote")
    _link_party(ws_db, b, "shared_party", "rep@cascade-robotics.example", company="Cascade Robotics")
    project_x = ws_db.create_project_with_new_id(name="Project X", category="other")
    ws_db.assign_issue_to_project(b, project_x)

    c = _issue(ws_db, "Please review Cascade Robotics services terms")
    _link_party(ws_db, c, "shared_party", "rep@cascade-robotics.example", company="Cascade Robotics")
    project_y = ws_db.create_project_with_new_id(name="Project Y", category="other")
    ws_db.assign_issue_to_project(c, project_y)

    d = _issue(ws_db, "Northgate Fabrication subcontract under Cascade Robotics")
    _link_party(ws_db, d, "shared_party", "rep@cascade-robotics.example", company="Cascade Robotics")
    _raw_item(ws_db, d, "Northgate Fabrication subcontract under Cascade Robotics", "d-1",
              body_preview="Northgate Fabrication is a separate subcontractor engagement.")
    project_z = ws_db.create_project_with_new_id(name="Project Z (subcontractor)", category="other")
    ws_db.assign_issue_to_project(d, project_z)

    _mock_claude_choosing_marker(monkeypatch, "Northgate Fabrication", "related_different_project")

    result = p2.process_new_item(a)

    assert result["action"] == "new_project"
    relationships = ws_db.list_work_object_relationships_by_type("rejected")
    assert len(relationships) == 1
    assert {relationships[0]["from_id"], relationships[0]["to_id"]} == {a, d}
    assert ws_db.get_issue(b)["project_id"] == project_x
    assert ws_db.get_issue(c)["project_id"] == project_y


# ===========================================================================
# 8. System-generated sender false-positive guard.
# ===========================================================================

def test_automated_sender_identity_is_excluded_even_when_it_would_otherwise_supply_2_points(ws_db, isolated_paths):
    """Marc's spec point 5: is_automated_sender already excludes a system's
    own notification address from every party/company signal, so 2+ OTHER
    real points are required before two automated notifications become a
    candidate. This constructs the case that would otherwise trivially
    give 2 points for free (same automated sender party linked to both,
    same "company") to confirm the exclusion is actually load-bearing here,
    not just theoretical - two unrelated automated notices about
    DIFFERENT real requisitions must not become a candidate purely because
    they were routed through the same notification address."""
    a = _issue(ws_db, "Ariba requisition notice - Item A")
    _link_party(ws_db, a, "party_notify", "notifications@acme-procurement.example",
                company="Acme Procurement System")

    b = _issue(ws_db, "Ariba requisition notice - Item B")
    _link_party(ws_db, b, "party_notify", "notifications@acme-procurement.example",
                company="Acme Procurement System")

    assert p2.find_candidates(a) == []


# ===========================================================================
# 9. Real bug found while building this corpus - fixed (task #334, 2026-08-11).
# ===========================================================================

def test_all_candidates_timing_out_should_not_write_a_false_rejected_precedent(ws_db, isolated_paths, monkeypatch):
    """Real bug found while building this corpus, fixed as task #334:
    process_new_item's final fallback used to be

        if judged:
            workgraph_lessons.record_confirmed_or_rejected(issue_id_a=work_object_id, status="rejected")

    which fired whenever `judged` was non-empty - but `judged` collects
    EVERY (candidate, verdict) pair regardless of what judge_candidates
    actually returned, including None (a timeout or an unparseable reply -
    see judge_candidates' own docstring: 'the caller treats it exactly like
    unrelated'). That equivalence is fine for the immediate grouping
    decision (no match either way), but it was NOT fine for Total Recall:
    a None verdict means the LLM was never successfully consulted at all,
    yet this wrote a genuine 'rejected' lesson for this item's category+
    company situation_key exactly as if a real, deliberate 'unrelated'
    read had happened. That false negative precedent then flowed into every
    FUTURE judge_candidates call for the same situation via _precedent_
    context_line's 'previously turned out NOT to match' framing - a
    self-inflicted bias seeded by nothing more than a subprocess timeout.

    Fixed: process_new_item now only records "rejected" when at least one
    verdict in `judged` is a real "unrelated" read - matching test_process_
    new_item_no_candidates_never_touches_lessons's existing guarantee for
    the zero-candidates case, extended to the all-None case too."""
    a = _issue(ws_db, "Requested approval for a services renewal", category="contract")
    _link_party(ws_db, a, "shared_party", "rep@driftglass-vendor.example", company="Driftglass Vendor Inc")
    b = _issue(ws_db, "REVIEW REQUESTED: services renewal quote", category="contract")
    _link_party(ws_db, b, "shared_party", "rep@driftglass-vendor.example", company="Driftglass Vendor Inc")

    def fake_popen_always_times_out(*a_, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen_always_times_out)

    result = p2.process_new_item(a)

    assert result["action"] == "new_project"  # the grouping decision itself is fine
    lesson = ws_db.get_lesson_by_situation("category:contract|company:driftglass vendor inc", "rejected")
    # NOTE: workgraph_lessons.situation_key lowercases company; matching its
    # own normalization here rather than guessing.
    if lesson is None:
        lesson = ws_db.get_lesson_by_situation(
            workgraph_lessons.situation_key("contract", "Driftglass Vendor Inc"), "rejected")
    assert lesson is None, (
        "process_new_item recorded a 'rejected' Total Recall precedent from a "
        "candidate set where every judge_candidates call timed out (verdict "
        "None) - no real LLM judgment ever happened, so no precedent should "
        "have been written. See this test's own docstring."
    )
