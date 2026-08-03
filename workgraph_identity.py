"""
workgraph_identity.py — identity formalization v0 (2026-08-03).

docs/design/CONFIDENCE_AND_IDENTITY_REDESIGN.md Section 3.3: "backfill,
not build" — this module MATERIALIZES anchors/containers from signals that
already work in production today (reference matching, party/company
relationship linking, thread-container exclusivity); it does not add any
new detection logic. No LLM calls, deterministic, idempotent (safe to
re-run - every write goes through an idempotent upsert/dedup path).

Anchors point at issues.id directly (TODAY's real entity) rather than a
not-yet-built work_objects table - a deliberate v0 simplification,
upgradeable later without changing this module's callers.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws
import workgraph_projects
import workgraph_signals

_CONTAINER_TYPE_BY_SOURCE = {
    "outlook_mail": "email_conversation",
    "teams_chat": "teams_chat",
    "calendar": "calendar_series",
    "sharepoint": "sharepoint_item",
}


def _container_key_quality(source: str, thread_key_source: str | None) -> str:
    if source == "calendar":
        return "exact" if thread_key_source == "graph_series_master_id" else "heuristic"
    return "exact"


def backfill_identity_anchors(now: float | None = None) -> dict:
    """One mechanical pass over every current issue's real raw_items/
    parties, materializing:
    - a source_container row per distinct (source, thread_key) the issue's
      raw_items actually carry (from thread_key + source + thread_key_source,
      not a live rescan of any external system);
    - a 'reference' identity_anchor (exclusive, strong) per real PR/PO base
      already on the issue (reuses reference_base_ids_for_issue - the exact
      set the live grouping/veto logic already trusts);
    - 'party'/'company' identity_anchors (non-exclusive, weak) per real
      external, non-automated relationship already on the issue (reuses
      the same is_automated_sender filter the live matching functions use).

    Idempotent and safe to re-run: containers upsert on their UNIQUE key;
    anchors dedupe against an already-active identical (type, value, issue)
    row. An exclusive anchor already active on a DIFFERENT issue (a real,
    pre-existing fragmentation/collision case) is skipped and counted, never
    an error - see create_identity_anchor's own docstring.

    Returns a small reconciliation-style summary, not a live report."""
    now = now if now is not None else time.time()
    issues = ws.list_issues(states=None, limit=10000)

    containers_written = 0
    anchors_written = 0
    anchor_conflicts = []  # [{issue_id, anchor_type, normalized_value, held_by}]

    for issue in issues:
        issue_id = issue["id"]
        raw_items = ws.get_raw_items_for_issue(issue_id)

        seen_container_keys = set()
        for ri in raw_items:
            source = ri.get("source")
            thread_key = ri.get("thread_key")
            container_type = _CONTAINER_TYPE_BY_SOURCE.get(source)
            if not container_type or not thread_key or (source, thread_key) in seen_container_keys:
                continue
            seen_container_keys.add((source, thread_key))
            container_id = f"sc-{source}-{thread_key}"
            ws.upsert_source_container(
                id=container_id, source=source, container_type=container_type,
                exact_key=thread_key, key_quality=_container_key_quality(source, ri.get("thread_key_source")),
                issue_id=issue_id,
            )
            containers_written += 1

        for ref_base in workgraph_projects.reference_base_ids_for_issue(issue_id):
            result = ws.create_identity_anchor(
                anchor_type="reference", normalized_value=ref_base, anchor_strength="strong",
                exclusive=True, issue_id=issue_id, created_by="backfill", now=now,
            )
            if result is not None:
                anchors_written += 1
            else:
                existing = ws.list_identity_anchors(status="active")
                held_by = next((a["issue_id"] for a in existing
                                 if a["anchor_type"] == "reference" and a["normalized_value"] == ref_base), None)
                if held_by and held_by != issue_id:
                    anchor_conflicts.append({"issue_id": issue_id, "anchor_type": "reference",
                                              "normalized_value": ref_base, "held_by": held_by})

        for party in ws.list_parties_for_issue(issue_id):
            if party.get("affiliation") != "external" or workgraph_signals.is_automated_sender(party.get("primary_email") or ""):
                continue
            if ws.create_identity_anchor(
                anchor_type="party", normalized_value=party["id"], anchor_strength="weak",
                exclusive=False, issue_id=issue_id, created_by="backfill", now=now,
            ) is not None:
                anchors_written += 1
            company = party.get("company")
            if company and ws.create_identity_anchor(
                anchor_type="company", normalized_value=company.strip().lower(), anchor_strength="weak",
                exclusive=False, issue_id=issue_id, created_by="backfill", now=now,
            ) is not None:
                anchors_written += 1

    return {
        "issues_scanned": len(issues),
        "containers_written": containers_written,
        "anchors_written": anchors_written,
        "anchor_conflicts": anchor_conflicts,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(backfill_identity_anchors(), indent=2))
