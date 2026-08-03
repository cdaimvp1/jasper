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

import json
import math
import re
import sys
import time
from pathlib import Path
from types import MappingProxyType
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import workgraph_store as ws
import workgraph_confidence as confidence
import workgraph_lessons
import workgraph_aristotle
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


def score_issue(issue: dict, now: float, weights: dict = DEFAULT_WEIGHTS,
                 identity_anchors: Optional[list] = None) -> tuple[float, str, Optional[int]]:
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
    Returns (priority_score, nba_reason, lesson_id_cited)."""
    if issue["state"] in ("done", "noise-archived", "dismissed"):
        return 0.0, "closed", None

    raw_items = ws.get_raw_items_for_issue(issue["id"])

    your_step = _is_your_step(issue["state"])
    staleness = _staleness_urgency(issue["updated_at"], now)
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

    days_quiet = int(max(0.0, (now - issue["updated_at"]) / DAY))

    # Aristotle (task #51) - a taught prerequisite check. Prepended, not
    # appended: this needs to be the first thing Marc reads, not buried after
    # staleness/value reasons. Only ever "no confirmation seen yet", never
    # "this hasn't happened" - see workgraph_aristotle.py's own docstring.
    prereq = workgraph_aristotle.check_prerequisites(issue["id"], raw_items)
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
    if lesson:
        reasons.append(f"precedent: {lesson['statement']}")
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


def recompute_all(now: float | None = None) -> dict:
    """Re-score every non-closed issue and persist priority_score + nba_reason.
    Called after curator's classify/cluster pass, and (per the plan) on a
    periodic tick even with zero new evidence — urgency marches forward on
    its own as due dates approach and threads go quiet."""
    if now is None:
        now = time.time()
    issues = ws.list_issues(states=["active", "waiting", "blocked"], limit=1000)
    # Confidence spine v1: one batched query for every issue's real
    # identity_anchors, not one query per issue (list_identity_anchors_
    # for_issues, same batching discipline as list_parties_for_issues).
    anchors_by_issue = ws.list_identity_anchors_for_issues([i["id"] for i in issues])
    updated = 0
    for issue in issues:
        score, reason, lesson_id = score_issue(issue, now, identity_anchors=anchors_by_issue.get(issue["id"]))
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
        kind = "nudge" if issue.get("state") == "waiting" else "draft_reply"
        candidates.append({
            "kind": kind, "label": "Nudge" if kind == "nudge" else "Draft a reply",
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


if __name__ == "__main__":
    print(json.dumps(recompute_all(), indent=2))
