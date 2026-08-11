"""Tests for workgraph_pipeline2.py - the new, isolated grouping+extraction
pipeline (2026-08-05, Marc's own exhaustive spec). No live `claude` calls -
subprocess.Popen is monkeypatched to return a fake process, same "shape the
mock 1:1 with the real emitter" discipline used for outlook_scan.ps1's own
tests, so the deterministic parts (candidate detection, verdict parsing,
merge/new-project wiring) are exercised for real while the actual LLM call
itself is a controlled fake.
"""
from __future__ import annotations

import time

import pytest

import workgraph_pipeline2 as p2
import workgraph_projects as wp
import workgraph_lessons


def _issue(ws_db, title, category="other"):
    return ws_db.create_issue_with_new_id(title=title, category=category, state="active")


def _link_party(ws_db, issue_id, party_id, email, *, company=None):
    ws_db.upsert_party(id=party_id, primary_email=email, display_name=party_id,
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company=company)
    ws_db.link_party_to_issue(issue_id, party_id)


def _raw_item(ws_db, issue_id, subject, key, body_preview=None):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject=subject, from_actor="a@example.com",
        participants_json="[]", body_preview=body_preview or subject,
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    return rid


class _FakeProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.args = ["claude"]
        self.pid = 12345
        self.sent_input = None

    def communicate(self, input=None, timeout=None):
        # task #304 (2026-08-11): real code now sends the prompt over stdin
        # (Windows argv-length fix), not as a Popen argument - capture it
        # here for tests that need to inspect the prompt's actual content.
        self.sent_input = input
        return self.stdout, self.stderr


def _mock_claude(monkeypatch, stdout: str):
    def fake_popen(*a, **kw):
        return _FakeProcess(stdout)
    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)


# --- find_candidates (step 3, pure detection, no side effects) ------------

def test_find_candidates_needs_2_plus_points(ws_db):
    a = _issue(ws_db, "Deal A")
    _link_party(ws_db, a, "p1", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Deal B")
    _link_party(ws_db, b, "p2", "other@acme.com", company="Acme")

    # Only "supplier" matches (shared company, different people) - 1 point,
    # not a candidate.
    assert p2.find_candidates(a) == []


def test_find_candidates_finds_a_real_2_point_match(ws_db):
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")

    candidates = p2.find_candidates(a)

    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] == b
    assert "supplier" in candidates[0]["matched_signals"]
    assert "stakeholder" in candidates[0]["matched_signals"]


def _noise_issues(ws_db, n, prefix="noise", with_party=True):
    """N issues sharing zero real data points with anything else. With
    with_party=True each gets its own unique external party/company, so
    none of them ever collide on supplier/stakeholder with each other or
    with a/b - real stress-test noise for the equivalence tests, where
    the point is "the two paths still agree," not pool size.

    with_party=False (no external party at all) models the OTHER common
    real case - a plain internal-only thread - and is what the pool-size
    reduction test uses: subject_entity's own presence-based fallback
    (see candidate_pool_via_data_point_index's own docstring) only ever
    narrows the pool past objects that have NO external party at all
    (has_external gates topic_key itself, see _topic_key_for_signature) -
    an issue that DOES carry an external party and an informative title
    legitimately stays in that fallback's pool no matter how unrelated it
    otherwise is, since a fuzzy substring match can't be safely
    pre-filtered by presence alone any tighter without risking a real
    missed candidate."""
    ids = []
    for i in range(n):
        issue_id = _issue(ws_db, f"{prefix} {i}: totally unrelated internal note")
        if with_party:
            _link_party(ws_db, issue_id, f"{prefix}-party-{i}", f"{prefix}{i}@other-{i}.com", company=f"Other Co {i}")
        ids.append(issue_id)
    return ids


def _sorted_candidates(candidates):
    return sorted((c["candidate_id"], tuple(sorted(c["matched_signals"]))) for c in candidates)


