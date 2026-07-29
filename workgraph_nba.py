"""
workgraph_nba.py — pure, deterministic next-best-action scorer. No LLM calls,
no clock reads beyond an explicit `now` passed in (testable, reproducible).

Modeled on the reference workgraph-nba.ts (Theo platform): a weighted score
plus a human-readable reason string built from arithmetic, not a model call.

Weighting note: the reference scorer's weights (0.40 value / 0.30 urgency /
0.20 isYourStep / 0.10 unblocks) lean on a dollar-value signal ("valueTCO")
that this work graph originally had no way to populate (no deal-value parsing
built yet) — v1 dropped it and leaned entirely on state + staleness instead.
Real issue data since then (Ariba requisitions with real dollar amounts in
their subject lines, e.g. "$53,702,143.00 USD") shows the figure is usually
right there in the thread text, so v2 adds a regex-extracted value signal —
still zero LLM calls, just a bigger regex surface. `is_your_step` stays the
dominant term; this is meant to break ties among several "your move" issues
toward the one with real money on the table, not to override whose turn it is.

v2 formula:
    score = 0.45 * is_your_step + 0.25 * staleness_urgency
          + 0.12 * due_urgency  + 0.18 * value_urgency

  - is_your_step: 1.0 if state == 'active' (the ball is in Marc's court),
    0.3 if 'blocked' (someone else's move, but it's stuck), 0 otherwise.
  - staleness_urgency: grows with days since last update, capped at 1.0 —
    the "gone quiet, should be moving" signal (mirrors Field Guide's
    stale_threshold_days=14 concept, but continuous rather than a hard cutoff).
  - due_urgency: 1.0 if overdue, decaying to 0 by 14 days out; 0 if no due
    date is set (the common case today — most issues have none yet).
  - value_urgency: log-scaled dollar amount found in the issue's own thread
    text (subject/body_preview across its raw_items), 0 below $1K, saturating
    at $100M. A single largest-figure-found heuristic — see
    `_extract_value_amount` for the known failure mode (an unrelated large
    number quoted in passing), which is exactly why this term is capped at
    0.18 rather than trusted outright.
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from pathlib import Path
from types import MappingProxyType
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws
import workgraph_lessons

DAY = 86400.0

# Named (fixed 2026-07-29): was a bare `14.0` literal independently duplicated in
# _staleness_urgency and _due_urgency below - same "two weeks" concept in both
# (staleness saturation, due-date decay window), but nothing tied them together,
# so tuning one without noticing the other was an easy way to drift them apart.
STALENESS_SATURATION_DAYS = 14.0

# MappingProxyType - same reasoning as workgraph_alerts.DEFAULT_THRESHOLDS (fixed
# 2026-07-29 alongside it): used directly as score_issue()'s mutable default
# argument; a read-only view turns an accidental in-place edit into an immediate
# TypeError instead of silently corrupting every future call's weights.
DEFAULT_WEIGHTS = MappingProxyType(
    {"is_your_step": 0.45, "staleness": 0.25, "due": 0.12, "value": 0.18})

# Fixed 2026-07-29: two confirmed gaps beyond the module's own disclosed
# "an unrelated figure gets picked up too" limitation. (1) "billion"/"bn"/"b"
# weren't recognized suffixes at all - "$1.2 billion" extracted as a raw 1.2,
# scoring as if it were a $1.20 deal instead of saturating the value term the
# way a genuinely billion-scale figure should. (2) a range like "$2.5-3
# million" truncated to the FIRST number only, with the suffix never applied
# to it either ("$2.5" raw, below _VALUE_FLOOR, so a multi-million-dollar
# range's real scale was silently discarded). The regex now captures an
# optional second number after a hyphen, and the suffix multiplier is
# applied to BOTH sides of a range - "best" (the higher of the two, per this
# function's own MAX-figure theory) is what gets kept.
_DOLLAR_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)(?:\s*-\s*([\d,]+(?:\.\d+)?))?\s*(million|mm|billion|bn|thousand|k|m|b)?\b",
    re.IGNORECASE)
_DOLLAR_SUFFIX_MULTIPLIER = {
    "million": 1_000_000, "mm": 1_000_000, "m": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
    "thousand": 1_000, "k": 1_000,
}
_VALUE_FLOOR = 1_000.0       # below this, don't treat it as a deal-value signal at all
_VALUE_LOG_LOW = 3.0         # log10(1,000)
_VALUE_LOG_SPAN = 5.0        # log10(100,000,000) - log10(1,000)


# Per-raw_item cache (fixed 2026-07-29): recompute_all() re-scores every open issue
# on every periodic tick "even with zero new evidence" (its own docstring), which
# used to mean re-running this regex over the SAME issue's SAME raw_item text every
# single tick forever. raw_items are append-only/immutable once inserted (subject +
# body_preview never change after ingest - confirmed, no update path exists for
# them), so caching the per-item extracted value by raw_item id is always safe: no
# invalidation logic needed, and a newly-linked raw_item just computes+caches on
# first encounter like normal.
_value_cache: dict[int, float] = {}


def _extract_item_value(item: dict) -> float:
    key = item.get("id")
    if key is not None and key in _value_cache:
        return _value_cache[key]
    best = 0.0
    text = " ".join(filter(None, [item.get("subject"), item.get("body_preview")]))
    for match in _DOLLAR_RE.finditer(text):
        suffix = (match.group(3) or "").lower()
        multiplier = _DOLLAR_SUFFIX_MULTIPLIER.get(suffix, 1)
        for group in (match.group(1), match.group(2)):
            if group is None:
                continue
            best = max(best, float(group.replace(",", "")) * multiplier)
    if key is not None:
        _value_cache[key] = best
    return best


def _extract_value_amount(issue_id: str) -> float:
    """Best-effort deterministic dollar-value extraction from this issue's own
    thread text (subject + body_preview of every linked raw_item). Takes the
    MAX figure found, on the theory that a deal's own value is usually the
    largest number quoted in its own thread. Known failure mode: an unrelated
    large figure mentioned in passing gets picked up too — acceptable because
    the resulting signal is capped at a modest weight, not trusted outright."""
    best = 0.0
    for item in ws.get_raw_items_for_issue(issue_id):
        best = max(best, _extract_item_value(item))
    return best


def _value_urgency(amount: float) -> float:
    if amount < _VALUE_FLOOR:
        return 0.0
    return max(0.0, min(1.0, (math.log10(amount) - _VALUE_LOG_LOW) / _VALUE_LOG_SPAN))


def _is_your_step(state: str) -> float:
    return {"active": 1.0, "blocked": 0.3}.get(state, 0.0)


def _staleness_urgency(updated_at: float, now: float) -> float:
    days = max(0.0, (now - updated_at) / DAY)
    return min(1.0, days / STALENESS_SATURATION_DAYS)  # same threshold Field Guide used


def _due_urgency(due_iso: str | None, now: float) -> float:
    if not due_iso:
        return 0.0
    try:
        import datetime
        due_dt = datetime.datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
        # Fixed 2026-07-29: a due date with no explicit timezone (a bare
        # "2026-08-01" from a date picker - the realistic common case)
        # parses as a NAIVE datetime, and .timestamp() on a naive datetime
        # assumes LOCAL time - while `now` is a UTC epoch from time.time().
        # Measured 4h drift on this machine (US Eastern). Explicit UTC
        # attachment removes the ambient-timezone dependency entirely.
        if due_dt.tzinfo is None:
            due_dt = due_dt.replace(tzinfo=datetime.timezone.utc)
        due_ts = due_dt.timestamp()
    except Exception:
        return 0.0
    days_until = (due_ts - now) / DAY
    if days_until <= 0:
        return 1.0  # overdue
    return max(0.0, 1.0 - (days_until / STALENESS_SATURATION_DAYS))


def score_issue(issue: dict, now: float, weights: dict = DEFAULT_WEIGHTS) -> tuple[float, str, Optional[int]]:
    """Pure-ISH: the only non-arithmetic steps are reading this issue's own
    raw_items for the value regex, and looking up a matching Total Recall
    lesson (both just DB reads, still zero LLM calls).
    Returns (priority_score, nba_reason, lesson_id_cited)."""
    if issue["state"] in ("done", "noise-archived"):
        return 0.0, "closed", None

    your_step = _is_your_step(issue["state"])
    staleness = _staleness_urgency(issue["updated_at"], now)
    due = _due_urgency(issue.get("due"), now)
    value_amount = _extract_value_amount(issue["id"])
    value = _value_urgency(value_amount)

    base_score = (weights["is_your_step"] * your_step
                  + weights["staleness"] * staleness
                  + weights["due"] * due
                  + weights["value"] * value)

    # Total Recall: a bounded add-on, never folded into the weights above
    # (which already foot to 1.0 on their own) - see
    # workgraph_lessons.apply_precedent_boost.
    lesson = workgraph_lessons.find_matching_lesson(issue)
    score = workgraph_lessons.apply_precedent_boost(base_score, lesson)

    days_quiet = int(max(0.0, (now - issue["updated_at"]) / DAY))
    reasons = []
    if issue["state"] == "active":
        reasons.append("your move")
    elif issue["state"] == "blocked":
        reasons.append("blocked but stuck")
    if days_quiet >= 7:
        reasons.append(f"quiet {days_quiet}d")
    if due > 0.9:
        reasons.append("overdue")
    elif due > 0.3:
        reasons.append("due soon")
    if value_amount >= 1_000_000:
        reasons.append(f"${value_amount / 1_000_000:,.1f}M")
    elif value_amount >= _VALUE_FLOOR:
        reasons.append(f"${value_amount:,.0f}")
    if lesson:
        reasons.append(f"precedent: {lesson['statement']}")
    if not reasons:
        reasons.append("waiting on someone else")

    return round(score, 4), " · ".join(reasons), (lesson["id"] if lesson else None)


def recompute_all(now: float | None = None) -> dict:
    """Re-score every non-closed issue and persist priority_score + nba_reason.
    Called after curator's classify/cluster pass, and (per the plan) on a
    periodic tick even with zero new evidence — urgency marches forward on
    its own as due dates approach and threads go quiet."""
    if now is None:
        now = time.time()
    issues = ws.list_issues(states=["active", "waiting", "blocked"], limit=1000)
    updated = 0
    for issue in issues:
        score, reason, lesson_id = score_issue(issue, now)
        action_kind = "review" if issue["state"] == "active" else "wait"
        ws.update_issue(issue["id"], priority_score=score, nba_reason=reason,
                         nba_action_kind=action_kind, lesson_id_cited=lesson_id)
        updated += 1
    return {"scored": updated, "as_of": now}


if __name__ == "__main__":
    print(json.dumps(recompute_all(), indent=2))
