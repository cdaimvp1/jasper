"""
rule_teaching.py — task #54: "#addrule" chat-based prerequisite-rule
teaching in Back Office. Wired into /api/socrates/ask (server_lean.py)
BEFORE workgraph_socrates.answer() - that function stays exactly as it was,
100% zero-LLM; this module is the new, clearly separate capability sitting
next to it, not folded into it.

Three entry points, checked by server_lean.py in this order:
  teach_from_chat(explanation, asker) - ALWAYS logs the verbatim explanation
    as a pending_prerequisite_suggestions row (that part never fails, no
    matter what the LLM does - a try/except around the extraction call
    guarantees the raw capture can't be lost to an extraction bug), then
    attempts rule_extraction.py's local-LLM structuring. Confident+valid ->
    the row is created WITH the structured fields already filled in, and the
    reply reflects them back. Not confident -> task #62's clarification
    conversation is offered instead of just pointing at Settings.
  try_continue_clarification(text, asker) - task #62. Advances an ACTIVE
    clarification conversation (offer -> ask_trigger -> ask_requires ->
    ask_match_on -> done) one turn at a time, filling in the same
    trigger_signal_type/requires_signal_type/match_on columns a confident
    extraction would have filled in directly. Must be checked BEFORE
    try_resolve_pending_confirmation below, since a bare "yes" mid-
    conversation means "yes, let's walk through it," not "confirm the
    rule." Returns None when nothing is active for this asker, so the
    caller falls through normally.
  try_resolve_pending_confirmation(text, asker) - checks whether `text`
    reads like a short yes/no answer, and if so resolves the asker's most
    recent still-pending taught_via_chat suggestion, within a recency
    window (not an open-ended search back through all history). Returns
    None - not a confirmation at all - when there's nothing to resolve, so
    the caller falls through to normal Socrates handling (a bare "yes" with
    nothing pending is a legitimately unanswerable question, not a bug).
    By the time a suggestion reaches this function its clarify_stage is
    always NULL again - either it was never in a conversation, or task
    #62's flow already finished and cleared it - so there's no overlap
    with try_continue_clarification above.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import rule_extraction
import workgraph_signals
import workgraph_store as ws

# Matches the cohort's own ~30-minute heartbeat convention (CLAUDE.md/the F9
# poller) - not load-bearing here, just a sensible, consistent recency floor
# so confirming something from days ago by accident isn't possible.
CONFIRMATION_WINDOW_SECONDS = 1800

_CONFIRM_RE = re.compile(r"^\s*(confirm|yes|y)\b", re.IGNORECASE)
_REJECT_RE = re.compile(r"^\s*(reject|no|n)\b", re.IGNORECASE)

ADDRULE_RE = re.compile(r"(?i)^#addrule\b\s*")

# Task #62 clarification-conversation replies. Separate from _CONFIRM_RE/
# _REJECT_RE above - "yes" during the "want to walk through it?" offer means
# something different from "yes" confirming an already-fully-structured
# rule, even though both start a conversation with the same word.
_CLARIFY_YES_RE = re.compile(r"^\s*(yes|y|sure|ok(ay)?|let'?s(\s+do\s+it)?|start)\b", re.IGNORECASE)
_CLARIFY_DECLINE_RE = re.compile(r"^\s*(no|n|nah|not\s+now|skip)\b", re.IGNORECASE)
_CLARIFY_CANCEL_RE = re.compile(r"^\s*(cancel|stop|nevermind|never\s+mind|forget\s+it)\b", re.IGNORECASE)


def strip_leading_mention(text: str) -> str:
    """Drops a leading "@Name " token if present - the frontend may send the
    full original message (mention included) even when it also wants this
    module to see the #addrule statement underneath it."""
    return re.sub(r"^@\S+\s*", "", text or "")


def is_addrule_message(text: str) -> bool:
    return bool(ADDRULE_RE.match(strip_leading_mention(text or "").strip()))


