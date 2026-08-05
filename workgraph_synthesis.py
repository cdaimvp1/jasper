"""
workgraph_synthesis.py — deterministic staleness check for per-entity
synthesis (Projects, and standalone Issues not yet grouped into a Project).
No LLM calls, no network calls, anywhere in this module.

Per Marc's explicit design requirement: an entity's full communication
history is never re-analyzed wholesale on every refresh. The staleness
question ("does this entity's synthesis need updating?") is 100% answerable
from workgraph.db alone - compute_evidence_marker() and list_stale_entities()
are pure functions of the store. Only the actual synthesis WORK (reading
evidence, extracting facts, writing a narrative) needs an LLM, and that
happens elsewhere entirely: curator's one-shot routine
(run_synthesis_oneshot() in ingest/scheduled_refresh.py), per
SYNTHESIS_ROUTINE.md.

Callable both as a library (from scheduled_refresh.py, to decide whether it's
worth waking curator at all) and standalone, so curator can query her own
work list directly:

    python workgraph_synthesis.py --list-stale
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws

# Bounded gate on curator's synthesis wake (Design-1 budget governor): no
# more than this many entities get synthesized in one wake, ranked
# never-synthesized-first then oldest-marker-first, so a backlog (e.g. after
# being offline a few days) can't turn one curator subprocess into a huge,
# expensive session. Anything past the cap just waits for the next of the 5
# daily scheduled_refresh runs - nothing is dropped, only deferred (see
# run()'s deferred_count).
DEFAULT_SYNTHESIS_LIMIT = 8


def compute_evidence_marker(entity_type: str, entity_id: str) -> str:
    """Revision-counter marker (design doc Section 9.5), not count/max_ts.

    D9/D10's real root cause, found by reading this module directly rather
    than assumed: the OLD marker ("count:N|max_ts:T") correctly flagged a
    late-arriving, old-timestamped item as stale at the top level (any count
    change flips the string) - but the delta this module used to decide
    WHAT was actually new (filtering raw_items by `occurred_ts > since`,
    `since` parsed from the old marker's max_ts) is blind to exactly that
    item, since its own occurred_ts is older than `since`. The entity got
    silently marked fresh again (touch_synthesis_marker) without the late
    content ever actually being folded into a re-synthesis.

    The fix: claims_revision (workgraph_claims.py / Section 9.2) is bumped
    once per raw_item at MATERIALIZATION time - i.e. in the order Jasper
    learned about an item, never in the item's own occurred_ts - which is
    exactly the property this comparison needed and occurred_ts structurally
    can't provide. It also subsumes the old separate materiality filter for
    free: a claim is only ever created for genuinely material content (an
    ask/decision/commitment/date), so a revision bump IS a real materiality
    signal, and pure NOISE/FYI evidence that produces zero claims correctly
    never changes the marker at all - no separate _is_material check needed.

    A project's scope is still every constituent issue's claims activity
    (Marc's requirement: one synthesis per underlying negotiation, which can
    span several issues/threads) - see get_project_claims_fingerprint's own
    docstring for why this is a hash over every member's (id, revision)
    pair, not just the max, computed at check time rather than kept as a
    synced second counter."""
    if entity_type == "project":
        rev = ws.get_project_claims_fingerprint(entity_id)
    elif entity_type == "issue":
        rev = ws.get_claims_revision(entity_id)
    else:
        raise ValueError(f"unknown entity_type: {entity_type}")
    return f"rev:{rev}"


def _parse_rev(marker: Optional[str]) -> int:
    """0 for a never-synthesized entity AND for any pre-migration marker in
    the old "count:...|max_ts:..." format - both correctly sort as the
    oldest/most-overdue bucket, and the old format is never matched by a
    freshly computed "rev:N" marker, so it's always treated as stale once
    (Section 9.5's documented one-time re-synthesis cost)."""
    if not marker or not marker.startswith("rev:"):
        return 0
    try:
        return int(marker.split(":", 1)[1])
    except ValueError:
        return 0


def list_stale_entities(limit: int = DEFAULT_SYNTHESIS_LIMIT, *, stats: Optional[dict] = None) -> list[dict]:
    """Every Project (ws.list_projects()) and every standalone Issue
    (ws.list_standalone_issue_ids() - no project_id) whose current claims-
    revision marker (Section 9.5) doesn't match its stored synthesis row (or
    has no synthesis row at all, which is stale by definition - always
    material, first pass). Pure/deterministic - no LLM/network call
    anywhere in this function or anything it calls.

    The old separate materiality filter (touch the marker without
    re-synthesizing when new evidence was NOISE/FYI-only) is gone - it's
    now structural: claims_revision only bumps when a real ask/decision/
    commitment/date claim is materialized, so purely immaterial evidence
    never changes the marker in the first place. `skipped_immaterial` is
    kept in `stats` at a constant 0 for callers that already read it
    (ingest/scheduled_refresh.py's log line) rather than removing the key.

    Bounded for curator by a per-wake cap: entities are ranked never-
    synthesized-first, then oldest-marker-first, and only the first `limit`
    are returned. The rest are deferred to the next wake, counted in
    `stats` if the caller passed one (see run()) - never silently dropped.
    """
    stale = []

    for project in ws.list_projects():
        marker = compute_evidence_marker("project", project["id"])
        existing = ws.get_synthesis("project", project["id"])
        if existing is not None and existing.get("synthesized_from_marker") == marker:
            continue
        stale.append({
            "entity_type": "project",
            "entity_id": project["id"],
            "name": project["name"],
            "current_marker": marker,
            "previous_marker": existing.get("synthesized_from_marker") if existing else None,
            "previous_summary": existing.get("summary") if existing else None,
            # Corrected pipeline Phase D (2026-08-05): does this project have
            # at least one confirmed member (see ws.project_has_confirmed_
            # grouping's own docstring)? Additive to the existing staleness
            # decision, not a filter on it - a still-provisional project
            # keeps getting its synthesis narrative refreshed same as
            # always; this just tells curator whether the SEPARATE real-
            # issue-extraction step (POST /api/workgraph/projects/{id}/
            # issues) also applies to it this wake.
            "has_confirmed_grouping": ws.project_has_confirmed_grouping(project["id"]),
        })

    for issue_id in ws.list_standalone_issue_ids():
        marker = compute_evidence_marker("issue", issue_id)
        existing = ws.get_synthesis("issue", issue_id)
        if existing is not None and existing.get("synthesized_from_marker") == marker:
            continue
        issue = ws.get_issue(issue_id)
        stale.append({
            "entity_type": "issue",
            "entity_id": issue_id,
            "name": issue["title"] if issue else issue_id,
            "current_marker": marker,
            "previous_marker": existing.get("synthesized_from_marker") if existing else None,
            "previous_summary": existing.get("summary") if existing else None,
        })

    # Rank: never-synthesized (previous_marker is None) first, then
    # oldest-revision first, so a big backlog doesn't starve entities that
    # have been waiting longest (same anti-starvation intent as the
    # 2026-07-29 fix to the old count/max_ts sort - _parse_rev is its
    # rev:N-marker successor).
    stale.sort(key=lambda s: (s["previous_marker"] is not None, _parse_rev(s["previous_marker"])))

    if stats is not None:
        stats["skipped_immaterial"] = 0
        stats["deferred"] = max(0, len(stale) - limit)

    return stale[:limit]


def run() -> dict:
    ws.init_workgraph()
    stats: dict = {}
    stale = list_stale_entities(stats=stats)
    return {"stale_count": len(stale), "stale": stale, **stats}


if __name__ == "__main__":
    # --list-stale is the only supported mode today (per the build spec) -
    # printed regardless, since there's nothing else this script does yet.
    print(json.dumps(run(), indent=2))
