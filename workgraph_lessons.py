"""
workgraph_lessons.py — Total Recall for Jasper: a small, deterministic
precedent store. No embeddings, no similarity model, no LLM call anywhere in
this module or in anything that reads from it.

A "lesson" is populated two ways, both close to free:
  1. Automatically, from a resolved project-grouping suggestion (see
     workgraph_projects.confirm_suggestion/reject_suggestion) - the
     statement is templated, not LLM-authored, so this path costs nothing
     extra to run.
  2. Optionally, as one extra field (`closing_lesson`) on a synthesis write
     curator was already making (see SYNTHESIS_ROUTINE.md) - never a
     standalone LLM call made just to mine a lesson.

Trust score starts at a flat prior and is nudged by outcome: +TRUST_BUMP
(capped at TRUST_CEILING) on a repeat-confirming hit, -TRUST_PENALTY (floored
at TRUST_FLOOR) when the OPPOSITE outcome lands for the same situation_key
(a reversal - this lesson was just contradicted). Same arithmetic-over-model
style as workgraph_nba.py.

Inline validation - deliberately just the two checks that matter here, not a
general governance framework:
  1. write time (validate_lesson_write) - the cited source_issue_id must be
     real, the statement must be non-empty and bounded in length, the
     outcome must be a known value.
  2. read time (find_matching_lesson / attach_learned) - re-check
     trust_score is in [0,1] and the cited issue still exists before trusting
     a lesson enough to change a score or render a badge. A lesson failing
     either check is treated as absent (abstain), never surfaced or applied,
     and never raised as an error - a missing/rejected lesson is a normal,
     expected outcome here (same discipline as PROJECT_GROUPING_ROUTINE.md's
     "abstaining is a correct, expected outcome").

Scoring integration is a bounded ADD-ON, never folded into
workgraph_nba.DEFAULT_WEIGHTS (which already foots to 1.0) - a precedent can
nudge the score, never override real value/urgency signal.
"""
from __future__ import annotations

from typing import Optional

import workgraph_signals
import workgraph_store as ws

MIN_TRUST = 0.6
DEFAULT_TRUST = 0.6
TRUST_BUMP = 0.1
TRUST_PENALTY = 0.15
TRUST_CEILING = 0.9
TRUST_FLOOR = 0.1
PRECEDENT_BOOST_WEIGHT = 0.10
STRONG_PRECEDENT_HITS = 3          # N repeats with zero reversals -> auto-resolve, curator never woken
STRONG_PRECEDENT_TRUST = 0.8       # DEFAULT_TRUST + (STRONG_PRECEDENT_HITS-1)*TRUST_BUMP, by construction -
                                   # a hit_count that ever crossed STRONG_PRECEDENT_HITS via a REVERSAL rather
                                   # than a clean run of repeats will have a lower trust_score than this, which
                                   # is exactly why precedent_prefilter gates on trust, not hit_count alone.
MAX_STATEMENT_LEN = 240

_OPPOSITE = {"confirmed": "rejected", "rejected": "confirmed"}


def situation_key(category: Optional[str], company: Optional[str]) -> Optional[str]:
    """Deterministic signature. None if there isn't enough real signal to
    key on - no category (or the catch-all 'other'), or no real company."""
    cat = (category or "").strip().lower()
    comp = (company or "").strip().lower()
    if not cat or cat == "other" or not comp:
        return None
    return f"category:{cat}|company:{comp}"


def _first_external_company(issue_id: str) -> Optional[str]:
    for p in ws.list_parties_for_issue(issue_id):
        if (p.get("affiliation") == "external" and p.get("company")
                and not workgraph_signals._SYSTEM_SENDER.match(p.get("primary_email") or "")):
            return p["company"]
    return None


def situation_key_for_issue(issue: dict) -> Optional[str]:
    company = _first_external_company(issue["id"])
    return situation_key(issue.get("category"), company)


def validate_lesson_write(*, source_issue_id: str, statement: str, outcome: str) -> Optional[str]:
    """Returns None if the write is OK, else a short rejection reason.
    Never raises - a rejected write is a normal, expected outcome (the
    caller treats None-return-from-record_* as 'no lesson recorded', not a
    failure of the operation that triggered it)."""
    if outcome not in ("confirmed", "rejected", "resolved"):
        return f"unknown outcome '{outcome}'"
    if not statement or not statement.strip():
        return "empty statement"
    if len(statement) > MAX_STATEMENT_LEN:
        return f"statement exceeds {MAX_STATEMENT_LEN} chars"
    if ws.get_issue(source_issue_id) is None:
        return f"source_issue_id '{source_issue_id}' does not exist"
    return None


def record_lesson(*, situation_key_val: str, statement: str, outcome: str, source_issue_id: str) -> Optional[dict]:
    """Writes a lesson (or bumps trust on the existing one for the same
    situation_key + outcome). Returns None - never raises - if
    validate_lesson_write rejects it."""
    reject_reason = validate_lesson_write(source_issue_id=source_issue_id, statement=statement, outcome=outcome)
    if reject_reason:
        return None
    return ws.upsert_lesson(
        situation_key=situation_key_val, outcome=outcome, statement=statement,
        source_issue_id=source_issue_id, default_trust=DEFAULT_TRUST, bump=TRUST_BUMP, ceiling=TRUST_CEILING,
    )