def teach_from_chat(text: str, asker: str) -> dict:
    """`text` is the raw message (may still carry a leading @mention - this
    strips it and the #addrule marker itself before treating the remainder
    as the explanation). Returns {"reply": str, "suggestion_id": int}."""
    stripped = strip_leading_mention(text or "").strip()
    explanation = ADDRULE_RE.sub("", stripped).strip()

    candidate = None
    if explanation:
        try:
            candidate = rule_extraction.extract_rule_candidate(explanation)
        except Exception:
            candidate = None  # an extraction bug must never lose the raw capture below

    if candidate:
        suggestion_id = ws.create_prerequisite_suggestion(
            origin="taught_via_chat",
            trigger_signal_type=candidate["trigger_signal_type"],
            requires_signal_type=candidate["requires_signal_type"],
            match_on=candidate["match_on"], reason=candidate["reflection"],
            evidence=None, raw_explanation=explanation, proposed_by=asker,
        )
        reply = (f'{candidate["reflection"]} Reply **confirm** to make this a real rule, '
                 f'or **reject** to discard it — or leave it, you can finish it in '
                 f'Settings › Prerequisite rules › Suggested.')
        return {"reply": reply, "suggestion_id": suggestion_id}

    # Not confident enough to auto-accept. Task #62: try a best-effort DRAFT
    # (unlike extract_rule_candidate, this doesn't require confidence=='high'
    # and returns whatever partial structure the model managed - or None if
    # Ollama itself was unreachable, which degrades to the same "ask all
    # three from scratch" case below).
    draft = None
    if explanation:
        try:
            draft = rule_extraction.extract_rule_draft(explanation)
        except Exception:
            draft = None  # same discipline as the candidate call above
    trigger = draft.get("trigger_signal_type") if draft else None
    requires = draft.get("requires_signal_type") if draft else None
    match_on = draft.get("match_on") if draft else None
    reflection = draft.get("reflection") if draft else None

    if trigger and requires and match_on:
        # The model produced a complete structure despite flagging low
        # confidence - there's nothing left to actually clarify, so this
        # gets the exact same review-and-confirm treatment as the
        # high-confidence path above, just honestly labeled as a guess.
        reason = reflection or f"{trigger} needs {requires} first, matched by {match_on}"
        suggestion_id = ws.create_prerequisite_suggestion(
            origin="taught_via_chat", trigger_signal_type=trigger, requires_signal_type=requires,
            match_on=match_on, reason=reason, evidence=None, raw_explanation=explanation, proposed_by=asker,
        )
        reply = (f'{reason} (best guess — low confidence). Reply **confirm** to make this a real '
                 f'rule, or **reject** to discard it — or leave it, you can finish it in '
                 f'Settings › Prerequisite rules › Suggested.')
        return {"reply": reply, "suggestion_id": suggestion_id}

    suggestion_id = ws.create_prerequisite_suggestion(
        origin="taught_via_chat", trigger_signal_type=trigger, requires_signal_type=requires,
        match_on=match_on, reason=None, evidence=None, raw_explanation=explanation, proposed_by=asker,
    )
    ws.set_suggestion_clarify_stage(suggestion_id, "offered")
    reply = ("I couldn't confidently structure that on my own. Want to walk through a couple of "
             "quick questions so I can set it up correctly? Reply **yes** to start, or **no** to "
             "leave it in Settings › Prerequisite rules › Suggested for now.")
    return {"reply": reply, "suggestion_id": suggestion_id}


# --- task #62: conversational clarification --------------------------------

def _signal_type_options() -> str:
    return "\n".join(f"{i + 1}. {t}" for i, t in enumerate(workgraph_signals.known_signal_types()))