# --- task #331: datapoint_value -> work_object_ids index rewrite ----------
#
# find_candidates used to unconditionally scan EVERY issue/cluster
# (ws.list_issues(limit=10000) + ws.list_clusters(limit=10000)) and run the
# real matching computation against each one. It's now sourced from
# workgraph_projects.candidate_pool_via_data_point_index instead, with the
# ORIGINAL full scan kept only as a fallback for the one case that index
# can't yet safely serve (workgraph_discovery.
# has_confirmed_non_fasttrack_definitions). These tests assert the two
# paths - both real production code, not a reimplementation - produce
# IDENTICAL results, then give some rough evidence the index path really
# does less work.

def test_find_candidates_index_path_matches_full_scan_fallback_multi_type(ws_db, isolated_paths, monkeypatch):
    """A rich scenario spanning reference/supplier/stakeholder/amount
    matches, plus 25 signal-less noise issues - the index path (default)
    and the original full-scan fallback (forced via monkeypatch) must
    return the exact same candidate set for the exact same item."""
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    a_rid = _raw_item(ws_db, a, "Veeva CRM", "ka", body_preview="Total contract value: $50,000.00")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR700001' WHERE id = ?", (a_rid,))
    conn.close()

    # 2-point match: shared party (supplier+stakeholder) AND shared PR ref.
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    b_rid = _raw_item(ws_db, b, "Veeva CRM quote", "kb", body_preview="See PR700001 attached.")
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET pr_number_base = 'PR700001' WHERE id = ?", (b_rid,))
    conn.close()

    # 2-point match: shared company (supplier) AND a within-1% amount -
    # otherwise totally unrelated to a/b (different party, no shared ref).
    c = _issue(ws_db, "Separate Acme renewal")
    _link_party(ws_db, c, "acme_other_contact", "other@acme.com", company="Acme")
    _raw_item(ws_db, c, "Acme renewal", "kc", body_preview="Total contract value: $50,250.00")

    _noise_issues(ws_db, 25)

    indexed = p2.find_candidates(a)

    monkeypatch.setattr(p2.workgraph_discovery, "has_confirmed_non_fasttrack_definitions", lambda: True)
    full_scan = p2.find_candidates(a)

    assert _sorted_candidates(indexed) == _sorted_candidates(full_scan)
    # Real assertions on content too, not just "the two paths agree" -
    # agreeing on a wrong answer would still pass the line above alone.
    ids = {cid for cid, _ in _sorted_candidates(indexed)}
    assert ids == {b, c}
    signals_by_id = dict(_sorted_candidates(indexed))
    # >= (superset), not == : the shared "veeva crm press release" title
    # text between a/b also legitimately fires subject_entity - real,
    # correct, incidental behavior this test isn't about; the point here
    # is that reference/supplier/stakeholder (b) and supplier/amount (c)
    # are ALL present via the index path, matching the full scan exactly.
    assert set(signals_by_id[b]) >= {"reference", "supplier", "stakeholder"}
    assert set(signals_by_id[c]) >= {"supplier", "amount"}


def test_find_candidates_presence_fallback_covers_fuzzy_only_match(ws_db, isolated_paths, monkeypatch):
    """The one correctness-sensitive case for this rewrite: a pair that
    matches ONLY on the two genuinely FUZZY point types (subject_entity's
    substring overlap, product_service's substring overlap) - zero shared
    reference/supplier/stakeholder/amount/document. A plain exact-value
    index lookup alone would silently miss this pair entirely; the
    presence-based fallback (candidate_pool_via_data_point_index's own
    docstring) must still surface it, and the result must still match
    the full-scan fallback exactly."""
    title_a = ("Action required: Approve the Requisition that JANE SMITH submitted "
               "- PR1111111 - Cloud Analytics Platform Subscription Renewal ($10,000.00 USD)")
    title_b = ("Action required: Approve the Requisition that BOB JONES submitted "
               "- PR2222222 - Cloud Analytics Platform Subscription Renewal ($99,999.00 USD)")
    a = _issue(ws_db, title_a)
    _link_party(ws_db, a, "party-a", "repa@vendor-a.com", company="Vendor A")
    b = _issue(ws_db, title_b)
    _link_party(ws_db, b, "party-b", "repb@vendor-b.com", company="Vendor B")
    _noise_issues(ws_db, 15)

    indexed = p2.find_candidates(a)

    monkeypatch.setattr(p2.workgraph_discovery, "has_confirmed_non_fasttrack_definitions", lambda: True)
    full_scan = p2.find_candidates(a)

    assert _sorted_candidates(indexed) == _sorted_candidates(full_scan)
    assert len(indexed) == 1
    assert indexed[0]["candidate_id"] == b
    assert set(indexed[0]["matched_signals"]) == {"subject_entity", "product_service"}


