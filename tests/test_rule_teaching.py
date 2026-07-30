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
    monkeypatch.setattr(rule_extraction, "extract_rule_draft", lambda explanation: None)

    result = rt.teach_from_chat("#addrule a signature request needs an approved PO first", "marc")

    assert "couldn't confidently structure" in result["reply"]
    assert "walk through" in result["reply"]
    suggestion = ws_db.get_prerequisite_suggestion(result["suggestion_id"])
    assert suggestion["origin"] == "taught_via_chat"
    assert suggestion["raw_explanation"] == "a signature request needs an approved PO first"
    assert suggestion["trigger_signal_type"] is None
    assert suggestion["status"] == "pending"
    assert suggestion["clarify_stage"] == "offered"


def test_teach_from_chat_strips_mention_and_marker_from_stored_explanation(ws_db, monkeypatch):
    import rule_extraction
    monkeypatch.setattr(rule_extraction, "extract_rule_candidate", lambda explanation: None)
    monkeypatch.setattr(rule_extraction, "extract_rule_draft", lambda explanation: None)

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
    monkeypatch.setattr(rule_extraction, "extract_rule_draft", boom)

    result = rt.teach_from_chat("#addrule something", "marc")

    suggestion = ws_db.get_prerequisite_suggestion(result["suggestion_id"])
    assert suggestion["raw_explanation"] == "something"
    assert suggestion["trigger_signal_type"] is None
    assert suggestion["clarify_stage"] == "offered"


def test_teach_from_chat_partial_draft_prefills_known_fields_and_offers_clarification(ws_db, monkeypatch):
    import rule_extraction
    monkeypatch.setattr(rule_extraction, "extract_rule_candidate", lambda explanation: None)
    monkeypatch.setattr(rule_extraction, "extract_rule_draft", lambda explanation: {
        "trigger_signal_type": "signature_requested_docusign",
        "requires_signal_type": None, "match_on": None, "reflection": None,
    })

    result = rt.teach_from_chat("#addrule something about docusign", "marc")

    suggestion = ws_db.get_prerequisite_suggestion(result["suggestion_id"])
    assert suggestion["trigger_signal_type"] == "signature_requested_docusign"
    assert suggestion["requires_signal_type"] is None
    assert suggestion["clarify_stage"] == "offered"
    assert "walk through" in result["reply"]


def test_teach_from_chat_fully_structured_low_confidence_draft_skips_clarification(ws_db, monkeypatch):
    """The model can flag confidence='low' yet still return a complete,
    valid structure - there's nothing left to clarify, so this gets the
    same review-and-confirm treatment as a confident extraction, not a
    pointless clarification offer."""
    import rule_extraction
    monkeypatch.setattr(rule_extraction, "extract_rule_candidate", lambda explanation: None)
    monkeypatch.setattr(rule_extraction, "extract_rule_draft", lambda explanation: {
        "trigger_signal_type": "signature_requested_docusign",
        "requires_signal_type": "ariba_pr_fully_approved",
        "match_on": "project", "reflection": "a low-confidence guess",
    })

    result = rt.teach_from_chat("#addrule something", "marc")

    assert "confirm" in result["reply"].lower()
    assert "low confidence" in result["reply"].lower()
    suggestion = ws_db.get_prerequisite_suggestion(result["suggestion_id"])
    assert suggestion["trigger_signal_type"] == "signature_requested_docusign"
    assert suggestion["clarify_stage"] is None


def test_try_continue_clarification_none_when_nothing_active(ws_db):
    assert rt.try_continue_clarification("yes", "marc") is None


def test_try_continue_clarification_decline_at_offer_clears_stage_keeps_pending(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
        match_on=None, reason=None, evidence=None, raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "offered")

    result = rt.try_continue_clarification("no thanks", "marc")

    assert "Settings" in result["reply"]
    suggestion = ws_db.get_prerequisite_suggestion(sid)
    assert suggestion["clarify_stage"] is None
    assert suggestion["status"] == "pending"


def test_try_continue_clarification_unrecognized_reply_at_offer_reasks(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
        match_on=None, reason=None, evidence=None, raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "offered")

    result = rt.try_continue_clarification("what do you mean", "marc")

    assert "yes" in result["reply"].lower()
    assert ws_db.get_prerequisite_suggestion(sid)["clarify_stage"] == "offered"


def test_try_continue_clarification_full_walkthrough_from_scratch(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
        match_on=None, reason=None, evidence=None, raw_explanation="a needs b", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "offered")

    r1 = rt.try_continue_clarification("yes", "marc")
    assert ws_db.get_prerequisite_suggestion(sid)["clarify_stage"] == "ask_trigger"
    assert "signature_requested_docusign" in r1["reply"]  # numbered option list

    r2 = rt.try_continue_clarification("signature_requested_docusign", "marc")
    assert ws_db.get_prerequisite_suggestion(sid)["clarify_stage"] == "ask_requires"
    assert ws_db.get_prerequisite_suggestion(sid)["trigger_signal_type"] == "signature_requested_docusign"

    r3 = rt.try_continue_clarification("ariba_pr_fully_approved", "marc")
    assert ws_db.get_prerequisite_suggestion(sid)["clarify_stage"] == "ask_match_on"
    assert ws_db.get_prerequisite_suggestion(sid)["requires_signal_type"] == "ariba_pr_fully_approved"

    r4 = rt.try_continue_clarification("project", "marc")
    assert "confirm" in r4["reply"].lower()
    suggestion = ws_db.get_prerequisite_suggestion(sid)
    assert suggestion["clarify_stage"] is None
    assert suggestion["match_on"] == "project"
    assert suggestion["reason"] == (
        "signature_requested_docusign needs ariba_pr_fully_approved first, matched by project")