def _match_signal_type(answer: str, known_types: list[str]) -> Optional[str]:
    """Accepts a 1-based list number, an exact case-insensitive name, or an
    unambiguous case-insensitive substring (so "docusign" resolves cleanly
    when exactly one known type contains it). Returns None on no match OR
    an ambiguous multi-match - a wrong guess is worse than asking again."""
    answer = (answer or "").strip()
    if not answer:
        return None
    if answer.isdigit():
        idx = int(answer) - 1
        return known_types[idx] if 0 <= idx < len(known_types) else None
    lowered = answer.lower()
    for t in known_types:
        if t.lower() == lowered:
            return t
    matches = [t for t in known_types if lowered in t.lower()]
    return matches[0] if len(matches) == 1 else None


def _match_on_answer(answer: str) -> Optional[str]:
    lowered = (answer or "").strip().lower()
    if lowered in ("1", "project", "p"):
        return "project"
    if lowered in ("2", "supplier", "s"):
        return "supplier"
    return None


def _ask_for_stage(stage: str) -> str:
    if stage == "ask_trigger":
        return ("What's the signal that shows up and should be checked against this rule (the "
                "trigger)? For example, a signature request. Reply with a number, or type the "
                "name:\n" + _signal_type_options())
    if stage == "ask_requires":
        return ("And what needs to have already happened FIRST, before that trigger should be "
                "treated as ready (the prerequisite)? Reply with a number, or type the name:\n"
                + _signal_type_options())
    return ("Last thing — should I match these up by **project** (same deal/thread) or by "
            "**supplier** (same external company, even across different deals)? Reply "
            "**project** or **supplier**.")


def _next_clarify_stage(suggestion: dict) -> Optional[str]:
    """Which field is still missing, in ask order - or None once all three
    are filled, meaning the conversation is done."""
    if not suggestion.get("trigger_signal_type"):
        return "ask_trigger"
    if not suggestion.get("requires_signal_type"):
        return "ask_requires"
    if not suggestion.get("match_on"):
        return "ask_match_on"
    return None


