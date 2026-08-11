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
    at $100M. Prefers a figure explicitly labeled as a total/contract-value/
    requisition amount; else the largest figure NOT flagged as a credit/
    adjustment/accrued-fee mention; else 0 if every candidate found is one of
    those (fixed 2026-08-01 - see `_extract_value_amount` for the real
    incident this closed and the remaining known failure mode, which is
    exactly why this term stays capped at 0.18 rather than trusted outright).
"""
from __future__ import annotations

import difflib
import json
import math
import re
import sys
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import workgraph_store as ws
import workgraph_confidence as confidence
import workgraph_lessons
import workgraph_aristotle
import workgraph_recommend
import text_extract

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

# Fixed 2026-08-01 (real incident: marc-308 showed $834,353 as "the deal's
# value" - it's really "the order form includes $834,353 in ACCRUED FEES",
# an adjustment figure, not the ~$53.7M headline value; marc-296 picked
# $10,000,000 when the only two numbers present were both explicitly labeled
# CREDITS). The plain max-of-everything heuristic has no way to tell a total
# from an adjustment - these two keyword-proximity cues do, cheaply, without
# an LLM call, matching this module's own house style. A DOWNWEIGHT cue near
# a figure means "this number is relative to some other, unstated total,"
# not the total itself.
_PREFER_CUE = re.compile(
    r"\b(total|contract value|tcv|po amount|purchase order|requisition|"
    r"subscription fee|agreement value|deal value|purchase price|"
    r"not[- ]to[- ]exceed|nte)\b", re.IGNORECASE)
_DOWNWEIGHT_CUE = re.compile(
    r"\b(credits?|adjustment|accrued|true-?up|refund|discount|rebate|"
    r"penalty|late fee|proration|prorated)\b", re.IGNORECASE)
_CUE_WINDOW_CHARS = 40  # chars either side of a $ match to look for a cue word


# Per-raw_item cache (fixed 2026-07-29): recompute_all() re-scores every open issue
# on every periodic tick "even with zero new evidence" (its own docstring), which
# used to mean re-running this regex over the SAME issue's SAME raw_item text every
# single tick forever. raw_items are append-only/immutable once inserted (subject +
# body_preview never change after ingest - confirmed, no update path exists for
# them), so caching the per-item extracted value by raw_item id is always safe: no
# invalidation logic needed, and a newly-linked raw_item just computes+caches on
# first encounter like normal.
_value_cache: dict[int, list[tuple[float, bool]]] = {}


def _extract_item_candidates(item: dict) -> list[tuple[float, bool, bool]]:
    """Every dollar figure found in one raw_item's subject + resolved body
    text, as (value, is_preferred, is_downweighted) triples - flags True
    when a total/contract-value/requisition-type or a credit/adjustment/
    accrued-fee-type cue word (respectively) appears within
    _CUE_WINDOW_CHARS of the match. Cached per raw_item id, same
    immutability reasoning as the pre-2026-08-01 single-float cache this
    replaces.

    Fixed 2026-08-01 (real-incident follow-up): used to read only
    item["body_preview"] (500 chars) - text_extract.resolve_item_text()
    reads the full, quote-stripped body when one was captured (task #43),
    falling back to body_preview for older mail. Checked before writing
    this: marc-308's own real $50M figure happened to already sit inside
    500 chars (found by task #24/#25's fixes, not this one) - not claiming
    that specific case as proof. This closes the general, forward-looking
    version of the same gap: any real figure that lands PAST character 500
    of a longer real email, which the old code could never have reached no
    matter what else was fixed.

    Also now scans this raw_item's own attachments' extracted_text (task
    #29's other half, attachment_extract.py) - a real order-form PDF or
    pricing XLSX sitting on disk was structurally invisible to this
    function before today, no matter what the email text itself said."""
    key = item.get("id")
    if key is not None and key in _value_cache:
        return _value_cache[key]
    text_parts = [item.get("subject"), text_extract.resolve_item_text(item)]
    if key is not None:
        for att in ws.list_attachments("raw_item", str(key)):
            if att.get("extracted_text"):
                text_parts.append(att["extracted_text"])
    text = " ".join(filter(None, text_parts))
    candidates: list[tuple[float, bool, bool]] = []
    for match in _DOLLAR_RE.finditer(text):
        suffix = (match.group(3) or "").lower()
        multiplier = _DOLLAR_SUFFIX_MULTIPLIER.get(suffix, 1)
        window_start = max(0, match.start() - _CUE_WINDOW_CHARS)
        window_end = min(len(text), match.end() + _CUE_WINDOW_CHARS)
        window = text[window_start:window_end]
        preferred = bool(_PREFER_CUE.search(window))
        downweighted = bool(_DOWNWEIGHT_CUE.search(window))
        for group in (match.group(1), match.group(2)):
            if group is None:
                continue
            candidates.append((float(group.replace(",", "")) * multiplier, preferred, downweighted))
    if key is not None:
        _value_cache[key] = candidates
    return candidates


def _extract_value_amount(raw_items: list[dict]) -> float:
    """Best-effort deterministic dollar-value extraction from this issue's own
    thread text (subject + body_preview of every linked raw_item).

    Three tiers, most-trusted first: (1) among figures explicitly labeled as
    a total/contract-value/requisition-type amount, take the max; (2) else,
    among figures NOT flagged as a credit/adjustment/accrued-fee mention,
    take the max, on the theory that a deal's own value is usually the
    largest un-cued number quoted in its own thread; (3) if EVERY candidate
    found is downweighted (e.g. the only figures present are both explicitly
    labeled "credit"), return 0.0 rather than confidently presenting a
    known-likely-wrong number as the deal's value - no signal is more
    honest here than a wrong one. Known remaining failure mode: an
    unrelated, un-cued large figure can still outrank a smaller real one at
    tier 2, which is why this signal stays capped at a modest weight below,
    not trusted outright."""
    all_candidates = [c for item in raw_items for c in _extract_item_candidates(item)]
    preferred = [v for v, is_preferred, _ in all_candidates if is_preferred]
    if preferred:
        return max(preferred)
    not_downweighted = [v for v, _, downweighted in all_candidates if not downweighted]
    if not_downweighted:
        return max(not_downweighted)
    return 0.0


def conflicting_value_figures_for_issue(raw_items: list[dict]) -> list[dict]:
    """Enhancement idea panel #16 (worker capability): _extract_value_amount
    above silently picks the max preferred-tier figure and moves on - it has
    no way to say "two different messages on this issue quote two different
    numbers for the SAME thing." That's a real, different signal: a stale
    figure lingering after an amendment, a typo, or a genuine discrepancy
    between what two parties think the deal is worth - all worth Marc's
    attention, none of which "just take the max" can surface.

    Deliberately per-MESSAGE, not per-candidate: takes each raw_item's own
    SINGLE best (max) preferred-tier figure, then compares those across
    items. A real production SOW/order-form email routinely has several
    internally legitimate preferred-cued figures of its own (milestone
    totals, a grand total, a per-unit price near the word "total") - naively
    collecting every preferred candidate across the whole thread flagged
    those as a false "15 disagreeing figures" conflict on real live data
    (confirmed during verification). Comparing each message's OWN headline
    total against the others' is what "two messages disagree" actually
    means. Returns one entry per distinct headline total found - {amount,
    raw_item_id, occurred_ts} - sorted highest-first. 0 or 1 distinct
    amounts means no conflict; callers should treat len(result) >= 2 as a
    real disagreement worth flagging."""
    per_item_best: dict = {}
    for item in raw_items:
        preferred_values = [v for v, is_preferred, _ in _extract_item_candidates(item) if is_preferred]
        if preferred_values:
            key = item.get("id")
            per_item_best[key] = {
                "amount": max(preferred_values), "raw_item_id": key,
                "occurred_ts": item.get("occurred_ts"),
            }
    distinct: dict[float, dict] = {}
    for entry in per_item_best.values():
        distinct.setdefault(entry["amount"], entry)
    return sorted(distinct.values(), key=lambda e: e["amount"], reverse=True)


def value_amount_for_issue(issue_id: str) -> float:
    """Public wrapper around _extract_value_amount for other modules
    (task #75, Supplier Relationship Dashboard) that need the same
    deterministic per-issue dollar figure without reaching into a private
    name or re-implementing the regex extraction."""
    return _extract_value_amount(ws.get_raw_items_for_issue(issue_id))


def value_amounts_for_issues(issue_ids: list[str]) -> dict[str, float]:
    """Batched form of value_amount_for_issue - one query for N issues
    instead of N. Fixed 2026-07-30 (hardening pass #3): workgraph_suppliers.
    list_suppliers() called value_amount_for_issue() once per open issue
    across every company, the dominant contributor to that endpoint's
    measured 3-4.5s single-worker freeze. Missing/unlinked ids return 0.0,
    same as value_amount_for_issue on an issue with no raw_items."""
    raw_items_by_issue = ws.get_raw_items_for_issues(issue_ids)
    return {iid: _extract_value_amount(raw_items_by_issue.get(iid, [])) for iid in issue_ids}


def value_at_risk_rollup() -> dict:
    """Task #65 (Value-at-risk rollup banner): sums _extract_value_amount
    across every open issue. Reuses the SAME deterministic, zero-LLM
    extraction score_issue's own value_urgency term already uses (rather
    than re-implementing it), so the banner total and each issue's own
    "$X" reason always agree with each other. The known failure mode
    carries over unchanged: an unrelated large figure quoted in passing in
    ANY open thread inflates this total - exactly why the banner must
    present itself as "value found in open threads," never as a certain
    "at risk" claim (see the frontend copy that renders this)."""
    issues = ws.list_issues(states=["active", "waiting", "blocked"], limit=1000)
    contributing = []
    for issue in issues:
        raw_items = ws.get_raw_items_for_issue(issue["id"])
        amount = _extract_value_amount(raw_items)
        if amount >= _VALUE_FLOOR:
            contributing.append({
                "issue_id": issue["id"], "title": issue.get("display_title") or issue["title"],
                "state": issue["state"], "amount": amount,
            })
    contributing.sort(key=lambda c: c["amount"], reverse=True)
    total = sum(c["amount"] for c in contributing)
    return {"total": total, "issue_count": len(contributing), "top": contributing[:5]}


def _value_urgency(amount: float) -> float:
    if amount < _VALUE_FLOOR:
        return 0.0
    return max(0.0, min(1.0, (math.log10(amount) - _VALUE_LOG_LOW) / _VALUE_LOG_SPAN))


def _is_your_step(state: str) -> float:
    return {"active": 1.0, "blocked": 0.3}.get(state, 0.0)


def _staleness_urgency(updated_at: float, now: float, saturation_days: float = STALENESS_SATURATION_DAYS) -> float:
    days = max(0.0, (now - updated_at) / DAY)
    return min(1.0, days / saturation_days)  # same threshold Field Guide used, per-category override optional


# Minimum real historical gaps required before trusting a category's own
# median over the flat default - enhancement idea panel #9's real risk: a
# category with only 1-2 issues (e.g. "onboarding: n=1" confirmed live this
# session) would derive its "typical cadence" from a single thread's own
# idiosyncratic gaps, not a real category pattern. 8 is a judgment call, not
# a measured requirement - chosen so a category needs at least a small
# handful of DIFFERENT issues' gaps contributing, not just one chatty thread.
_MIN_GAPS_FOR_CATEGORY_BASELINE = 8
# A category whose real median gap is unusually SHORT (e.g. a handful of
# same-day back-and-forth threads) shouldn't make staleness trigger at 1-2
# days for everything in that category - floor matches this module's other
# staleness-adjacent minimum (STALENESS_SATURATION_DAYS's own order of
# magnitude), not an arbitrary smaller number.
_CATEGORY_BASELINE_FLOOR_DAYS = 3.0


def compute_category_staleness_baselines() -> dict[str, float]:
    """Enhancement idea panel #9: the flat STALENESS_SATURATION_DAYS (14d)
    applies the same "gone quiet" threshold to every category - but a
    contract negotiation with a law firm and a same-day Teams back-and-forth
    have genuinely different natural cadences, confirmed by reading real
    category data live this session. issues.updated_at alone can't ground
    this: EVERY currently-open issue showed an update within the last 3.6
    days on this exact live DB when checked (a real, current backlog-catch-
    up effect, not a per-category cadence fact) - using today's snapshot
    would just encode that transient artifact as every category's new
    "normal." Grounded instead in the durable signal: the real gaps between
    CONSECUTIVE raw_items on the SAME issue, across that category's full
    history (not just currently-open issues) - a thread's own pacing is real
    regardless of when the whole system last had a catch-up sweep.

    Returns {category: saturation_days} only for categories with at least
    _MIN_GAPS_FOR_CATEGORY_BASELINE real gaps to draw from; a thin category
    is simply absent (caller falls back to STALENESS_SATURATION_DAYS), never
    given a single-thread-derived guess."""
    issues = ws.list_issues(states=None, limit=10000)
    issue_ids = [i["id"] for i in issues]
    category_by_issue = {i["id"]: (i.get("category") or "other") for i in issues}
    raw_items_by_issue = ws.get_raw_items_for_issues(issue_ids)

    gaps_by_category: dict[str, list[float]] = {}
    for issue_id, raw_items in raw_items_by_issue.items():
        category = category_by_issue.get(issue_id, "other")
        ts_sorted = sorted(ri["occurred_ts"] for ri in raw_items if ri.get("occurred_ts"))
        for prev, cur in zip(ts_sorted, ts_sorted[1:]):
            gap_days = (cur - prev) / DAY
            if gap_days > 0:
                gaps_by_category.setdefault(category, []).append(gap_days)

    baselines = {}
    for category, gaps in gaps_by_category.items():
        if len(gaps) < _MIN_GAPS_FOR_CATEGORY_BASELINE:
            continue
        gaps.sort()
        mid = len(gaps) // 2
        median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
        baselines[category] = max(_CATEGORY_BASELINE_FLOOR_DAYS, median)
    return baselines


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


def snooze_history_from_state_history(state_history: list[dict]) -> list[dict]:
    """Enhancement idea panel #10: a transition to 'waiting' with a real
    actor recorded is a deliberate snooze (the single-issue and bulk-triage
    action endpoints both pass actor=config('manager','id') on every such
    call) - an ORGANIC wait (workgraph_classify.recompute_issue_state's
    automated rule) calls update_issue with no actor at all, so actor is
    NULL for those rows. issue_state_history already has everything needed
    (task #22's own actor column) - no new table, no new write path."""
    return [h for h in state_history if h.get("to_state") == "waiting" and h.get("actor")]


# Snooze avoidance boost (enhancement idea panel #10): repeated snoozing is
# itself a signal Marc keeps avoiding this, which should make it MORE
# visible over time, not quietly disappear again every time. An additive,
# bounded add-on applied after confidence damping - same shape as Total
# Recall's apply_precedent_boost just above, not folded into DEFAULT_WEIGHTS
# (rebalancing those is a bigger call than this pass makes, same reasoning
# as E8's distinct-sender scaling). +0.05 per snooze, capped at 5 snoozes
# (+0.25 max) - a judgment call, not a measured requirement.
_SNOOZE_BOOST_PER_COUNT = 0.05
_SNOOZE_BOOST_MAX_COUNT = 5

# Enhancement idea panel #11: an unmet Aristotle prerequisite means acting on
# this issue is exactly the "verify before proceeding" case the check exists
# to catch - it should score lower, not the same as a confirmed-safe issue of
# equal urgency. Multiplicative, applied where prereq is actually checked in
# score_issue (see the comment there for why 0.6 and why multiplicative).
_GATED_ISSUE_DOWNWEIGHT = 0.6


def _apply_snooze_avoidance_boost(score: float, snooze_count: int) -> float:
    boost = _SNOOZE_BOOST_PER_COUNT * min(snooze_count, _SNOOZE_BOOST_MAX_COUNT)
    return min(1.0, score + boost)


# Enhancement idea panel #12: ask density - how many distinct open asks are
# CURRENTLY stacked on this one issue, not whether any single one is stale/
# escalated (E8/#10 already cover those). Three simultaneously-open asks on
# one thread is a real, different signal from one - resolving it clears more
# at once, and a pile of unanswered asks accumulating on the same issue is
# itself worth surfacing. Same bounded-additive shape as the snooze boost
# just above; +0.04 per ask beyond the first, capped at 5 asks (+0.16 max).
_ASK_DENSITY_BOOST_PER_ASK = 0.04
_ASK_DENSITY_BOOST_MAX_ASKS = 5


def ask_density_for_issue(open_claims: list[dict]) -> int:
    """Count of currently-open ask-type claims - open_claims is whatever the
    caller already fetched (list_open_claims_for_issue(s), optionally
    pre-filtered to claim_type='ask'), no new query needed here."""
    return sum(1 for c in open_claims if c.get("claim_type") == "ask")


def _apply_ask_density_boost(score: float, ask_count: int) -> float:
    extra_asks = max(0, ask_count - 1)
    boost = _ASK_DENSITY_BOOST_PER_ASK * min(extra_asks, _ASK_DENSITY_BOOST_MAX_ASKS)
    return min(1.0, score + boost)


# Enhancement idea panel #13: attached-document value corroboration. Since
# E6 (attachment_extract.py) started giving real attachments actual
# extracted TEXT (not just filename/hash metadata), _extract_item_candidates
# above already folds that text into the SAME candidate pool the email body
# contributes to - a real improvement, but it only WIDENS what counts as a
# candidate figure, it can't tell "this issue's chosen deal value happens to
# also sit inside a real order form/SOW" from "this figure was only ever
# mentioned once, in a single email." Those are different confidence levels:
# a party can misstate a number in a quick reply; a genuine attached
# document independently carrying the identical figure is real corroborating
# evidence. This is deliberately EXACT-value matching, not "any large figure
# in an attachment" - a document quoting some OTHER number isn't
# corroboration of the value this issue is scored on.
_VALUE_CORROBORATION_BOOST = 0.05


def _dollar_values_in_text(text: Optional[str]) -> set[float]:
    """Every raw dollar figure present in `text` (suffix multiplier applied),
    as a set of rounded floats - same regex/suffix table as
    _extract_item_candidates, but without the preferred/downweighted cue
    bookkeeping, since corroboration only needs to know WHICH numbers are
    present, not which one score_issue should prefer."""
    if not text:
        return set()
    values = set()
    for match in _DOLLAR_RE.finditer(text):
        suffix = (match.group(3) or "").lower()
        multiplier = _DOLLAR_SUFFIX_MULTIPLIER.get(suffix, 1)
        for group in (match.group(1), match.group(2)):
            if group is not None:
                values.add(round(float(group.replace(",", "")) * multiplier, 2))
    return values


def attachment_corroborates_value(raw_items: list[dict], value_amount: float) -> bool:
    """True if `value_amount` (the figure _extract_value_amount already
    chose for this issue) also appears, independently, in a real
    attachment's extracted_text - not merely somewhere in the combined
    candidate pool. False below _VALUE_FLOOR (nothing to corroborate) and
    false when the issue has no attachments with extracted text at all
    (attachment_extract.py hasn't run on them, or they're not text-bearing
    file types)."""
    if value_amount < _VALUE_FLOOR:
        return False
    target = round(value_amount, 2)
    for item in raw_items:
        key = item.get("id")
        if key is None:
            continue
        for att in ws.list_attachments("raw_item", str(key)):
            if target in _dollar_values_in_text(att.get("extracted_text")):
                return True
    return False


def score_issue(issue: dict, now: float, weights: dict = DEFAULT_WEIGHTS,
                 identity_anchors: Optional[list] = None,
                 category_staleness_baselines: Optional[dict] = None,
                 state_history: Optional[list] = None,
                 open_claims: Optional[list] = None) -> tuple[float, str, Optional[int]]:
    """Pure-ISH: the only non-arithmetic steps are reading this issue's own
    raw_items for the value regex, and looking up a matching Total Recall
    lesson (both just DB reads, still zero LLM calls).

    `identity_anchors` (2026-08-03, confidence spine v1): this issue's real
    identity_anchors rows, pre-fetched by the caller (recompute_all batches
    this via list_identity_anchors_for_issues - one query for every issue,
    not one per issue). Empty/None both fall back to the match_kind shim -
    a confirmed-zero-anchors issue still gets the shim's category-only
    0.15, not an undeserved "no anchors -> full trust" reading (see
    context_accuracy's own docstring for why that default is right for the
    shim's OWN empty-list case but wrong here).

    `category_staleness_baselines` (enhancement idea panel #9): the caller's
    already-computed compute_category_staleness_baselines() result - one
    call for every issue, not one per issue, same batching discipline as
    identity_anchors. None/missing-category both fall back to the flat
    STALENESS_SATURATION_DAYS, same as before this feature existed.
    Returns (priority_score, nba_reason, lesson_id_cited)."""
    if issue["state"] in ("done", "noise-archived", "dismissed"):
        return 0.0, "closed", None

    raw_items = ws.get_raw_items_for_issue(issue["id"])

    your_step = _is_your_step(issue["state"])
    saturation_days = (category_staleness_baselines or {}).get(
        issue.get("category") or "other", STALENESS_SATURATION_DAYS)
    staleness = _staleness_urgency(issue["updated_at"], now, saturation_days)
    due = _due_urgency(issue.get("due"), now)
    value_amount = _extract_value_amount(raw_items)
    value = _value_urgency(value_amount)

    base_score = (weights["is_your_step"] * your_step
                  + weights["staleness"] * staleness
                  + weights["due"] * due
                  + weights["value"] * value)

    # Confidence spine v0/v1 (2026-08-03): damps base_score by how much
    # context actually supports it - thin/stale/no-evidence issues rank
    # lower, never higher, than well-evidenced ones. Safe to apply for real
    # here (unlike the grouping model's hard auto-merge/suggest thresholds):
    # priority_score is a continuous ranking input, not a calibrated
    # pass/fail gate, so damping it can't flip a discrete decision the way
    # it could there. Cheap by design - reuses raw_items already fetched
    # above; identity_anchors, if given, was already batched by the caller.
    has_reference = any(ri.get("pr_number") for ri in raw_items)
    present = set()
    if issue.get("category") and issue["category"] != "other":
        present.add("category")
    if raw_items:
        present.add("evidence")
    ctx = confidence.context_accuracy(
        present_fields=present, required_fields={"category", "evidence"},
        evidence_ts=[ri.get("occurred_ts") for ri in raw_items if ri.get("occurred_ts")], now=now,
        match_kinds=["reference"] if has_reference else ["category"],
        total_refs=1, unresolved_refs=0 if has_reference else 1,
        # Deliberately a truthy check, not "is not None": an issue with
        # confirmed-zero real anchors should fall back to the match_kind
        # shim's category-only 0.15, not the "no anchors -> full trust"
        # default that's correct for the shim's OWN empty-list case but
        # would wrongly read as high confidence here.
        anchor_strengths=([a["anchor_strength"] for a in identity_anchors] if identity_anchors else None),
    )
    base_score = confidence.effective_score(base_score, ctx["context_accuracy"])

    # Total Recall: a bounded add-on, never folded into the weights above
    # (which already foot to 1.0 on their own) - see
    # workgraph_lessons.apply_precedent_boost.
    #
    # Phase 0 fix (D11, 2026-08-03): workgraph_lessons is ENTIRELY a grouping-
    # correction store (every row comes from record_confirmed_or_rejected,
    # called right after a project-suggestion resolves) - it carries no
    # valid precedent for NBA urgency, only for grouping. Gated off by
    # default behind config('grouping','legacy_lessons_cross_engine_enabled')
    # until Total Recall grows an NBA-scoped lesson type of its own.
    lesson = None
    if config.get("grouping", "legacy_lessons_cross_engine_enabled"):
        lesson = workgraph_lessons.find_matching_lesson(issue)
    score = workgraph_lessons.apply_precedent_boost(base_score, lesson)

    snoozes = snooze_history_from_state_history(state_history) if state_history else []
    score = _apply_snooze_avoidance_boost(score, len(snoozes))

    ask_count = ask_density_for_issue(open_claims) if open_claims else 0
    score = _apply_ask_density_boost(score, ask_count)

    value_corroborated = attachment_corroborates_value(raw_items, value_amount)
    if value_corroborated:
        score = min(1.0, score + _VALUE_CORROBORATION_BOOST)

    days_quiet = int(max(0.0, (now - issue["updated_at"]) / DAY))

    # Aristotle (task #51) - a taught prerequisite check. Prepended, not
    # appended: this needs to be the first thing Marc reads, not buried after
    # staleness/value reasons. Only ever "no confirmation seen yet", never
    # "this hasn't happened" - see workgraph_aristotle.py's own docstring.
    prereq = workgraph_aristotle.check_prerequisites(issue["id"], raw_items)
    # Task #319: a second, real source of gating signal - object-to-object
    # project_links dependencies, not just Aristotle's own taught signal-
    # type rules. Only checked when Aristotle's own rule already found
    # nothing (check_prerequisites_all's own "first match wins" discipline,
    # extended across both sources rather than picking one arbitrarily).
    if not prereq:
        prereq = workgraph_aristotle.check_project_link_prerequisite(issue["id"])
    if prereq:
        # Enhancement idea panel #11: has_unmet_prerequisite was persisted
        # (issues.has_unmet_prerequisite, set a few lines below in
        # recompute_all) and shown as a warning, but never actually
        # affected priority_score - a gated issue ranked identically to a
        # confirmed-safe one of the same urgency, even though acting on it
        # without the missing confirmation is exactly what Aristotle exists
        # to prevent. Multiplicative, not subtractive, so it scales with
        # whatever the issue's real urgency already is rather than a flat
        # penalty that could push a already-low-urgency issue negative.
        # 0.6 (40% reduction) is a judgment call, not a measured requirement.
        score *= _GATED_ISSUE_DOWNWEIGHT
    reasons = [prereq["warning"]] if prereq else []
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
    if value_corroborated:
        reasons.append("value confirmed by attachment")
    if lesson:
        reasons.append(f"precedent: {lesson['statement']}")
    if len(snoozes) >= 2:
        last_snoozed_days = int(max(0.0, (now - snoozes[-1]["changed_ts"]) / DAY))
        reasons.append(f"snoozed {len(snoozes)}x, last {last_snoozed_days}d ago")
    if ask_count >= 3:
        reasons.append(f"{ask_count} open asks")
    if not reasons:
        reasons.append("waiting on someone else")

    return round(score, 4), " · ".join(reasons), (lesson["id"] if lesson else None)


def run_choice_log_expiry_daily_if_due(now: float | None = None) -> Optional[dict]:
    """Phase 0 fix (D12, 2026-08-03): same once-a-day gate as retention/
    health_check/aristotle_detection/suggestion_expiry (ws.claim_daily_run).
    'ignored'/'expired' were valid nba_choice_log states from the start but
    nothing ever wrote them - an 'offered' row with no matching action just
    sat open forever, so get_most_recent_open_choice_log kept returning an
    offer that was no longer live. Reuses STALENESS_SATURATION_DAYS (this
    file's own "two weeks" window) as the default TTL rather than inventing
    a second unrelated staleness constant."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not ws.claim_daily_run("nba_choice_log_expiry", today):
        return None
    ttl_days = config.get("nba", "choice_log_ttl_days") or STALENESS_SATURATION_DAYS
    expired = ws.expire_stale_nba_choice_logs(ttl_days)
    return {"expired": expired, "ttl_days": ttl_days}


# --- Task #318: NBA outcome tracking - the one fuzzy piece --------------
# Everything above/below this block (nba_outcome_log's create/dismiss
# events) is fully deterministic, logged straight from the server route
# that observed the reaction - no judgment involved. This block is the one
# genuinely ambiguous piece the task asked for: given a hero-draft-reply's
# real suggested_text (the only action_kind that ever carries one - see
# nba_outcome_log's own table comment) and a plausible later sent version
# of it, how much did Marc actually rewrite it?
#
# Two real, separate limitations, both worth naming plainly rather than
# quietly working around:
#
# (1) No draft-to-sent linkage exists anywhere in Outlook's own COM model -
#     Display()ing a draft and later hitting Send() on it produces a brand
#     new Sent Items EntryID with zero reference back to the draft that
#     became it. outlook_com_sent_ingest.py (task #270) already pulls every
#     sent item into raw_items as a real outbound row on the SAME issue
#     (via conversation_id/thread_key, exactly like its inbound sibling),
#     which is what makes correlation possible AT ALL, but it can only ever
#     be "the closest outbound item on this issue after the draft" - a
#     heuristic time-window match, not a verified link. Two real drafts
#     fired off close together on the same issue could produce a wrong
#     pairing; this is disclosed, not hidden, in rewrite_note.
#
# (2) There is no live, synchronous LLM-call path anywhere in this codebase
#     (confirmed by search - every place real judgment/generation is needed,
#     e.g. workgraph_proactive's contract-review dispatch, routes through a
#     Team Room message to a separate worker session, an async round-trip
#     with no fit for a lightweight per-row classification like this one).
#     Building that dispatch path is real, new infrastructure - exactly
#     what this task said not to invent for this one piece. classify_
#     rewrite_severity below is therefore a deterministic difflib-ratio
#     PROXY for "how much did the meaning change," not a real semantic
#     judgment - it will call `judge_fn` instead when one is supplied (kept
#     pluggable so a real LLM-backed classifier can be dropped in later
#     without touching the capture/correlation plumbing around it), but
#     ships with no judge_fn wired in. Flagged here and in the task report,
#     not silently passed off as more than it is.

SENT_TEXT_CORRELATION_WINDOW_SECONDS = 7 * DAY  # give up trying to correlate after this long
REWRITE_SEVERITY_THRESHOLD = 0.35  # judgment call: below this, "accepted as-is" with minor edits


def _find_likely_sent_reply(issue_id: str, after_ts: float,
                             window_seconds: float = SENT_TEXT_CORRELATION_WINDOW_SECONDS) -> Optional[dict]:
    """The closest real outbound raw_item on this issue occurring after
    after_ts (the draft event's own detected_ts) and within window_seconds -
    see this module's own limitation (1) above for why this is a best-
    effort heuristic, never a verified link. None when nothing outbound
    shows up on this issue in the window (the draft was never sent, or
    Sent Items ingestion hasn't caught up yet - both real, both handled the
    same honest way: keep it pending, don't guess)."""
    candidates = [
        ri for ri in ws.get_raw_items_for_issue(issue_id)
        if ri.get("direction") == "outbound" and ri.get("occurred_ts")
        and after_ts < ri["occurred_ts"] <= after_ts + window_seconds
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda ri: ri["occurred_ts"])


def classify_rewrite_severity(suggested_text: str, sent_text: str,
                               judge_fn: Optional[Any] = None) -> dict:
    """Returns {"severity": float in [0,1], "note": str}. severity is how
    much sent_text diverges from suggested_text - 0 means identical, 1
    means unrecognizable. judge_fn, if given, is called instead as
    judge_fn(suggested_text, sent_text) -> {"severity": float, "note": str}
    (the real semantic-judgment hook this module has no live caller for
    yet - see the block docstring above); with none supplied (the only path
    actually exercised today), falls back to a plain difflib.
    SequenceMatcher ratio - a cheap, honest character-level proxy for
    "did the wording change," explicitly NOT a claim about whether the
    MEANING changed."""
    if judge_fn is not None:
        return judge_fn(suggested_text, sent_text)
    similarity = difflib.SequenceMatcher(None, suggested_text or "", sent_text or "").ratio()
    severity = round(1.0 - similarity, 4)
    return {
        "severity": severity,
        "note": (f"difflib character-similarity proxy (not a semantic judgment - "
                 f"see workgraph_nba module docstring): {similarity:.2f} similarity"),
    }


def attempt_rewrite_judgment(limit: int = 50, now: float | None = None) -> dict:
    """Works through nba_outcome_log rows that captured real suggested_text
    and have no sent_text resolved yet (ws.list_nba_outcomes_pending_
    rewrite_judgment) - for each, tries to find a plausible sent
    counterpart (_find_likely_sent_reply) and, if one turns up, classifies
    the rewrite severity and records it (ws.record_rewrite_judgment,
    flipping outcome to 'rewritten' only above REWRITE_SEVERITY_THRESHOLD).
    Rows still inside the correlation window with no counterpart yet are
    left untouched (Sent Items ingestion may just not have caught up);
    rows that have aged past the window with nothing found are marked
    abandoned (ws.mark_nba_outcome_correlation_abandoned) so they stop
    being retried forever."""
    if now is None:
        now = time.time()
    pending = ws.list_nba_outcomes_pending_rewrite_judgment(limit)
    judged = abandoned = still_pending = 0
    for row in pending:
        sent_item = _find_likely_sent_reply(row["issue_id"], row["detected_ts"])
        if sent_item is not None:
            sent_text = text_extract.resolve_item_text(sent_item) or sent_item.get("body_preview") or ""
            result = classify_rewrite_severity(row["suggested_text"], sent_text)
            ws.record_rewrite_judgment(
                row["id"], sent_text=sent_text, rewrite_severity=result["severity"],
                rewrite_note=result["note"], rewritten=result["severity"] >= REWRITE_SEVERITY_THRESHOLD,
            )
            judged += 1
        elif now - row["detected_ts"] > SENT_TEXT_CORRELATION_WINDOW_SECONDS:
            ws.mark_nba_outcome_correlation_abandoned(
                row["id"],
                f"no outbound raw_item found on this issue within "
                f"{SENT_TEXT_CORRELATION_WINDOW_SECONDS / DAY:.0f}d of the draft - "
                "either never sent, or Outlook's lack of draft-to-sent linkage means "
                "this heuristic correlation simply couldn't find it (see module docstring)",
            )
            abandoned += 1
        else:
            still_pending += 1
    return {"judged": judged, "abandoned": abandoned, "still_pending": still_pending}


def run_rewrite_judgment_daily_if_due(now: float | None = None) -> Optional[dict]:
    """Same once-a-day gate as run_choice_log_expiry_daily_if_due above -
    Sent Items ingestion runs on its own cadence, so re-attempting
    correlation more than once a day buys nothing real."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not ws.claim_daily_run("nba_rewrite_judgment", today):
        return None
    return attempt_rewrite_judgment(now=now)


def recompute_all(now: float | None = None) -> dict:
    """Re-score every non-closed issue and persist priority_score + nba_reason.
    Called after curator's classify/cluster pass, and (per the plan) on a
    periodic tick even with zero new evidence — urgency marches forward on
    its own as due dates approach and threads go quiet.

    Fixed 2026-08-06 (Marc's direct report, root-caused against a real
    example - marc-1172, an issue that stayed labeled "your move" long
    after recompute_issue_state had already flipped it to "waiting"):
    this used to cap at limit=1000, and list_issues orders by
    priority_score DESC NULLS LAST - issues that already have a score keep
    winning that window every tick (self-reinforcing), while a freshly
    created or freshly state-changed issue (NULL/stale score) sorts toward
    the bottom and can be starved out of ever being reached once the open-
    issue count exceeds 1000. The pipeline2 backfill just grew that count
    past 2,600 - comfortably past the old cap, so this was silently
    excluding a large and growing fraction of issues from ever being
    rescored, not just a theoretical edge case. Raised well above any
    realistic near-term corpus size rather than removing the cap outright,
    since list_issues still needs *some* bound to build its SQL LIMIT."""
    if now is None:
        now = time.time()
    issues = ws.list_issues(states=["active", "waiting", "blocked"], limit=10000)
    # Confidence spine v1: one batched query for every issue's real
    # identity_anchors, not one query per issue (list_identity_anchors_
    # for_issues, same batching discipline as list_parties_for_issues).
    anchors_by_issue = ws.list_identity_anchors_for_issues([i["id"] for i in issues])
    # Enhancement idea panel #9: one category-baseline computation for this
    # whole tick, not one per issue - same batching discipline as anchors
    # above. Real DB-wide scan (list_issues(states=None) internally), so
    # this deliberately isn't cheap enough to call per-issue.
    category_staleness_baselines = compute_category_staleness_baselines()
    # Enhancement idea panel #10: one batched state-history query, same
    # shape as anchors_by_issue above - list_issue_state_history_for_issues
    # already exists (built for workgraph_alerts, same batching discipline).
    state_history_by_issue = ws.list_issue_state_history_for_issues([i["id"] for i in issues])
    # Enhancement idea panel #12: one batched open-asks query, same shape as
    # the others above - list_open_claims_for_issues already existed (built
    # for rank_actions), just filtered to claim_type='ask' here since that's
    # the only type ask_density_for_issue cares about.
    open_asks_by_issue = ws.list_open_claims_for_issues([i["id"] for i in issues], claim_type="ask")
    updated = 0
    for issue in issues:
        score, reason, lesson_id = score_issue(
            issue, now, identity_anchors=anchors_by_issue.get(issue["id"]),
            category_staleness_baselines=category_staleness_baselines,
            state_history=state_history_by_issue.get(issue["id"]),
            open_claims=open_asks_by_issue.get(issue["id"]),
        )
        action_kind = "review" if issue["state"] == "active" else "wait"
        # task #55: reason's prefix is a fixed, owned string (workgraph_
        # aristotle.WARNING_PREFIX) - checking it here avoids recomputing
        # check_prerequisites() a second time just to get this boolean.
        has_unmet_prerequisite = reason.startswith(workgraph_aristotle.WARNING_PREFIX)
        ws.update_issue(issue["id"], touch_updated_at=False, priority_score=score, nba_reason=reason,
                         nba_action_kind=action_kind, lesson_id_cited=lesson_id,
                         has_unmet_prerequisite=1 if has_unmet_prerequisite else 0)
        updated += 1
    return {"scored": updated, "as_of": now}


def candidate_actions(
    issue: dict, evidence: list[dict], synthesis: Optional[dict] = None,
    project_synthesis: Optional[dict] = None,
) -> list[dict]:
    """Part E1 of the grouping/NBA redesign (2026-07-30): unifies the 3
    previously-uncoordinated "what should Marc do next" surfaces - this
    issue's own single nba_action_kind/nba_reason verdict, workgraph_
    recommend.py's per-evidence-row recommendations (ev["recommendations"],
    a LIST per row since task #15 - already populated by
    attach_recommendations before this is called), and curator's own
    synthesis suggested_actions - into one ranked,
    deduped list. Each candidate: {kind, label, rationale, score,
    source_surface}. Top-ranked is the de facto NBA; the rest are real
    alternatives, not previously visible together anywhere.

    Read-time only - does NOT change what recompute_all() above writes to
    issues.nba_action_kind/nba_reason. Confirmed by reading recompute_all
    itself: that column's 7-value CHECK-constrained enum is only EVER
    written as 'review' or 'wait' by real code (the other 5 allowed
    values are unused) - it was never a rich action-type vocabulary, and
    repurposing it here would have meant either a real CHECK-constraint
    mismatch against workgraph_recommend's own kind strings (contract_
    review/prep/summarize, none of which are in that enum) or silently
    losing information by forcing everything into 2 buckets. This
    function is purely additive instead - no schema change, no new
    clickable UI yet (that's Part E2), just making the 3 signals
    comparable in one ranked place for the first time.

    Scoring bands (fixed 2026-07-31, Marc's direct report): the 'nba' and
    'evidence_row' surfaces are BOTH mechanical templates - a fixed label
    keyed off issue state or a shallow rule ("has an attachment" ->
    "review it") - neither reads or reasons about what the message
    actually asks for. 'synthesis' candidates are the one surface curator
    actually read and reasoned about the specific content to produce
    (SYNTHESIS_ROUTINE.md's suggested_actions). The original scores (nba=
    priority_score, evidence_row=0.5, synthesis=0.45 flat) put the LEAST
    specific candidate on top almost every time, since priority_score and
    0.5 usually beat 0.45 - confirmed live on marc-185, where "Draft a
    reply / your move" outranked "Confirm the Nintex DocGen notice is
    legitimate" and "Approve/reject PR1111865...". A reasoned candidate
    must outrank a template one whenever both exist, not the reverse.

    Fixed 2026-08-02 (task #21): the caller used to pick between issue-
    level and project-level synthesis with a bare `synthesis or
    project_synthesis`, which only checks whether the DICT is truthy - a
    synthesis row with a real summary but an empty suggested_actions list
    is still a non-empty dict, so it always won that `or` and the
    project's own suggested_actions were never considered even when they
    had real content. Fixed by taking both explicitly and preferring
    synthesis only when it actually carries suggested_actions."""
    candidates = []
    effective_synthesis = synthesis if (synthesis and synthesis.get("suggested_actions")) else project_synthesis

    # Generic-template ceiling: neither templated surface may outscore a
    # real synthesis candidate, no matter how high this issue's own
    # priority_score is.
    _GENERIC_CEILING = 0.6
    _SYNTHESIS_BAND = 0.9

    if issue.get("nba_reason"):
        # Task #233: the issue's own top-line NBA card used to be blind to
        # WHAT the issue actually is - "waiting" always got the same generic
        # "Nudge," "active" always got the same generic "Draft a reply,"
        # even when the most recent evidence is a structured, recognized
        # signal (an Ariba approval, a signature request) that isn't
        # nudge-able or reply-able at all - nobody drafts an email reply to
        # Ariba. When the most recent evidence row (evidence is already
        # ts DESC - see list_evidence) carries a signal_type this system
        # has a real, specific action for, that wins; otherwise the
        # original state-based nudge/draft_reply behavior is unchanged.
        most_recent_signal_type = evidence[0].get("signal_type") if evidence else None
        signal_kind_label = workgraph_recommend.SIGNAL_ACTION_KIND_LABEL.get(most_recent_signal_type)
        if signal_kind_label:
            kind, label = signal_kind_label
        else:
            kind = "nudge" if issue.get("state") == "waiting" else "draft_reply"
            label = "Nudge" if kind == "nudge" else "Draft a reply"
        candidates.append({
            "kind": kind, "label": label,
            "rationale": issue["nba_reason"],
            "score": min(issue.get("priority_score") or 0.5, _GENERIC_CEILING),
            "source_surface": "nba",
        })

    seen_kinds = {c["kind"] for c in candidates}
    for ev in evidence:
        # task #15: a row can carry more than one real recommendation (e.g.
        # an attachment matching both invoice-audit and SOW language) - every
        # one of them is a genuine candidate, not just the first.
        for rec in (ev.get("recommendations") or []):
            if not rec or rec.get("kind") in seen_kinds:
                continue
            seen_kinds.add(rec["kind"])
            candidates.append({
                "kind": rec["kind"], "label": rec.get("label") or rec["kind"],
                "rationale": rec.get("rationale") or "",
                "score": min(0.5, _GENERIC_CEILING), "source_surface": "evidence_row",
                # 2026-08-02, detail-panel port: the ONLY source_surface with a
                # real single raw_item_id to point to (nba is issue-level,
                # synthesis is project/issue-aggregate) - lets the checklist UI
                # scope this action to the specific ask/decision/etc. that
                # shares this same raw_item_id, instead of every candidate
                # landing in the unscoped "General" bucket.
                "raw_item_id": ev.get("raw_item_id"),
            })

    if effective_synthesis:
        seen_labels = {c["label"] for c in candidates}
        for idx, a in enumerate(effective_synthesis.get("suggested_actions") or []):
            label = a.get("label") or ""
            if not label or label in seen_labels:
                continue
            seen_labels.add(label)
            candidates.append({
                "kind": "custom", "label": label, "rationale": a.get("rationale") or "",
                # idx-ordered within the band so curator's own authored
                # order survives ties, never below _GENERIC_CEILING.
                "score": _SYNTHESIS_BAND - (idx * 0.01), "source_surface": "synthesis",
            })

    # Phase 0 fix (D15, 2026-08-03): this used to append an unconditional
    # "Draft a reply" fallback whenever none of the three real surfaces
    # above (nba_reason, evidence recommendations, synthesis suggested_
    # actions) produced anything - a candidate with literally no supporting
    # evidence, presented exactly like a real one. Removed: no evidence,
    # no candidate. An issue with nothing to suggest now honestly returns
    # an empty list instead of a manufactured action.

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:4]


# --- NBA v2: rank actions, not issues (design doc Section 11, Phase 4) -----
# Ranking unit is an open ask/commitment CLAIM owned by Marc (workgraph_
# claims.py, Section 9.4's deterministic owner - never a keyword guess), not
# an issue. Additive and observe-only (Section 11.5): does not change
# issues.priority_score, recompute_all, or candidate_actions above - this is
# a new, separate surface pending Marc's own review before anything wires
# into the primary Inbox sort.

DEFAULT_CLAIM_WEIGHTS = MappingProxyType(
    {"staleness": 0.40, "due": 0.30, "value": 0.15, "escalation": 0.15})

# Tiered, not decaying (Section 11.3): claims.date_kind is curator-judged
# hard/soft, never a parsed calendar date - no free-text date parsing exists
# to compute a real days-until-due number (same stated scope limit as
# Section 9.7). Task #57's fix, made concrete here: this is keyed on
# date_kind alone, never on the date claim's owner - a counterparty's own
# hard deadline that still has real consequences for Marc counts in full,
# never downweighted for not being "his" deadline.
_DATE_TIER = MappingProxyType({"hard": 1.0, "soft": 0.5})

# A genuinely chatty single issue with 5 open asks shouldn't fill the whole
# ranked list on its own - a real, stated design cap (Section 11.4), not an
# oversight.
_MAX_ACTIONS_PER_ISSUE = 2

DEFAULT_RANK_ACTIONS_LIMIT = 20


def _issue_date_urgency(date_claims: list[dict]) -> float:
    best = 0.0
    for c in date_claims:
        best = max(best, _DATE_TIER.get(c.get("date_kind"), 0.0))
    return best


def distinct_escalation_sender_count(raw_items: list[dict]) -> int:
    """Enhancement idea panel #8: claims.escalated (Section 9.3) is a flat
    boolean - it can't tell "the same one person nagged twice" from "three
    different people independently pushed on this," and the second is a
    real, stronger signal Marc should weigh higher. Claims don't record
    which raw_item triggered each escalation touch (touch_claim just
    updates the same row - see workgraph_claims.py), so this is computed at
    the issue level instead: distinct inbound senders among ACTIONABLE-ASK/
    WAITING-ON-OTHERS raw_items, which is exactly the population that
    could have driven an escalation in the first place. Outbound items
    (Marc's own asks) never count - this is about how many OTHER people are
    pushing, not how many times Marc himself has asked."""
    senders = {
        (ri.get("from_actor") or "").strip().lower()
        for ri in raw_items
        if ri.get("direction") == "inbound"
        and ri.get("item_class") in ("ACTIONABLE-ASK", "WAITING-ON-OTHERS")
        and (ri.get("from_actor") or "").strip()
    }
    return len(senders)


def score_claim(claim: dict, *, date_urgency: float, value_urgency_score: float,
                 now: float, weights: dict = DEFAULT_CLAIM_WEIGHTS,
                 distinct_sender_count: int = 1) -> tuple[float, str]:
    """Pure. staleness is keyed on the CLAIM's own first_seen_ts (how long
    THIS specific ask has sat open) - a more precise clock than score_issue
    has access to, which only ever sees the whole issue's updated_at.
    escalation reuses claims.escalated (Section 9.3's real repeat/
    escalation signal, previously computed but never consumed for
    anything) - v1 had no equivalent. Confidence damping is applied by the
    caller (rank_actions), not here - same issue-level context_accuracy
    score_issue already computes, reused rather than recomputed per claim.

    distinct_sender_count (enhancement idea panel #8) scales the escalation
    term instead of adding a new weight bucket - rebalancing DEFAULT_CLAIM_
    WEIGHTS is a bigger call than this pass makes. 1 sender (the default,
    and the floor - a claim can't be escalated by zero people) still gets
    the same escalated=1.0 this always gave; 3+ distinct senders is full
    credit; 2 is partial. Callers that don't know real sender counts (the
    default of 1) get exactly v1's old binary behavior, unchanged."""
    staleness = _staleness_urgency(claim["first_seen_ts"], now)
    escalation = min(1.0, max(1, distinct_sender_count) / 3) if claim.get("escalated") else 0.0
    score = (weights["staleness"] * staleness + weights["due"] * date_urgency
             + weights["value"] * value_urgency_score + weights["escalation"] * escalation)

    reasons = []
    if claim.get("escalated"):
        if distinct_sender_count >= 2:
            reasons.append(f"escalated by {distinct_sender_count} different people")
        else:
            reasons.append("escalated")
    days_open = int(max(0.0, (now - claim["first_seen_ts"]) / DAY))
    if days_open >= 7:
        reasons.append(f"open {days_open}d")
    if date_urgency >= 1.0:
        reasons.append("hard deadline on this thread")
    elif date_urgency >= 0.5:
        reasons.append("soft deadline on this thread")
    if not reasons:
        reasons.append("open")

    return score, " · ".join(reasons)


def rank_actions(limit: int = DEFAULT_RANK_ACTIONS_LIMIT, now: float | None = None) -> list[dict]:
    """Every open ask/commitment claim owned by Marc, across every open
    issue, ranked globally by score_claim - the real "what should I do
    next" list `candidate_actions` couldn't provide at anything beyond
    single-issue scope, and v1's issue-level priority_score couldn't
    provide at all (an issue-level score can't tell three urgent asks on
    one thread from one middling one). decision claims are excluded from
    the ranked list itself (owner is always None by design - a decision is
    a joint fact, not an obligation) - see workgraph_claims.py's owner
    derivation for why.

    Batched throughout (list_open_claims_for_issues / get_raw_items_for_issues
    / list_identity_anchors_for_issues) - one query per input across every
    open issue, not one per issue, same N+1-avoidance discipline as
    recompute_all/score_issue above."""
    if now is None:
        now = time.time()

    issues = ws.list_issues(states=["active", "waiting", "blocked"], limit=1000)
    issue_ids = [i["id"] for i in issues]
    claims_by_issue = ws.list_open_claims_for_issues(issue_ids)
    raw_items_by_issue = ws.get_raw_items_for_issues(issue_ids)
    anchors_by_issue = ws.list_identity_anchors_for_issues(issue_ids)

    ranked: list[dict] = []
    for issue in issues:
        issue_id = issue["id"]
        claims = claims_by_issue.get(issue_id, [])
        actionable = [c for c in claims if c["claim_type"] in ("ask", "commitment") and c.get("owner") == "marc"]
        if not actionable:
            continue

        date_urgency = _issue_date_urgency([c for c in claims if c["claim_type"] == "date"])
        raw_items = raw_items_by_issue.get(issue_id, [])
        value_score = _value_urgency(_extract_value_amount(raw_items))
        distinct_sender_count = distinct_escalation_sender_count(raw_items)

        has_reference = any(ri.get("pr_number") for ri in raw_items)
        present = set()
        if issue.get("category") and issue["category"] != "other":
            present.add("category")
        if raw_items:
            present.add("evidence")
        anchors = anchors_by_issue.get(issue_id)
        ctx = confidence.context_accuracy(
            present_fields=present, required_fields={"category", "evidence"},
            evidence_ts=[ri.get("occurred_ts") for ri in raw_items if ri.get("occurred_ts")], now=now,
            match_kinds=["reference"] if has_reference else ["category"],
            total_refs=1, unresolved_refs=0 if has_reference else 1,
            anchor_strengths=([a["anchor_strength"] for a in anchors] if anchors else None),
        )

        issue_candidates = []
        for claim in actionable:
            base, reason = score_claim(claim, date_urgency=date_urgency, value_urgency_score=value_score,
                                        now=now, distinct_sender_count=distinct_sender_count)
            score = confidence.effective_score(base, ctx["context_accuracy"])
            issue_candidates.append({
                "claim_id": claim["id"], "issue_id": issue_id, "project_id": issue.get("project_id"),
                "text": claim["text"], "claim_type": claim["claim_type"],
                "score": round(score, 4), "reason": reason,
                "raw_item_id": claim.get("raw_item_id"),
            })
        issue_candidates.sort(key=lambda c: c["score"], reverse=True)
        ranked.extend(issue_candidates[:_MAX_ACTIONS_PER_ISSUE])

    ranked.sort(key=lambda c: c["score"], reverse=True)
    return ranked[:limit]


if __name__ == "__main__":
    print(json.dumps(recompute_all(), indent=2))
