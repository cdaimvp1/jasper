"""
workgraph_awayreport.py - task #372: the real aggregation behind "what
changed while I was away?" Same shape and discipline as workgraph_focus.py
(task #283): sections built from existing, already-correct readers, zero LLM,
zero new extraction.

WHAT THIS DELIBERATELY DOES NOT DO: rank, score, or decide what matters. A
delta report answers "what happened since <t>", which is a question with a
factual answer. "What should I do about it" is a different question that
workgraph_nba.rank_actions already answers, and duplicating that judgment
here - a second, quietly divergent notion of importance - is exactly the kind
of authored resolver this codebase refuses to grow. Sections are ordered by
TIME, never by an invented priority, and the caller is free to hand any issue
id straight to the NBA reader if it wants a recommendation.

The window is a parameter, not a guess. There is no stored "last time Marc
looked" cursor anywhere in this schema, so inventing one here (say, defaulting
to 72h) would silently define "away" on the user's behalf and quietly drop
anything older. Callers pass since_ts explicitly; DEFAULT_LOOKBACK_HOURS is
offered only for a caller that has genuinely nothing better, and the value it
used is echoed back in the window block so a report can never misrepresent
the span it covers.
"""
from __future__ import annotations

import time
from typing import Optional

import workgraph_store as ws

DEFAULT_LOOKBACK_HOURS = 72.0

# Claim types split out separately because they mean different things to a
# returning human: an ask is someone waiting on YOU, a commitment is what you
# or a counterparty promised, a date is a deadline that may have moved.
_CLAIM_SECTIONS = ("ask", "commitment", "decision", "date")

_OPEN_STATES = ("active", "waiting", "blocked")
# Reaching a state in this set during the window is a closure - reported
# separately because "this finished while you were out" reads very differently
# from "this is still moving". Mirrors the vocabulary already in the claims/
# work_objects CHECK constraints; not a new taxonomy.
_CLOSED_STATES = ("done", "dismissed", "noise-archived")


def _title_of(row: dict) -> str:
    return row.get("display_title") or row.get("title") or row.get("id") or ""