def record_confirmed_or_rejected(*, issue_id_a: str, status: str) -> Optional[dict]:
    """Called from workgraph_projects.py right after a project-suggestion
    resolves (confirmed or rejected). The statement is templated - this path
    costs nothing extra to run, it's just plumbing on a decision already
    made. Also penalizes the OPPOSITE outcome's lesson for the same
    situation_key, if one exists - a contradiction just landed for it."""
    issue_a = ws.get_issue(issue_id_a)
    if issue_a is None:
        return None
    key = situation_key_for_issue(issue_a)
    if key is None:
        return None
    company = key.split("|", 1)[1].split("company:", 1)[1]
    category = key.split("|", 1)[0].split("category:", 1)[1]
    outcome = "confirmed" if status == "confirmed" else "rejected"

    ws.penalize_lesson(key, _OPPOSITE[outcome], penalty=TRUST_PENALTY, floor=TRUST_FLOOR)

    existing = ws.get_lesson_by_situation(key, outcome)
    n = (existing["hit_count"] + 1) if existing else 1
    verb = "the same project" if outcome == "confirmed" else "judged UNRELATED"
    statement = f"same-category ('{category}') threads involving {company} have been {verb} {n} time(s)"
    # Confirmed bug, 2026-07-29: `company` is an unbounded external-party name
    # (e.g. a long legal entity name) - a long enough one pushed this over
    # MAX_STATEMENT_LEN, and validate_lesson_write rejects that on EVERY
    # call, not just the first insert. Since this path also runs on repeat
    # confirm/reject (the trust-bump path), that meant trust_score/hit_count
    # could never update again for that situation_key - silently, forever,
    # indistinguishable from "no lesson yet." Truncating here guarantees
    # record_lesson always gets a statement within the cap.
    if len(statement) > MAX_STATEMENT_LEN:
        statement = statement[:MAX_STATEMENT_LEN - 1].rstrip() + "…"
    return record_lesson(situation_key_val=key, statement=statement, outcome=outcome, source_issue_id=issue_id_a)


def precedent_prefilter(issue: dict) -> Optional[str]:
    """Design-1 hook: before creating a pending weak-signal suggestion (or
    waking curator to judge one), check whether this issue's situation_key
    has already been rejected/confirmed STRONG_PRECEDENT_HITS+ times with a
    trust score high enough that a reversal hasn't recently walked it back
    down. Returns 'confirmed' or 'rejected' to auto-resolve deterministically
    - curator is never woken for it - or None to fall through to today's
    behavior (create/keep the pending suggestion)."""
    key = situation_key_for_issue(issue)
    if key is None:
        return None
    for outcome in ("rejected", "confirmed"):
        lesson = ws.get_lesson_by_situation(key, outcome)
        if lesson and lesson["hit_count"] >= STRONG_PRECEDENT_HITS and lesson["trust_score"] >= STRONG_PRECEDENT_TRUST:
            return outcome
    return None


def find_matching_lesson(issue: dict) -> Optional[dict]:
    """Read path for NBA scoring / cockpit display. Re-validates trust_score
    range and that the cited source issue still exists - a lesson failing
    either check is treated as absent (abstain), never applied. Only
    'confirmed'/'resolved' outcomes are ever surfaced as a positive
    precedent boost - a 'rejected' lesson (these two are NOT the same
    project) has nothing useful to say about priority."""
    key = situation_key_for_issue(issue)
    if key is None:
        return None
    best = None
    for outcome in ("confirmed", "resolved"):
        lesson = ws.get_lesson_by_situation(key, outcome)
        if lesson is None:
            continue
        if not (0.0 <= lesson["trust_score"] <= 1.0):
            continue
        if ws.get_issue(lesson["source_issue_id"]) is None:
            continue
        if lesson["trust_score"] < MIN_TRUST:
            continue
        if best is None or lesson["trust_score"] > best["trust_score"]:
            best = lesson
    return best


def apply_precedent_boost(base_score: float, lesson: Optional[dict]) -> float:
    """Bounded additive nudge - never enough to push the score past 1.0,
    never folded into workgraph_nba.DEFAULT_WEIGHTS (which already foots to
    1.0 on its own), never enough on its own to override real value/urgency
    signal from the base formula."""
    if lesson is None:
        return base_score
    boost = min(PRECEDENT_BOOST_WEIGHT * lesson["trust_score"], 1.0 - base_score)
    return base_score + max(0.0, boost)


def confidence_band(trust_score: float) -> str:
    if trust_score >= 0.8:
        return "high"
    if trust_score >= MIN_TRUST:
        return "medium"
    return "low"


def attach_learned(issues: list[dict]) -> list[dict]:
    """Mutates and returns `issues`: adds a `learned` key (dict or None) per
    issue, resolved from its cited lesson (issues.lesson_id_cited, set by
    workgraph_nba.recompute_all). Computed at READ time - same pattern as
    workgraph_recommend.attach_recommendations - so a trust-score change
    (e.g. a reversal) is reflected immediately rather than waiting for the
    next NBA recompute. Re-validates the lesson the same way
    find_matching_lesson does; a lesson that no longer checks out renders as
    no badge, not an error."""
    for issue in issues:
        lesson_id = issue.get("lesson_id_cited")
        lesson = ws.get_lesson(lesson_id) if lesson_id else None
        if (lesson is None or not (0.0 <= lesson["trust_score"] <= 1.0)
                or ws.get_issue(lesson["source_issue_id"]) is None):
            issue["learned"] = None
            continue
        issue["learned"] = {"statement": lesson["statement"], "confidence": confidence_band(lesson["trust_score"])}
    return issues