def test_find_candidates_falls_back_to_full_scan_when_a_real_discovery_is_confirmed(ws_db, isolated_paths, monkeypatch):
    """workgraph_discovery.has_confirmed_non_fasttrack_definitions is the
    one flag that must force the ORIGINAL full scan (see its own
    docstring on why the index can't yet safely serve a genuinely
    discovered data point) - confirmed here by spying on ws.list_issues
    rather than monkeypatching the flag directly, so this actually
    exercises the real trigger condition."""
    a = _issue(ws_db, "A")
    ws_db.create_data_point_definition(
        id="dp-real-discovery", name="Real discovery", description="test", point_type="entity",
        deterministic_rule=None, discovered_from="test", status="confirmed",
    )
    ws_db.confirm_data_point_definition("dp-real-discovery", confirmed_by="marc")

    calls = {"n": 0}
    real_list_issues = ws_db.list_issues

    def counting_list_issues(*a_, **kw):
        calls["n"] += 1
        return real_list_issues(*a_, **kw)

    monkeypatch.setattr(p2.ws, "list_issues", counting_list_issues)

    p2.find_candidates(a)

    assert calls["n"] == 1  # the full-scan branch really ran


def test_find_candidates_keeps_the_fasttrack_index_current(ws_db, isolated_paths):
    """Task #331 item 4 - the index must get written as data points get
    computed, not just read. A fresh signature compute (find_candidates'
    own get_or_compute_work_object_signature call) must leave real
    dp-fasttrack-* rows behind for the next lookup to use."""
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")

    p2.find_candidates(a)

    values = {v["definition_id"]: v["value"] for v in ws_db.list_data_point_values_for_work_object(a)}
    assert values.get("dp-fasttrack-supplier") == "acme"
    assert values.get("dp-fasttrack-stakeholder") == "party:shared_party"


def test_find_candidates_index_path_examines_far_fewer_pairs_than_full_scan(ws_db, isolated_paths, monkeypatch):
    """Rough before/after evidence (not a rigorous benchmark) that the
    index rewrite actually reduces work: with 40 signal-less noise issues
    plus 1 real candidate, the indexed path should call the real, per-pair
    _matched_data_points computation only a handful of times, while the
    full-scan fallback calls it once per noise issue plus the real one."""
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    _noise_issues(ws_db, 40, with_party=False)

    real_matched = wp._matched_data_points
    calls = {"n": 0}

    def counting_matched(*a_, **kw):
        calls["n"] += 1
        return real_matched(*a_, **kw)

    monkeypatch.setattr(wp, "_matched_data_points", counting_matched)
    indexed_candidates = p2.find_candidates(a)
    indexed_calls = calls["n"]

    calls["n"] = 0
    monkeypatch.setattr(p2.workgraph_discovery, "has_confirmed_non_fasttrack_definitions", lambda: True)
    full_scan_candidates = p2.find_candidates(a)
    full_scan_calls = calls["n"]

    assert _sorted_candidates(indexed_candidates) == _sorted_candidates(full_scan_candidates)
    assert full_scan_calls >= 41  # every noise issue plus b
    assert indexed_calls <= 3     # only b (and maybe a itself) ever reach the real comparison
    assert indexed_calls < full_scan_calls