def test_try_continue_clarification_walkthrough_by_number(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
        match_on=None, reason=None, evidence=None, raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "ask_trigger")

    result = rt.try_continue_clarification("9", "marc")  # signature_requested is index 9 in known list

    suggestion = ws_db.get_prerequisite_suggestion(sid)
    assert suggestion["trigger_signal_type"] == "signature_requested"
    assert suggestion["clarify_stage"] == "ask_requires"
    assert "project" in result["reply"].lower() or "supplier" in result["reply"].lower() \
        or "prerequisite" in result["reply"].lower()


def test_try_continue_clarification_skips_already_prefilled_fields(ws_db):
    """A partial low-confidence draft can already know the trigger - the
    walkthrough should only ask about what's actually missing."""
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type="signature_requested_docusign",
        requires_signal_type=None, match_on=None, reason=None, evidence=None,
        raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "offered")

    result = rt.try_continue_clarification("yes", "marc")

    assert ws_db.get_prerequisite_suggestion(sid)["clarify_stage"] == "ask_requires"
    assert "prerequisite" in result["reply"].lower()


def test_try_continue_clarification_rejects_requires_matching_trigger(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type="signature_requested_docusign",
        requires_signal_type=None, match_on=None, reason=None, evidence=None,
        raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "ask_requires")

    result = rt.try_continue_clarification("signature_requested_docusign", "marc")

    assert "different" in result["reply"].lower()
    suggestion = ws_db.get_prerequisite_suggestion(sid)
    assert suggestion["requires_signal_type"] is None
    assert suggestion["clarify_stage"] == "ask_requires"


def test_try_continue_clarification_invalid_signal_type_reasks_same_stage(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
        match_on=None, reason=None, evidence=None, raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "ask_trigger")

    result = rt.try_continue_clarification("something totally made up", "marc")

    assert "didn't catch" in result["reply"].lower()
    suggestion = ws_db.get_prerequisite_suggestion(sid)
    assert suggestion["trigger_signal_type"] is None
    assert suggestion["clarify_stage"] == "ask_trigger"


def test_try_continue_clarification_ambiguous_substring_reasks(ws_db):
    """"ariba" matches several known types - ambiguous, must not silently
    pick one."""
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
        match_on=None, reason=None, evidence=None, raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "ask_trigger")

    result = rt.try_continue_clarification("ariba", "marc")

    assert ws_db.get_prerequisite_suggestion(sid)["trigger_signal_type"] is None
    assert "didn't catch" in result["reply"].lower()


def test_try_continue_clarification_unambiguous_substring_matches(ws_db):
    """"docusign" alone is ambiguous (matches both signature_requested_
    docusign and signature_completed_docusign) - "requested_docusign" is
    not."""
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
        match_on=None, reason=None, evidence=None, raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "ask_trigger")

    result = rt.try_continue_clarification("requested_docusign", "marc")

    assert ws_db.get_prerequisite_suggestion(sid)["trigger_signal_type"] == "signature_requested_docusign"


def test_try_continue_clarification_invalid_match_on_reasks(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved", match_on=None, reason=None,
        evidence=None, raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "ask_match_on")

    result = rt.try_continue_clarification("neither", "marc")

    assert "project" in result["reply"].lower() and "supplier" in result["reply"].lower()
    assert ws_db.get_prerequisite_suggestion(sid)["match_on"] is None


def test_try_continue_clarification_cancel_mid_conversation_rejects_suggestion(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type="signature_requested_docusign",
        requires_signal_type=None, match_on=None, reason=None, evidence=None,
        raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "ask_requires")

    result = rt.try_continue_clarification("cancel", "marc")

    assert "cancelled" in result["reply"].lower()
    suggestion = ws_db.get_prerequisite_suggestion(sid)
    assert suggestion["status"] == "rejected"
    assert suggestion["clarify_stage"] is None


def test_try_continue_clarification_respects_recency_window(ws_db):
    old_ts = time.time() - rt.CONFIRMATION_WINDOW_SECONDS - 60
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
        match_on=None, reason=None, evidence=None, raw_explanation="x", proposed_by="marc",
    )
    ws_db.set_suggestion_clarify_stage(sid, "offered")
    conn = ws_db._connect()
    conn.execute("UPDATE pending_prerequisite_suggestions SET created_ts = ?", (old_ts,))

    assert rt.try_continue_clarification("yes", "marc") is None


def test_try_continue_clarification_ignores_other_askers(ws_db):
    sid = ws_db.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
        match_on=None, reason=None, evidence=None, raw_explanation="x", proposed_by="someone_else",
    )
    ws_db.set_suggestion_clarify_stage(sid, "offered")

    assert rt.try_continue_clarification("yes", "marc") is None


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