def try_continue_clarification(text: str, asker: str) -> Optional[dict]:
    """Advances (or starts, or cancels) an active task #62 clarification
    conversation by one turn. Returns {"reply": str} if `text` was consumed
    as part of that conversation, or None if there's no active conversation
    for this asker - the caller falls through to normal handling (including
    try_resolve_pending_confirmation below) in that case. Must be checked
    BEFORE try_resolve_pending_confirmation: a bare "yes" while the "want to
    walk through it?" offer is pending means something different from a
    "yes" confirming an unrelated already-structured suggestion."""
    stripped = strip_leading_mention(text or "").strip()
    if not asker or not stripped:
        return None

    since_ts = time.time() - CONFIRMATION_WINDOW_SECONDS
    suggestion = ws.get_most_recent_clarifying_suggestion_by_asker(asker, since_ts)
    if suggestion is None:
        return None

    if _CLARIFY_CANCEL_RE.match(stripped):
        ws.set_suggestion_clarify_stage(suggestion["id"], None)
        ws.resolve_prerequisite_suggestion(suggestion["id"], "rejected")
        return {"reply": "Cancelled — discarded."}

    stage = suggestion["clarify_stage"]

    if stage == "offered":
        if _CLARIFY_DECLINE_RE.match(stripped):
            ws.set_suggestion_clarify_stage(suggestion["id"], None)
            return {"reply": "No problem — it's saved in Settings › Prerequisite rules › "
                              "Suggested whenever you want to finish it."}
        if not _CLARIFY_YES_RE.match(stripped):
            return {"reply": "Reply **yes** to walk through it together, or **no** to leave it "
                              "in Settings for now."}
        next_stage = _next_clarify_stage(suggestion)
        if next_stage is None:
            # Shouldn't normally happen - an "offered" row always has at
            # least one field missing, that's WHY it was offered - but stay
            # honest rather than asking a pointless question if it ever is.
            ws.set_suggestion_clarify_stage(suggestion["id"], None)
            return {"reply": "Actually, that one's already fully structured. Reply **confirm** "
                              "to make it a real rule, or **reject** to discard it."}
        ws.set_suggestion_clarify_stage(suggestion["id"], next_stage)
        return {"reply": _ask_for_stage(next_stage)}

    known_types = workgraph_signals.known_signal_types()

    if stage == "ask_trigger":
        matched = _match_signal_type(stripped, known_types)
        if not matched:
            return {"reply": "I didn't catch a valid one — pick a number, or type the name "
                              "exactly:\n" + _signal_type_options()}
        ws.update_prerequisite_suggestion_structure(suggestion["id"], trigger_signal_type=matched)
        suggestion["trigger_signal_type"] = matched

    elif stage == "ask_requires":
        matched = _match_signal_type(stripped, known_types)
        if not matched:
            return {"reply": "I didn't catch a valid one — pick a number, or type the name "
                              "exactly:\n" + _signal_type_options()}
        if matched == suggestion.get("trigger_signal_type"):
            return {"reply": "That has to be different from what triggers the check — pick "
                              "another one:\n" + _signal_type_options()}
        ws.update_prerequisite_suggestion_structure(suggestion["id"], requires_signal_type=matched)
        suggestion["requires_signal_type"] = matched

    elif stage == "ask_match_on":
        matched = _match_on_answer(stripped)
        if not matched:
            return {"reply": "Reply **project** or **supplier**."}
        ws.update_prerequisite_suggestion_structure(suggestion["id"], match_on=matched)
        suggestion["match_on"] = matched

    else:
        # The clarify_stage CHECK constraint rules out any other stored
        # value, so this is unreachable in practice - treat it the same as
        # "nothing active" defensively rather than raising.
        return None

    next_stage = _next_clarify_stage(suggestion)
    if next_stage is not None:
        ws.set_suggestion_clarify_stage(suggestion["id"], next_stage)
        return {"reply": _ask_for_stage(next_stage)}

    reason = (f"{suggestion['trigger_signal_type']} needs {suggestion['requires_signal_type']} "
              f"first, matched by {suggestion['match_on']}")
    ws.update_prerequisite_suggestion_structure(suggestion["id"], reason=reason)
    ws.set_suggestion_clarify_stage(suggestion["id"], None)
    return {"reply": f"Got it: {reason}. Reply **confirm** to make this a real rule, or "
                      "**reject** to discard it."}


def try_resolve_pending_confirmation(text: str, asker: str) -> Optional[dict]:
    """Returns {"reply": str} if `text` resolved a pending suggestion, or
    None if it didn't look like a confirm/reject answer, or nothing was
    actually pending for this asker - the caller falls through to normal
    Socrates handling in either None case."""
    stripped = strip_leading_mention(text or "").strip()
    is_confirm = bool(_CONFIRM_RE.match(stripped))
    is_reject = bool(_REJECT_RE.match(stripped))
    if not is_confirm and not is_reject:
        return None
    if not asker:
        return None

    since_ts = time.time() - CONFIRMATION_WINDOW_SECONDS
    suggestion = ws.get_most_recent_pending_suggestion_by_asker(asker, since_ts)
    if suggestion is None:
        return None  # looked like an answer, but nothing was actually pending

    if is_confirm:
        if not (suggestion.get("trigger_signal_type") and suggestion.get("requires_signal_type")
                and suggestion.get("match_on")):
            return {"reply": "That one isn't structured enough to confirm yet — finish it in "
                              "Settings › Prerequisite rules › Suggested, or explain it "
                              "again with more detail."}
        ws.create_prerequisite_rule(
            trigger_signal_type=suggestion["trigger_signal_type"],
            requires_signal_type=suggestion["requires_signal_type"],
            match_on=suggestion["match_on"], reason=suggestion.get("reason") or "",
            created_by=asker,
        )
        ws.resolve_prerequisite_suggestion(suggestion["id"], "confirmed")
        return {"reply": "Done — that's a real rule now."}

    ws.resolve_prerequisite_suggestion(suggestion["id"], "rejected")
    return {"reply": "Discarded."}
