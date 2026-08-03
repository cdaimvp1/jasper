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
import workgraph_sessionize

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
      the same is_automated_sender filter the live matching functions use);
    - Teams sub-session boundaries (workgraph_sessionize.py) for every
      distinct teams_chat container touched - additive/observe-only, not
      yet consulted by the live classify/grouping path (see this module's
      own module docstring for why that's a deliberate, separate, reviewed
      step, not an automatic wire-in).

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
    teams_chat_keys = set()

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
            if source == "teams_chat":
                teams_chat_keys.add(thread_key)
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

    sessions_written = 0
    multi_session_containers = []  # [{thread_key, session_count}] - real splits found
    for thread_key in teams_chat_keys:
        messages = ws.list_raw_items_by_thread_key("teams_chat", thread_key)
        sessioned = workgraph_sessionize.sessionize_teams_messages(messages)
        container_id = f"sc-teams_chat-{thread_key}"
        by_session = {}
        for msg in sessioned:
            by_session.setdefault(msg["session_sequence"], []).append(msg)
        for seq, msgs in by_session.items():
            ws.upsert_source_session(
                id=f"{container_id}-s{seq}", source_container_id=container_id, session_sequence=seq,
                started_ts=msgs[0]["occurred_ts"], ended_ts=msgs[-1]["occurred_ts"],
                boundary_reason=msgs[0]["boundary_reason"] or "first_message",
            )
            sessions_written += 1
        if len(by_session) > 1:
            multi_session_containers.append({"thread_key": thread_key, "session_count": len(by_session)})

    return {
        "issues_scanned": len(issues),
        "containers_written": containers_written,
        "anchors_written": anchors_written,
        "anchor_conflicts": anchor_conflicts,
        "teams_sessions_written": sessions_written,
        "teams_containers_with_multiple_sessions": multi_session_containers,
    }


def run_backfill_daily_if_due(now: float | None = None) -> dict | None:
    """Same once-a-day gate as retention/health_check/suggestion_expiry/
    choice_log_expiry (ws.claim_daily_run) - piggybacks scheduled_refresh.
    py's 5x/day cycle. This is what keeps identity_anchors/source_
    containers from going stale for issues created or updated AFTER the
    one-time backfill ran - nothing in the live classify/grouping path
    writes anchors inline (deliberately: this stays a periodic maintenance
    sweep over the real signals, not a hot-path write, same discipline as
    every other daily gate in this codebase). Returns None on every call
    that isn't the day's first claim."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not ws.claim_daily_run("identity_anchor_backfill", today):
        return None
    return backfill_identity_anchors(now=now)


if __name__ == "__main__":
    import json
    print(json.dumps(backfill_identity_anchors(), indent=2))
