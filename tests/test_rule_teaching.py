"""Regression tests for rule_teaching.py (task #54) - the #addrule chat
capture and confirm/reject resolution flow. rule_extraction.extract_rule_
candidate is monkeypatched directly (its own HTTP-mocking is covered in
test_rule_extraction.py) so these tests focus on rule_teaching's own logic:
message parsing, always-capture-raw-text-first, and the confirm/reject
resolution against the shared suggestion queue."""
from __future__ import annotations

import time

import rule_teaching as rt


def test_strip_leading_mention_removes_at_name():
    assert rt.strip_leading_mention("@tia #addrule hello") == "#addrule hello"


def test_strip_leading_mention_noop_without_mention():
    assert rt.strip_leading_mention("#addrule hello") == "#addrule hello"


def test_is_addrule_message_true_with_and_without_mention():
    assert rt.is_addrule_message("#addrule a needs b") is True
    assert rt.is_addrule_message("@tia #addrule a needs b") is True
    assert rt.is_addrule_message("  #ADDRULE a needs b") is True


def test_is_addrule_message_false_for_normal_question():
    assert rt.is_addrule_message("what's the status of the Acme renewal") is False
    assert rt.is_addrule_message("@tia can you check on this") is False


def test_teach_from_chat_always_creates_a_suggestion_even_without_extraction(ws_db, monkeypatch):
    import rule_extraction
    monkeypatch.setattr(rule_extraction, "extract_rule_candidate", lambda explanation: None)

    result = rt.teach_from_chat("#addrule a signature request needs an approved PO first", "marc")

    assert "couldn't confidently match" in result["reply"]
    suggestion = ws_db.get_prerequisite_suggestion(result["suggestion_id"])
    assert suggestion["origin"] == "taught_via_chat"
    assert suggestion["raw_explanation"] == "a signature request needs an approved PO first"
    assert suggestion["trigger_signal_type"] is None
    assert suggestion["status"] == "pending"


def test_teach_from_chat_strips_mention_and_marker_from_stored_explanation(ws_db, monkeypatch):
    import rule_extraction
    monkeypatch.setattr(rule_extraction, "extract_rule_candidate", lambda explanation: None)

    result = rt.teach_from_chat("@tia #addrule the real explanation text", "marc")

    suggestion = ws_db.get_prerequisite_suggestion(result["suggestion_id"])
    assert suggestion["raw_explanation"] == "the real explanation text"


def test_teach_from_chat_populates_structured_fields_when_confident(ws_db, monkeypatch):
    import rule_extraction
    monkeypatch.setattr(rule_extraction, "extract_rule_candidate", lambda explanation: {
        "trigger_signal_type": "signature_requested_docusign",
        "requires_signal_type": "ariba_pr_fully_approved",
        "match_on": "project",
        "reflection": "A DocuSign request needs an approved Ariba PO first, matched by project.",
    })

    result = rt.teach_from_chat("#addrule signature requires PO approval", "marc")

    assert "confirm" in result["reply"].lower()
    suggestion = ws_db.get_prerequisite_suggestion(result["suggestion_id"])
    assert suggestion["trigger_signal_type"] == "signature_requested_docusign"
    assert suggestion["requires_signal_type"] == "ariba_pr_fully_approved"
    assert suggestion["match_on"] == "project"


def test_teach_from_chat_extraction_exception_still_captures_raw_text(ws_db, monkeypatch):
    """An extraction bug must never lose the deterministic capture."""
    import rule_extraction

    def boom(explanation):
        raise RuntimeError("simulated extraction crash")

    monkeypatch.setattr(rule_extraction, "extract_rule_candidate", boom)

    result = rt.teach_from_chat("#addrule something", "marc")

    suggestion = ws_db.get_prerequisite_suggestion(result["suggestion_id"])
    assert suggestion["raw_explanation"] == "something"
    assert suggestion["trigger_signal_type"] is None


def test_try_resolve_pending_confirmation_none_for_non_answer(ws_db):
    assert rt.try_resolve_pending_confirmation("what's the status of Acme", "marc") is None


def test_try_resolve_pending_confirmation_none_when_nothing_pending(ws_db):
    assert rt.try_resolve_pending_confirmation("confirm", "marc") is None


def test_try_resolve_pending_confirmation_confirms_and_creates_real_rule(ws_db):
    ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved", match_on="project",
        reason="a real reason", evidence=None, raw_explanation="x", proposed_by="marc",
    )

    result = rt.try_resolve_pending_confirmation("confirm", "marc")

    assert result == {"reply": "Done — that's a real rule now."}
    rules = ws_db.list_prerequisite_rules()
    assert len(rules) == 1
    assert rules[0]["trigger_signal_type"] == "signature_requested_docusign"
    pending = ws_db.list_prerequisite_suggestions("pending")
    assert pending == []


def test_try_resolve_pending_confirmation_rejects_without_creating_a_rule(ws_db):
    ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved", match_on="project",
        reason="x", evidence=None, raw_explanation="x", proposed_by="marc",
    )

    result = rt.try_resolve_pending_confirmation("no thanks", "marc")

    assert result == {"reply": "Discarded."}
    assert ws_db.list_prerequisite_rules() == []


def test_try_resolve_pending_confirmation_refuses_unstructured_confirm(ws_db):
    ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
        match_on=None, reason=None, evidence=None, raw_explanation="vague thing", proposed_by="marc",
    )

    result = rt.try_resolve_pending_confirmation("confirm", "marc")

    assert "isn't structured enough" in result["reply"]
    assert ws_db.list_prerequisite_rules() == []
    # still pending - an unstructured confirm attempt doesn't resolve it
    assert len(ws_db.list_prerequisite_suggestions("pending")) == 1


def test_try_resolve_pending_confirmation_respects_recency_window(ws_db, monkeypatch):
    old_ts = time.time() - rt.CONFIRMATION_WINDOW_SECONDS - 60
    ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved", match_on="project",
        reason="x", evidence=None, raw_explanation="x", proposed_by="marc",
    )
    conn = ws_db._connect()
    conn.execute("UPDATE pending_prerequisite_suggestions SET created_ts = ?", (old_ts,))

    assert rt.try_resolve_pending_confirmation("confirm", "marc") is None


def test_try_resolve_pending_confirmation_ignores_other_askers(ws_db):
    ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved", match_on="project",
        reason="x", evidence=None, raw_explanation="x", proposed_by="someone_else",
    )
    assert rt.try_resolve_pending_confirmation("confirm", "marc") is None
