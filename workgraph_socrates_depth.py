"""
workgraph_socrates_depth.py — depth classifier for Socrates-for-Jasper.

Decides how much retrieval work a free-text question warrants before
workgraph_socrates.answer() plans anything:

  lookup   - check precedent (Total Recall) + the relevant synthesis, nothing else.
  standard - + a narrow, targeted search of linked evidence.
  deep     - + a wider, cross-issue evidence search. Also raises the bar an
             answer must clear to auto-resolve (from 'medium' to 'high'
             confidence) - a deep question is one where a shrug-worthy answer
             isn't good enough.

Jasper has no structured task-kind field the way Theo does, so the primary
signal here is the ISSUE itself (its priority/state, when the question names
one) - not free text. Free text is an ESCALATE-ONLY fallback, exactly like
Theo's depth-classifier.ts: it can only raise the depth, never lower it, and
it is only consulted when nothing more structured is available. An explicit
caller-requested depth can raise the floor further but can never force a
shallower treatment than the issue's own state warrants - a blocked or
high-priority issue can't be waved down to 'lookup' just because someone
asked for a quick check.

Pure, deterministic, no LLM call, no IO (the issue dict is passed in already
fetched). Every decision carries a rationale trail for audit, same discipline
as the rest of body/workgraph_*.py.
"""
from __future__ import annotations

import re
from typing import Optional

DEPTH_RANK = {"lookup": 0, "standard": 1, "deep": 2}
_DEPTH_BY_RANK = ["lookup", "standard", "deep"]

# Tiers each depth plans, in authority/cost order - cheapest + most-grounded
# first, matching Theo's retrieval-planner.ts ordering rationale.
TIERS_BY_DEPTH = {
    "lookup": ["recall", "materialized"],
    "standard": ["recall", "materialized", "targeted-research"],
    "deep": ["recall", "materialized", "targeted-research", "broad-research"],
}

# The band a CLEAN tier must reach for early-stop to fire. A lookup/standard
# question is satisfied by 'medium'; a deep one needs 'high'.
STOP_BAND_BY_DEPTH = {"lookup": "medium", "standard": "medium", "deep": "high"}

# Escalate-only vocabulary (fallback path only): presence signals real
# judgment is warranted, not a quick lookup. Adapted from the same
# legal/commercial/compliance territory Jasper's own issue categories and
# workgraph_recommend.py's regexes already live in - not invented from
# scratch. Matching can only RAISE depth, so a false positive over-serves
# (safe) and never under-serves.
_DEEP_SIGNAL = re.compile(
    r"\b(negotiat|liabilit|indemnif|terminat|breach|sovereignt|residenc|"
    r"concentrat|single[-\s]?source|exception|escalat|regulat|complian|"
    r"renew|penalt|jurisdict|risk|sign(ed|off)?|award)",
    re.I,
)


def _raise(depth: str, levels: int = 1) -> str:
    rank = min(DEPTH_RANK["deep"], DEPTH_RANK[depth] + levels)
    return _DEPTH_BY_RANK[rank]


def _max_depth(a: str, b: str) -> str:
    return a if DEPTH_RANK[a] >= DEPTH_RANK[b] else b


def classify_depth(*, text: str = "", issue: Optional[dict] = None,
                    explicit_depth: Optional[str] = None) -> dict:
    """Classify a question into a depth + execution profile.

    issue: the issue dict the question is scoped to, when known (has
      'priority'/'state'). Absent for a free-standing chat question.
    explicit_depth: an asker-requested depth override ('lookup'|'standard'|
      'deep'). Wins over inference, but the safety floor below can still
      raise it further.

    Returns {depth, tiers, stop_band, rationale}.
    """
    rationale: list[str] = []

    # --- 1) INFERENCE ---------------------------------------------------
    if explicit_depth in DEPTH_RANK:
        inferred = explicit_depth
        rationale.append(f"explicit caller depth: {explicit_depth}")
    elif issue and issue.get("priority") == "high":
        inferred = "standard"
        rationale.append("issue priority 'high' -> standard")
    else:
        inferred = "standard"
        rationale.append("no structured signal -> standard (balanced default)")

    # --- 2) SAFETY FLOOR (escalate-only, applies even over an explicit ask) -
    depth = inferred

    def apply_floor(floor: str, reason: str) -> None:
        nonlocal depth
        if DEPTH_RANK[floor] > DEPTH_RANK[depth]:
            depth = _max_depth(depth, floor)
            rationale.append(f"safety floor: {reason} -> {floor}")

    if issue and issue.get("state") == "blocked":
        apply_floor("deep", "issue is blocked")
    if issue and issue.get("priority") == "high":
        apply_floor("standard", "issue priority 'high'")
    if text and _DEEP_SIGNAL.search(text):
        apply_floor(_raise(inferred), "text fallback: high-stakes vocabulary present")

    # --- 3) PROFILE -------------------------------------------------------
    return {
        "depth": depth,
        "tiers": TIERS_BY_DEPTH[depth],
        "stop_band": STOP_BAND_BY_DEPTH[depth],
        "rationale": rationale,
    }