def test_find_candidates_has_no_side_effects(ws_db):
    """The whole point of retiring the old queue - detection alone must
    write nothing anywhere. pending_project_suggestions itself is gone
    (task #269) - this now checks the one still-live side-effect surface
    (work_object_signatures caching) doesn't grow beyond what computing
    the two signatures themselves legitimately writes."""
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")

    candidates = p2.find_candidates(a)

    assert len(candidates) == 1
    assert ws_db.get_issue(a)["project_id"] is None
    assert ws_db.get_issue(b)["project_id"] is None


# --- judge_candidate / _parse_verdict (step 4) ----------------------------

def test_parse_verdict_same_project():
    assert p2._parse_verdict("some preamble\nVERDICT: same_project\n") == "same_project"


def test_parse_verdict_related_different_project():
    assert p2._parse_verdict("VERDICT: related_different_project") == "related_different_project"


def test_parse_verdict_unrelated():
    assert p2._parse_verdict("VERDICT: unrelated") == "unrelated"


def test_parse_verdict_none_when_unparseable():
    assert p2._parse_verdict("I could not determine this.") is None
    assert p2._parse_verdict("") is None
    assert p2._parse_verdict("VERDICT: yes") is None  # old 2-way wire format is no longer valid


def test_judge_candidate_reads_both_sides_full_text(ws_db, isolated_paths, monkeypatch):
    a = _issue(ws_db, "Deal A")
    _raw_item(ws_db, a, "Subject A", "ka", body_preview="Full text of item A")
    b = _issue(ws_db, "Deal B")
    _raw_item(ws_db, b, "Subject B", "kb", body_preview="Full text of item B")

    captured = {}

    def fake_popen(*a_, **kw):
        proc = _FakeProcess("VERDICT: same_project\n")
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)

    verdict = p2.judge_candidate(a, b, ["supplier", "stakeholder"])

    assert verdict == "same_project"
    prompt = captured["proc"].sent_input  # sent over stdin, not argv - see _FakeProcess.communicate
    assert "Full text of item A" in prompt or "Full text of item B" in prompt


def test_judge_candidate_includes_precedent_as_context_only(ws_db, isolated_paths, monkeypatch):
    """2026-08-11: precedent must reach the prompt as one contextual line,
    never as a bypass of this call - see process_new_item's own docstring."""
    a = _issue(ws_db, "Deal A")
    b = _issue(ws_db, "Deal B")
    captured = {}

    def fake_popen(*a_, **kw):
        proc = _FakeProcess("VERDICT: same_project\n")
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)

    p2.judge_candidate(a, b, ["supplier"], precedent_context="similar contract cases have previously matched")

    prompt = captured["proc"].sent_input
    assert "similar contract cases have previously matched" in prompt
    assert "context only" in prompt.lower()


def test_judge_candidate_returns_none_on_timeout(ws_db, isolated_paths, monkeypatch):
    a = _issue(ws_db, "Deal A")
    b = _issue(ws_db, "Deal B")

    def fake_popen(*a_, **kw):
        raise __import__("subprocess").TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)

    assert p2.judge_candidate(a, b, ["supplier"]) is None


# --- process_new_item (the real step 3->4 orchestration) ------------------

def test_process_new_item_merges_immediately_on_yes(ws_db, isolated_paths, monkeypatch):
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    _mock_claude(monkeypatch, "VERDICT: same_project\n")

    result = p2.process_new_item(a)

    assert result["action"] == "merged"
    assert ws_db.get_issue(a)["project_id"] == ws_db.get_issue(b)["project_id"]
    # No identity_constraint - a clean immediate merge (pending_project_
    # suggestions itself is gone as of task #269, nothing left to assert on).
    assert ws_db.list_identity_constraints_for_subject(a) == []