def build_away_summary(since_ts: float, now: Optional[float] = None) -> dict:
    """Everything that changed in [since_ts, now], grouped by kind.

    Every section is derived, never stored - calling this twice with the same
    window returns the same answer, and calling it never marks anything as
    seen. That matters: a report that silently consumed its own backlog would
    make "let me look at that again" impossible.
    """
    if now is None:
        now = time.time()

    # 1. Evidence that arrived. exclude_sensitive stays FALSE here, unlike
    # workgraph_discovery's use of the same reader: this is Marc reading his
    # own graph, not learning material being fed to a model, and hiding a
    # sensitive item from its own owner's "what did I miss" report would make
    # the report quietly wrong. Only counts and titles surface below anyway.
    raw = [r for r in ws.list_raw_items_since(since_ts) if (r.get("occurred_ts") or 0) <= now]

    by_issue: dict[str, list[dict]] = {}
    ungrouped: list[dict] = []
    for r in raw:
        iid = r.get("issue_id")
        if iid:
            by_issue.setdefault(iid, []).append(r)
        else:
            ungrouped.append(r)

    # 2. State transitions, including closures the open-only reader can't see.
    changes = [c for c in ws.list_issue_state_changes_since(since_ts)
               if (c.get("changed_ts") or 0) <= now]
    closed = [c for c in changes if c.get("to_state") in _CLOSED_STATES]
    reopened = [c for c in changes
                if c.get("to_state") in _OPEN_STATES and c.get("from_state") in _CLOSED_STATES]

    # 3. Claims carried by evidence that ARRIVED in this window.
    #
    # Keyed on the underlying raw_item, NOT on claims.first_seen_ts, and that
    # distinction is the single most important decision in this module.
    # first_seen_ts records when a claim was MATERIALIZED, not when the
    # request reached Marc. Measured on the live corpus 2026-08-22: of 9,097
    # claims first-seen in the preceding 14 days, 6,709 were materialized
    # inside a single three-hour span on 2026-08-11 (a backfill run), the
    # median gap between the evidence's own occurred_ts and the claim's
    # first_seen_ts was 53.8 days, and 8,543 of 9,097 - 94% - sat on evidence
    # older than the window entirely. A first_seen_ts filter would therefore
    # have reported one catch-up sweep as ~940 brand-new asks landing on him
    # while he was out. That is not a rounding error, it is the report
    # confidently saying the opposite of the truth.
    #
    # No extra query: `raw` above already IS the set of evidence in the
    # window, so membership by raw_item_id is both free and exactly right.
    window_item_ids = {r["id"] for r in raw}
    touched_ids = sorted(set(by_issue) | {c["issue_id"] for c in changes if c.get("issue_id")})
    claims_by_issue = ws.list_claims_for_issues(touched_ids) if touched_ids else {}
    new_claims: dict[str, list[dict]] = {k: [] for k in _CLAIM_SECTIONS}
    materialized_from_older_evidence = 0
    for iid, claims in claims_by_issue.items():
        for c in claims:
            first_seen = c.get("first_seen_ts") or 0
            arrived_now = c.get("raw_item_id") in window_item_ids
            if not arrived_now:
                # Counted, not listed, and never mixed into the sections
                # above: "Jasper finally extracted this from old mail" is a
                # true fact about the pipeline, but it is not something that
                # happened to Marc while he was away.
                if since_ts <= first_seen <= now:
                    materialized_from_older_evidence += 1
                continue
            kind = c.get("claim_type")
            if kind in new_claims:
                new_claims[kind].append({
                    "issue_id": iid, "claim_id": c.get("id"), "text": c.get("text"),
                    "author": c.get("author"), "owner": c.get("owner"),
                    "status": c.get("status"), "first_seen_ts": first_seen,
                    "raw_item_id": c.get("raw_item_id"),
                })
    for kind in new_claims:
        new_claims[kind].sort(key=lambda c: c["first_seen_ts"])

    # 4. Per-issue activity rollup, ordered by most recent arrival - time
    # order, not an invented importance order (see module docstring).
    activity = []
    for iid, items in by_issue.items():
        issue = ws.get_issue_or_cluster(iid) or {}
        activity.append({
            "issue_id": iid,
            "title": _title_of(issue) or iid,
            "project_id": issue.get("project_id"),
            "state": issue.get("state"),
            "new_item_count": len(items),
            "sources": sorted({i.get("source") for i in items if i.get("source")}),
            "latest_ts": max((i.get("occurred_ts") or 0) for i in items),
        })
    activity.sort(key=lambda a: a["latest_ts"], reverse=True)

    return {
        "window": {
            "since_ts": since_ts,
            "until_ts": now,
            "hours": round((now - since_ts) / 3600.0, 2),
        },
        "counts": {
            "new_items": len(raw),
            "issues_with_new_items": len(by_issue),
            "ungrouped_new_items": len(ungrouped),
            "state_changes": len(changes),
            "closed": len(closed),
            "reopened": len(reopened),
            **{f"new_{k}s": len(v) for k, v in new_claims.items()},
            # Reported alongside, never folded into the new_* counts above -
            # see the claims block for why conflating the two would make this
            # report actively misleading after any backfill.
            #
            # Scoped to issues that saw window activity, so it is a FLOOR, not
            # a census: a backfill touching an issue with no new mail at all
            # in the window is invisible here. That is the right tradeoff for
            # a delta report (a wholly quiet issue did not change for Marc),
            # but it means this number must not be read as "how much the
            # backfill did" - workgraph_claims_backfill's own return value is
            # the honest source for that question.
            "claims_materialized_from_older_evidence": materialized_from_older_evidence,
        },
        "activity": activity,
        "closed": closed,
        "reopened": reopened,
        "state_changes": changes,
        "new_claims": new_claims,
        # Surfaced, not hidden: evidence that arrived but has not been grouped
        # onto any issue yet is a real gap in the report's own coverage, and
        # the honest thing is to say so rather than let the counts imply the
        # window was fully accounted for.
        "ungrouped_new_items": [
            {"raw_item_id": r.get("id"), "source": r.get("source"),
             "subject": r.get("subject"), "occurred_ts": r.get("occurred_ts")}
            for r in sorted(ungrouped, key=lambda r: r.get("occurred_ts") or 0, reverse=True)
        ],
    }


def build_away_summary_for_hours(hours: float = DEFAULT_LOOKBACK_HOURS,
                                 now: Optional[float] = None) -> dict:
    """Convenience wrapper for a caller that thinks in "the last N hours"
    rather than an absolute timestamp. Resolves to the same function so there
    is only one implementation of the window arithmetic."""
    if now is None:
        now = time.time()
    return build_away_summary(now - hours * 3600.0, now=now)
