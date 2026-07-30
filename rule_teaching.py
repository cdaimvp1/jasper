"""
rule_teaching.py — task #54: "#addrule" chat-based prerequisite-rule
teaching in Back Office. Wired into /api/socrates/ask (server_lean.py)
BEFORE workgraph_socrates.answer() - that function stays exactly as it was,
100% zero-LLM; this module is the new, clearly separate capability sitting
next to it, not folded into it.

Two entry points:
  teach_from_chat(explanation, asker) - ALWAYS logs the verbatim explanation
    as a pending_prerequisite_suggestions row (that part never fails, no
    matter what the LLM does - a try/except around the extraction call
    guarantees the raw capture can't be lost to an extraction bug), then
    attempts rule_extraction.py's local-LLM structuring. Confident+valid ->
    the row is created WITH the structured fields already filled in, and the
    reply reflects them back. Not confident/unavailable -> the row is
    raw-only, and the reply says so honestly rather than pretending it
    understood.
  try_resolve_pending_confirmation(text, asker) - checks whether `text`
    reads like a short yes/no answer, and if so resolves the asker's most
    recent still-pending taught_via_chat suggestion, within a recency
    window (not an open-ended search back through all history). Returns
    None - not a confirmation at all - when there's nothing to resolve, so
    the caller falls through to normal Socrates handling (a bare "yes" with
    nothing pending is a legitimately unanswerable question, not a bug).
"""
from __future__ import annotations

import re
import time
from typing import Optional

import rule_extraction
import workgraph_store as ws

# Matches the cohort's own ~30-minute heartbeat convention (CLAUDE.md/the F9
# poller) - not load-bearing here, just a sensible, consistent recency floor
# so confirming something from days ago by accident isn't possible.
CONFIRMATION_WINDOW_SECONDS = 1800

_CONFIRM_RE = re.compile(r"^\s*(confirm|yes|y)\b", re.IGNORECASE)
_REJECT_RE = re.compile(r"^\s*(reject|no|n)\b", re.IGNORECASE)

ADDRULE_RE = re.compile(r"(?i)^#addrule\b\s*")


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
    else:
        suggestion_id = ws.create_prerequisite_suggestion(
            origin="taught_via_chat", trigger_signal_type=None, requires_signal_type=None,
            match_on=None, reason=None, evidence=None, raw_explanation=explanation, proposed_by=asker,
        )
        reply = ("Logged what you said for review — I couldn't confidently match it to a known "
                 "signal type on my own. You can structure it further in Settings › "
                 "Prerequisite rules › Suggested, or ask a worker to help interpret it.")
    return {"reply": reply, "suggestion_id": suggestion_id}


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