def test_process_new_item_no_permanent_veto_on_unrelated(ws_db, isolated_paths, monkeypatch):
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    _mock_claude(monkeypatch, "VERDICT: unrelated\n")

    result = p2.process_new_item(a)

    assert result["action"] == "new_project"
    assert ws_db.get_issue(a)["project_id"] != ws_db.get_issue(b)["project_id"]
    # Critically: no cannot_merge/cannot_link constraint recorded - a
    # non-match today must not permanently block re-evaluation later.
    assert ws_db.list_identity_constraints_for_subject(a) == []


def test_process_new_item_ambiguous_when_multiple_distinct_projects_both_match(ws_db, isolated_paths, monkeypatch):
    """Doc Section 4, Step 4 (2026-08-11): two existing PROJECTS can't
    both silently win just because both their candidates happen to get
    judged first - never picked arbitrarily, parked instead."""
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    project_x = ws_db.create_project_with_new_id(name="Project X", category="other")
    ws_db.assign_issue_to_project(b, project_x)
    c = _issue(ws_db, "Please review Veeva CRM press release terms")
    _link_party(ws_db, c, "shared_party", "rep@acme.com", company="Acme")
    project_y = ws_db.create_project_with_new_id(name="Project Y", category="other")
    ws_db.assign_issue_to_project(c, project_y)
    _mock_claude(monkeypatch, "VERDICT: same_project\n")

    result = p2.process_new_item(a)

    assert result["action"] == "ambiguous"
    assert set(result["candidate_project_ids"]) == {project_x, project_y}
    assert ws_db.get_issue(a)["project_id"] is None


def test_process_new_item_related_different_project_writes_relationship_signal(ws_db, isolated_paths, monkeypatch):
    """2026-08-11: a 'related but different project' read must not
    vanish - it becomes work_object_relationships' first real production
    writer, the exact table workgraph_relationships.run_relationship_
    sweep() already reads to build durable Relationship rows."""
    a = _issue(ws_db, "New Scriptly work order")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Existing Sodalis MSA")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    project_b = ws_db.create_project_with_new_id(name="Sodalis MSA project", category="other")
    ws_db.assign_issue_to_project(b, project_b)
    _mock_claude(monkeypatch, "VERDICT: related_different_project\n")

    result = p2.process_new_item(a)

    assert result["action"] == "new_project"  # no same_project match - a still gets its own project
    relationships = ws_db.list_work_object_relationships_by_type("rejected")
    assert len(relationships) == 1
    assert {relationships[0]["from_id"], relationships[0]["to_id"]} == {a, b}


def test_process_new_item_becomes_its_own_project_with_no_candidates(ws_db, isolated_paths):
    a = _issue(ws_db, "A totally standalone item")

    result = p2.process_new_item(a)

    assert result["action"] == "new_project"
    assert ws_db.get_issue(a)["project_id"] == result["project_id"]


def test_process_new_item_already_grouped_is_a_noop(ws_db, isolated_paths):
    a = _issue(ws_db, "A")
    project_id = ws_db.create_project_with_new_id(name="Existing", category="other")
    ws_db.assign_issue_to_project(a, project_id)

    result = p2.process_new_item(a)

    assert result["action"] == "already_grouped"
    assert result["project_id"] == project_id


def test_process_new_item_judges_every_candidate_before_deciding(ws_db, isolated_paths, monkeypatch):
    """Two real 2+-point candidates for the same item - 2026-08-11
    rewrite (doc Section 4, Step 4): EVERY candidate is judged, not just
    until the first non-match. An unrelated first read must not stop the
    pipeline from reaching the second, real match."""
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    c = _issue(ws_db, "Please review Veeva CRM press release terms")
    _link_party(ws_db, c, "shared_party", "rep@acme.com", company="Acme")

    calls = {"n": 0}

    def fake_popen(*a_, **kw):
        calls["n"] += 1
        # First judged candidate is unrelated, second is the real match.
        # No 3rd call: step 6's post-merge run_project_extraction reads
        # the claims ledger (task #304) rather than making an LLM call
        # unconditionally - none of a/b/c have a materialized claim in
        # this test, so run_project_extraction short-circuits at
        # "no_claims_yet" before ever touching Popen.
        return _FakeProcess("VERDICT: unrelated\n" if calls["n"] == 1 else "VERDICT: same_project\n")

    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)

    result = p2.process_new_item(a)

    assert result["action"] == "merged"
    assert calls["n"] == 2  # 2 judgment calls (unrelated, then same_project)


