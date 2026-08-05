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

    def communicate(self, timeout=None):
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


def test_find_candidates_never_creates_a_suggestion_row(ws_db):
    """The whole point of retiring the old queue - detection alone must
    leave pending_project_suggestions untouched."""
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")

    p2.find_candidates(a)

    assert ws_db.list_project_suggestions(status="pending") == []


# --- judge_candidate / _parse_verdict (step 4) ----------------------------

def test_parse_verdict_yes():
    assert p2._parse_verdict("some preamble\nVERDICT: yes\n") is True


def test_parse_verdict_no():
    assert p2._parse_verdict("VERDICT: no") is False


def test_parse_verdict_none_when_unparseable():
    assert p2._parse_verdict("I could not determine this.") is None
    assert p2._parse_verdict("") is None


def test_judge_candidate_reads_both_sides_full_text(ws_db, isolated_paths, monkeypatch):
    a = _issue(ws_db, "Deal A")
    _raw_item(ws_db, a, "Subject A", "ka", body_preview="Full text of item A")
    b = _issue(ws_db, "Deal B")
    _raw_item(ws_db, b, "Subject B", "kb", body_preview="Full text of item B")

    captured = {}

    def fake_popen(args, **kw):
        captured["prompt"] = args[2]  # ["claude", "-p", prompt, ...]
        return _FakeProcess("VERDICT: yes\n")

    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)

    verdict = p2.judge_candidate(a, b, ["supplier", "stakeholder"])

    assert verdict is True
    assert "Full text of item A" in captured["prompt"] or "Full text of item B" in captured["prompt"]


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
    _mock_claude(monkeypatch, "VERDICT: yes\n")

    result = p2.process_new_item(a)

    assert result["action"] == "merged"
    assert ws_db.get_issue(a)["project_id"] == ws_db.get_issue(b)["project_id"]
    # No pending suggestion, no identity_constraint - a clean immediate merge.
    assert ws_db.list_project_suggestions(status="pending") == []


def test_process_new_item_no_permanent_veto_on_no(ws_db, isolated_paths, monkeypatch):
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    _mock_claude(monkeypatch, "VERDICT: no\n")

    result = p2.process_new_item(a)

    assert result["action"] == "new_project"
    assert ws_db.get_issue(a)["project_id"] != ws_db.get_issue(b)["project_id"]
    # Critically: no cannot_merge/cannot_link constraint recorded - a "no"
    # today must not permanently block re-evaluation later.
    assert ws_db.list_identity_constraints_for_subject(a) == []


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


def test_process_new_item_tries_next_candidate_after_a_no(ws_db, isolated_paths, monkeypatch):
    """Two real 2+-point candidates for the same item - a "no" on the
    first must not stop the pipeline from trying the second."""
    a = _issue(ws_db, "Requested approval for Veeva CRM press release")
    _link_party(ws_db, a, "shared_party", "rep@acme.com", company="Acme")
    b = _issue(ws_db, "MARC REVIEW REQUESTED: Veeva CRM press release quote")
    _link_party(ws_db, b, "shared_party", "rep@acme.com", company="Acme")
    c = _issue(ws_db, "Please review Veeva CRM press release terms")
    _link_party(ws_db, c, "shared_party", "rep@acme.com", company="Acme")

    calls = {"n": 0}

    def fake_popen(*a_, **kw):
        calls["n"] += 1
        # First judged candidate says no, second says yes - then a 3rd
        # call is step 6's own post-merge extraction, which also reads
        # this same fake stdout (a harmless "no" line it just ignores,
        # since it isn't a VERDICT: line at all).
        return _FakeProcess("VERDICT: no\n" if calls["n"] == 1 else "VERDICT: yes\n")

    monkeypatch.setattr(p2.subprocess, "Popen", fake_popen)

    result = p2.process_new_item(a)

    assert result["action"] == "merged"
    assert calls["n"] == 3  # 2 judgment calls (no, then yes) + 1 post-merge extraction call


# --- run_project_extraction (step 6) --------------------------------------

def test_run_project_extraction_creates_new_issues_from_full_text(ws_db, isolated_paths, monkeypatch):
    project_id = ws_db.create_project_with_new_id(name="Test Project", category="other")
    a = _issue(ws_db, "Kickoff note")
    _raw_item(ws_db, a, "Kickoff", "ka", body_preview="We need a signed SOW by Friday.")
    ws_db.assign_issue_to_project(a, project_id)

    _mock_claude(
        monkeypatch,
        "ITEM: Get SOW signed | STATUS: active | NOTE: mentioned directly in the text\n"
        "SUMMARY: Waiting on a signed SOW.\n",
    )

    result = p2.run_project_extraction(project_id)

    assert result["action"] == "extracted"
    assert len(result["created_issue_ids"]) == 1
    created = ws_db.get_issue(result["created_issue_ids"][0])
    assert created["title"] == "Get SOW signed"
    assert created["project_id"] == project_id
    assert result["summary"] == "Waiting on a signed SOW."


def test_run_project_extraction_skips_already_tracked_titles(ws_db, isolated_paths, monkeypatch):
    project_id = ws_db.create_project_with_new_id(name="Test Project", category="other")
    existing = _issue(ws_db, "Get SOW signed")
    ws_db.assign_issue_to_project(existing, project_id)

    _mock_claude(monkeypatch, "ITEM: Get SOW signed | STATUS: active | NOTE: same as before\n")

    result = p2.run_project_extraction(project_id)

    assert result["created_issue_ids"] == []


def test_run_project_extraction_not_found(ws_db, isolated_paths):
    assert p2.run_project_extraction("proj-does-not-exist")["action"] == "not_found"
