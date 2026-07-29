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

# item_class values that never justify waking curator on their own (see
# _is_material) - matches workgraph_classify.py's taxonomy.
_NON_MATERIAL_CLASSES = ("NOISE", "FYI-EVIDENCE")


def compute_evidence_marker(entity_type: str, entity_id: str) -> str:
    """Pure. A project's evidence scope is the UNION of every one of its
    constituent issues' evidence (Marc's requirement: one synthesis per
    underlying negotiation, which can span several issues/threads) - an
    issue's scope is just its own evidence. Marker is deliberately just
    count + max timestamp, not a hash of full content: cheap, and sufficient
    to detect "something new landed" without needing to diff bodies."""
    if entity_type == "project":
        evidence: list[dict] = []
        for issue in ws.list_issues_for_project(entity_id):
            evidence.extend(ws.list_evidence(issue["id"]))
    elif entity_type == "issue":
        evidence = ws.list_evidence(entity_id)
    else:
        raise ValueError(f"unknown entity_type: {entity_type}")

    count = len(evidence)
    max_ts = max((e["ts"] for e in evidence), default=0.0)
    return f"count:{count}|max_ts:{max_ts}"


def _parse_max_ts(marker: Optional[str]) -> float:
    if not marker:
        return 0.0
    for part in marker.split("|"):
        if part.startswith("max_ts:"):
            try:
                return float(part.split(":", 1)[1])
            except ValueError:
                return 0.0
    return 0.0


def _new_raw_items_since(entity_type: str, entity_id: str, previous_marker: Optional[str]) -> list[dict]:
    """raw_items newly occurred since previous_marker's max_ts - the actual
    DELTA a materiality check needs, not just the count/timestamp marker."""
    since = _parse_max_ts(previous_marker)
    if entity_type == "project":
        issue_ids = [i["id"] for i in ws.list_issues_for_project(entity_id)]
    else:
        issue_ids = [entity_id]
    items = []
    for iid in issue_ids:
        items.extend(r for r in ws.get_raw_items_for_issue(iid) if r["occurred_ts"] > since)
    return items


def _is_material(new_items: list[dict]) -> bool:
    """True if the new evidence is worth waking curator over: anything
    beyond NOISE/FYI-EVIDENCE, or an FYI item whose extraction (if one
    exists yet) actually carries an ask/decision/commitment. Pure inbox
    churn (an automated FYI notice, a closure receipt) returns False."""
    for item in new_items:
        if item.get("item_class") not in _NON_MATERIAL_CLASSES:
            return True
        extraction = ws.get_extraction(item["id"])
        if extraction and any(extraction["extracted_json"].get(k) for k in ("asks", "decisions", "commitments")):
            return True
    return False


def list_stale_entities(limit: int = DEFAULT_SYNTHESIS_LIMIT, *, stats: Optional[dict] = None) -> list[dict]:
    """Every Project (ws.list_projects()) and every standalone Issue
    (ws.list_standalone_issue_ids() - no project_id) whose current evidence
    marker doesn't match its stored synthesis row (or has no synthesis row at
    all, which is stale by definition - always material, first pass).
    Pure/deterministic - no LLM/network call anywhere in this function or
    anything it calls, INCLUDING the materiality check and the marker-touch
    it triggers.

    Two things keep this bounded and cheap for curator:
      - materiality filter: an entity whose only new evidence since its last
        synthesis is NOISE/FYI-EVIDENCE with no real extraction content has
        its marker silently advanced (ws.touch_synthesis_marker) instead of
        being added to the list - it won't keep re-flagging every wake for
        evidence that never needed a re-synthesis.
      - per-wake cap: entities are ranked never-synthesized-first, then
        oldest-marker-first, and only the first `limit` are returned. The
        rest are deferred to the next wake, counted in `stats` if the caller
        passed one (see run()) - never silently dropped.
    """
    stale = []
    skipped_immaterial = 0

    for project in ws.list_projects():
        marker = compute_evidence_marker("project", project["id"])
        existing = ws.get_synthesis("project", project["id"])
        if existing is not None and existing.get("synthesized_from_marker") == marker:
            continue
        if existing is not None:
            new_items = _new_raw_items_since("project", project["id"], existing.get("synthesized_from_marker"))
            if not _is_material(new_items):
                ws.touch_synthesis_marker("project", project["id"], marker)
                skipped_immaterial += 1
                continue
        stale.append({
            "entity_type": "project",
            "entity_id": project["id"],
            "name": project["name"],
            "current_marker": marker,
            "previous_marker": existing.get("synthesized_from_marker") if existing else None,
            "previous_summary": existing.get("summary") if existing else None,
        })

    for issue_id in ws.list_standalone_issue_ids():
        marker = compute_evidence_marker("issue", issue_id)
        existing = ws.get_synthesis("issue", issue_id)
        if existing is not None and existing.get("synthesized_from_marker") == marker:
            continue
        if existing is not None:
            new_items = _new_raw_items_since("issue", issue_id, existing.get("synthesized_from_marker"))
            if not _is_material(new_items):
                ws.touch_synthesis_marker("issue", issue_id, marker)
                skipped_immaterial += 1
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
    # oldest-marker first, so a big backlog doesn't starve entities that
    # have been waiting longest.
    stale.sort(key=lambda s: (s["previous_marker"] is not None, s["previous_marker"] or ""))

    if stats is not None:
        stats["skipped_immaterial"] = skipped_immaterial
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