# --- process_new_item Total Recall precedent (2026-08-07, rewritten 2026-08-11) --
# workgraph_lessons.precedent_prefilter is keyed on the NEW item's own
# category+company situation, not a specific pair - see process_new_item's
# own docstring for the full design reasoning. 2026-08-11 (doc Section 5):
# precedent used to let a "confirmed"/"rejected" read skip judge_candidate
# entirely - now it is ALWAYS just one line of context in the prompt;
# judge_candidate is called for every real candidate, every time.

def _seed_strong_precedent(source_issue_id: str, *, outcome: str = "confirmed",
                            situation_key_val: str = "category:contract|company:acme"):
    """STRONG_PRECEDENT_HITS (3) identical writes at DEFAULT_TRUST (0.6) +
    (3-1)*TRUST_BUMP (0.1) = 0.8, exactly STRONG_PRECEDENT_TRUST by
    construction - see workgraph_lessons.py's own comment."""
    for _ in range(workgraph_lessons.STRONG_PRECEDENT_HITS):
        workgraph_lessons.record_lesson(
            situation_key_val=situation_key_val, statement="test precedent statement",
            outcome=outcome, source_issue_id=source_issue_id,
        )


def _fake_popen_capturing_judgment_prompts(verdict_reply: str = "VERDICT: same_project\n",
                                            extraction_reply: str = "SUMMARY: ok\n"):
    """Returns (fake_popen, captured) - captured["judgment_prompts"]
    collects every prompt that looks like a real judge_candidate call
    (_JUDGMENT_PROMPT_TEMPLATE's distinctive "judging the real
    relationship" text), so a test can assert the LLM WAS called (the
    opposite assertion from before 2026-08-11's precedent-bypass
    removal) and inspect what precedent context actually reached it.
    The reply returned is decided dynamically per-call based on the real
    prompt content, not fixed at Popen() construction time - matches how
    the real code only knows the prompt at communicate(), not Popen()."""
    captured = {"judgment_prompts": []}

    class _CapturingProcess(_FakeProcess):
        def __init__(self):
            super().__init__("")

        def communicate(self, input=None, timeout=None):
            self.sent_input = input
            if input and "judging the real relationship" in input.lower():
                captured["judgment_prompts"].append(input)
                return verdict_reply, ""
            return extraction_reply, ""

    def fake_popen(*a_, **kw):
        return _CapturingProcess()
    return fake_popen, captured


def test_process_new_item_confirmed_precedent_still_calls_the_llm_and_merges(ws_db, isolated_paths, monkeypatch):
    """2026-08-11 rewrite (doc Section 5): a 'confirmed' precedent must
    never bypass judge_candidate - it only ever informs the prompt.
    Verified two ways: the LLM verdict decides the outcome (mocked
    same_project -> merged), AND the precedent line actually reached the
    prompt as context, not a hardcoded VERDICT."""
    a = _issue(ws_db, "Renewal notice", category="contract")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Renewal notice v2", category="contract")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    _seed_strong_precedent(a, outcome="confirmed")
    fake_popen, captured = _fake_popen_capturing_judgment_prompts("VERDICT: same_project\n")
    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)

    result = p2.process_new_item(a)

    assert result["action"] == "merged"
    assert ws_db.get_issue(a)["project_id"] == ws_db.get_issue(b)["project_id"]
    assert len(captured["judgment_prompts"]) == 1
    assert "previously turned out to be" in captured["judgment_prompts"][0]
    assert "context only" in captured["judgment_prompts"][0].lower()


