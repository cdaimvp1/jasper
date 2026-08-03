"""
workgraph_sessionize.py — Teams sub-session boundaries (identity
formalization, 2026-08-03).

docs/design/CONFIDENCE_AND_IDENTITY_REDESIGN.md Section 3.2's real gap:
Teams anchors only at the chat-container level today - every message in a
chat shares one thread_key regardless of how many distinct topics/deals
pass through that same conversation over time (marc-362's real shape:
one Teams chat container covering several unrelated PR approvals plus
entirely unrelated conversation).

v0, deliberately simplified from the Blueprint's full boundary model
(rare-entity overlap / artifact overlap / action-object overlap / topic-
token overlap corroboration - none of which have an existing
implementation in this codebase to reuse, so building them now would be
new detection logic, not formalization, exactly what Section 3.3's
"backfill, not build" rule is about). This version uses only two signals
Jasper already has:
  1. Exclusive reference-anchor continuity (a shared PR/PO base keeps
     messages in one session regardless of gap; a DIFFERENT reference is
     a real topic-shift signal, not just a gap).
  2. Time gap between consecutive messages, with an 8h/72h split matching
     the Blueprint's own thresholds.

Real reply-threading (`replyToId`) is NOT used - ingest/normalize.py does
not capture it today (confirmed absent from the real Teams payload shape
this session profiled). That stays a named, real gap for whenever Teams
ingest is revisited, not silently substituted for here.

No LLM calls, deterministic, pure (no DB reads/writes) - callers are
responsible for fetching raw_items in chronological order and persisting
the result.
"""
from __future__ import annotations

HOUR = 3600.0
DEFAULT_GAP_SAME_HOURS = 8.0
DEFAULT_GAP_NEW_HOURS = 72.0


def sessionize_teams_messages(messages: list, *, gap_same_hours: float = DEFAULT_GAP_SAME_HOURS,
                               gap_new_hours: float = DEFAULT_GAP_NEW_HOURS) -> list:
    """`messages`: raw_items rows (dicts) for ONE teams_chat container, in
    chronological order by `occurred_ts` - the caller's responsibility, not
    re-sorted here (a caller that already has them ordered from a query
    shouldn't pay for a redundant sort, and re-sorting silently would mask
    a caller bug that fed the wrong order).

    Returns a NEW list of dicts, each the original row plus:
      - session_sequence: 0-indexed, increments at each detected boundary
      - boundary_reason: why THIS message started a new session (None for
        the first message in its session, including the very first message
        in the whole list)

    Boundary rules, checked against the CURRENT SESSION's own reference
    (the most recent non-null `pr_number_base` seen since the last
    boundary - sticky across ref-less messages in between, not just a
    check against the single immediately-preceding message, so a reply
    with no reference doesn't blind the check to a real topic shift two
    messages later) and the gap since the immediately-preceding message:
      1. The session already has a reference AND this message has a
         DIFFERENT one -> new session (reference mismatch is stronger
         evidence than any gap).
      2. This message's reference matches the session's (or the session
         has none yet and adopts this one) -> same session, regardless
         of gap.
      3. No reference to compare -> gap-based: <= gap_same_hours stays;
         > gap_new_hours splits; the ambiguous middle defaults to staying
         (the Blueprint's own corroboration signals for that middle
         aren't implemented - see module docstring - so v0 doesn't guess
         a split it can't corroborate)."""
    result = []
    session_sequence = 0
    session_ref = None
    prev_ts = None
    for msg in messages:
        boundary_reason = None
        this_ref = msg.get("pr_number_base")
        if prev_ts is None:
            boundary_reason = "first_message"
            session_ref = this_ref
        else:
            gap = (msg.get("occurred_ts") or 0) - prev_ts
            if session_ref and this_ref and this_ref != session_ref:
                session_sequence += 1
                boundary_reason = "reference_mismatch"
                session_ref = this_ref
            elif this_ref:
                session_ref = this_ref  # matches, or the session adopts its first reference
            elif gap > gap_new_hours * HOUR:
                session_sequence += 1
                boundary_reason = "gap_exceeds_72h"
                session_ref = None
            # else: <= gap_same_hours, or the ambiguous 8-72h middle with
            # no corroborating signal - stays in the same session.
        result.append({**msg, "session_sequence": session_sequence, "boundary_reason": boundary_reason})
        prev_ts = msg.get("occurred_ts") or 0
    return result