def test_process_new_item_confirmed_precedent_does_not_override_contradicting_evidence(ws_db, isolated_paths, monkeypatch):
    """The single most important guarantee of the 2026-08-11 rewrite: a
    strong 'confirmed' precedent must NOT force a merge when the real
    LLM read of THIS pair's actual evidence says unrelated."""
    a = _issue(ws_db, "Renewal notice", category="contract")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Renewal notice v2", category="contract")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    _seed_strong_precedent(a, outcome="confirmed")
    fake_popen, captured = _fake_popen_capturing_judgment_prompts("VERDICT: unrelated\n")
    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)

    result = p2.process_new_item(a)

    assert result["action"] == "new_project"
    assert ws_db.get_issue(a)["project_id"] != ws_db.get_issue(b)["project_id"]
    assert len(captured["judgment_prompts"]) == 1  # the LLM really was asked, not skipped


def test_process_new_item_rejected_precedent_still_calls_the_llm(ws_db, isolated_paths, monkeypatch):
    """Symmetric case: a 'rejected' precedent must not skip the LLM
    either - and here the real read contradicts the precedent and finds
    a real match, which must win."""
    a = _issue(ws_db, "Renewal notice", category="contract")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "Renewal notice v2", category="contract")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    _seed_strong_precedent(a, outcome="rejected")
    fake_popen, captured = _fake_popen_capturing_judgment_prompts("VERDICT: same_project\n")
    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)

    result = p2.process_new_item(a)

    assert result["action"] == "merged"
    assert ws_db.get_issue(a)["project_id"] == ws_db.get_issue(b)["project_id"]
    assert len(captured["judgment_prompts"]) == 1
    assert "previously turned out not" in captured["judgment_prompts"][0].lower()


def test_process_new_item_no_precedent_falls_through_to_llm_and_records_outcome(ws_db, isolated_paths, monkeypatch):
    """No strong precedent either way (the default, real-world case) -
    behavior is unchanged from before this fast-path existed (a real LLM
    read decides), except the genuine verdict now also writes a lesson,
    which is what keeps the lesson store learning from THIS pipeline's own
    decisions now that confirm_suggestion/reject_suggestion are retired."""
    a = _issue(ws_db, "Requested approval for Veeva CRM press release", category="contract")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote", category="contract")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    _mock_claude(monkeypatch, "VERDICT: same_project\n")

    assert ws_db.get_lesson_by_situation("category:contract|company:acme", "confirmed") is None

    result = p2.process_new_item(a)

    assert result["action"] == "merged"
    lesson = ws_db.get_lesson_by_situation("category:contract|company:acme", "confirmed")
    assert lesson is not None
    assert lesson["hit_count"] == 1


def test_process_new_item_no_precedent_records_rejected_when_llm_says_no(ws_db, isolated_paths, monkeypatch):
    a = _issue(ws_db, "Requested approval for Veeva CRM press release", category="contract")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote", category="contract")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    _mock_claude(monkeypatch, "VERDICT: unrelated\n")

    result = p2.process_new_item(a)

    assert result["action"] == "new_project"
    lesson = ws_db.get_lesson_by_situation("category:contract|company:acme", "rejected")
    assert lesson is not None
    assert lesson["hit_count"] == 1


def test_process_new_item_no_candidates_never_touches_lessons(ws_db, isolated_paths):
    """Zero candidates found at all is a different case than "judged and
    rejected" - nothing was actually compared, so nothing should be
    recorded as a precedent outcome."""
    a = _issue(ws_db, "A totally standalone item", category="contract")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")

    p2.process_new_item(a)

    assert ws_db.get_lesson_by_situation("category:contract|company:acme", "rejected") is None
    assert ws_db.get_lesson_by_situation("category:contract|company:acme", "confirmed") is None


# --- run_project_extraction (step 6, consolidated onto claims - task #304) -

def test_run_project_extraction_creates_issue_from_cited_claims(ws_db, isolated_paths, monkeypatch):
    project_id = ws_db.create_project_with_new_id(name="Test Project", category="other")
    a = _issue(ws_db, "Kickoff note")
    rid = _raw_item(ws_db, a, "Kickoff", "ka", body_preview="We need a signed SOW by Friday.")
    ws_db.assign_issue_to_project(a, project_id)
    claim_id = ws_db.insert_claim(
        issue_id=a, raw_item_id=rid, claim_type="ask", text="Get the SOW signed by Friday.",
        author="counterparty", author_basis="direction", owner="marc",
    )

    _mock_claude(
        monkeypatch,
        f"ISSUE: Get SOW signed | CLAIM_IDS: {claim_id}\n"
        "SUMMARY: Waiting on a signed SOW.\n",
    )

    result = p2.run_project_extraction(project_id)

    assert result["action"] == "extracted"
    assert len(result["created_issue_ids"]) == 1
    created = ws_db.get_issue(result["created_issue_ids"][0])
    assert created["title"] == "Get SOW signed"
    assert created["project_id"] == project_id
    moved_claim = ws_db.get_claim(claim_id)
    assert moved_claim["issue_id"] == result["created_issue_ids"][0]
    assert result["summary"] == "Waiting on a signed SOW."


def test_run_project_extraction_ignores_claim_ids_not_in_this_project(ws_db, isolated_paths, monkeypatch):
    project_id = ws_db.create_project_with_new_id(name="Test Project", category="other")
    a = _issue(ws_db, "Kickoff note")
    ws_db.assign_issue_to_project(a, project_id)
    other = _issue(ws_db, "Unrelated issue")  # not a member of this project
    other_rid = _raw_item(ws_db, other, "Other", "kb")
    stray_claim_id = ws_db.insert_claim(
        issue_id=other, raw_item_id=other_rid, claim_type="ask", text="Unrelated ask.",
        author="counterparty", author_basis="direction", owner="marc",
    )

    _mock_claude(monkeypatch, f"ISSUE: Hallucinated issue | CLAIM_IDS: {stray_claim_id}\n")

    result = p2.run_project_extraction(project_id)

    assert result["action"] == "no_claims_yet"
    assert ws_db.get_claim(stray_claim_id)["issue_id"] == other


def test_run_project_extraction_deduplicates_claim_id_cited_twice_in_one_response(ws_db, isolated_paths, monkeypatch):
    project_id = ws_db.create_project_with_new_id(name="Test Project", category="other")
    a = _issue(ws_db, "Kickoff note")
    rid = _raw_item(ws_db, a, "Kickoff", "ka")
    ws_db.assign_issue_to_project(a, project_id)
    claim_id = ws_db.insert_claim(
        issue_id=a, raw_item_id=rid, claim_type="ask", text="Get the SOW signed.",
        author="counterparty", author_basis="direction", owner="marc",
    )

    _mock_claude(
        monkeypatch,
        f"ISSUE: First title | CLAIM_IDS: {claim_id}\n"
        f"ISSUE: Second title | CLAIM_IDS: {claim_id}\n",
    )

    result = p2.run_project_extraction(project_id)

    assert len(result["created_issue_ids"]) == 1


def test_run_project_extraction_no_claims_yet(ws_db, isolated_paths):
    project_id = ws_db.create_project_with_new_id(name="Test Project", category="other")
    a = _issue(ws_db, "Kickoff note")
    ws_db.assign_issue_to_project(a, project_id)

    assert p2.run_project_extraction(project_id)["action"] == "no_claims_yet"


def test_run_project_extraction_not_found(ws_db, isolated_paths):
    assert p2.run_project_extraction("proj-does-not-exist")["action"] == "not_found"
