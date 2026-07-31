"""
remediate_calendar_series.py - one-off retroactive remediation (step 7 of
the meeting-grouping/related-project identity design pass,
C:\\Users\\lane_marc@lilly.com\\.claude\\plans\\hidden-sniffing-dewdrop.md).

The step 3 fix (ingest/normalize.py's _calendar_series_key) only changes
thread_key derivation for FUTURE calendar ingestion - it does not
retroactively merge Issues that were already created, fragmented, before
that fix existed. Real production case this exists to fix: 7+ separate
Issues for one real recurring "DROP-IN HOURS" meeting series (each
occurrence got its own thread_key = its own event id, under the pre-fix
logic), several of which also ended up scattered across different
projects (proj-007/proj-013) alongside an unrelated "AI Model Weekly"
series and an unrelated governance issue.

This is a STANDALONE script, not folded into the routine backfill_reclassify
mechanism, because it's materially more invasive: it reassigns *issue*
identity itself (not just project membership) across every FK table that
references issue_id - raw_items, thread_map, evidence, work_tasks,
issue_parties, issue_state_history, nba_choice_log, shadow_grouping_log
(synthesis is left alone - see EXECUTE_GROUP's own comment).

SAFE BY DEFAULT: running this file with no arguments only ever finds and
PRINTS proposed consolidation groups - it never writes anything. Writing
requires an explicit --execute flag PLUS either --group N (one specific
group, by its printed index) or --all (every group found) - matching the
plan's own "dry-run first, then per-group or blanket confirmation" design,
with the granularity choice left to whoever runs this, not decided here.

Usage:
    python remediate_calendar_series.py                  # dry run only
    python remediate_calendar_series.py --execute --group 0
    python remediate_calendar_series.py --execute --all
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws

_FK_TABLES_ISSUE_ID = (
    "raw_items", "evidence", "work_tasks", "issue_state_history",
    "nba_choice_log", "shadow_grouping_log",
)


def _calendar_series_key_for_row(subject: str, organizer: str, occurred_ts: float) -> str:
    """Same synthetic-key shape as ingest/normalize.py's _calendar_series_
    key (the "no seriesMasterId" branch - confirmed the ONLY branch that
    ever actually fires against real captured Graph payloads this session).
    Recomputed from what's already stored on the raw_items row (subject,
    from_actor as organizer, occurred_ts as epoch) rather than re-deriving
    from the original Graph payload, which isn't preserved verbatim in the
    DB - this script only needs the SAME grouping key, not a byte-identical
    replay."""
    hhmm = time.strftime("%H:%M", time.gmtime(occurred_ts))
    norm_subject = ws.normalize_topic_key(subject or "")
    return f"synth:{(organizer or '').lower()}|{norm_subject}|{hhmm}"


def find_consolidation_groups() -> list[dict]:
    """Read-only. Groups every real calendar raw_item by the series key it
    SHOULD have under the step-3 fix, then keeps only groups spanning 2+
    DISTINCT already-created issues (a group of 1 issue needs no
    consolidation - it was already correctly a single Issue, whether by
    luck or a real thread_key match). Returns one dict per group:
    {"series_key", "winner", "losers", "loser_members_by_issue"} - winner
    is the earliest-opened issue (the most natural "original" identity for
    a long-running recurring series)."""
    rows = ws.get_calendar_raw_items_for_remediation()
    by_key: dict[str, set] = {}
    for r in rows:
        if not r["issue_id"]:
            continue
        key = _calendar_series_key_for_row(r["subject"], r["from_actor"], r["occurred_ts"])
        by_key.setdefault(key, set()).add(r["issue_id"])

    groups = []
    for key, issue_ids in by_key.items():
        if len(issue_ids) < 2:
            continue
        issues = [ws.get_issue(iid) for iid in issue_ids]
        issues = [i for i in issues if i is not None]
        issues.sort(key=lambda i: i.get("opened_at") or 0)
        winner, losers = issues[0], issues[1:]
        groups.append({
            "series_key": key,
            "winner": winner,
            "losers": losers,
        })
    groups.sort(key=lambda g: g["series_key"])
    return groups


def print_dry_run(groups: list[dict]) -> None:
    if not groups:
        print("No consolidation groups found - nothing to remediate.")
        return
    print(f"Found {len(groups)} consolidation group(s):\n")
    for idx, g in enumerate(groups):
        print(f"[{idx}] series_key = {g['series_key']}")
        w = g["winner"]
        print(f"    WINNER (kept, earliest-opened): {w['id']} - {w['title']!r} (state={w['state']}, project={w.get('project_id')})")
        for loser in g["losers"]:
            print(f"    loser (would be archived + reassigned): {loser['id']} - {loser['title']!r} "
                  f"(state={loser['state']}, project={loser.get('project_id')})")
        print()


def execute_group(group: dict) -> dict:
    """Actually consolidates ONE group - all-or-nothing transaction, same
    BEGIN IMMEDIATE/COMMIT/ROLLBACK pattern as merge_issues_txn (see that
    function's own docstring for why this repo uses that pattern rather
    than the module's public autocommit helpers for a multi-step write).
    Every loser's real FK rows are repointed to the winner; issue_parties
    is repointed row-by-row with an existing-party check first (its PK is
    (issue_id, party_id) - the same party could already be linked to both
    winner and loser). synthesis rows are deliberately left alone (orphaned
    but harmless - never hard-deleted, matching this codebase's own
    never-silently-drop convention; nothing queries a synthesis row by an
    archived issue's id in practice)."""
    winner_id = group["winner"]["id"]
    loser_ids = [l["id"] for l in group["losers"]]
    now = time.time()
    result = {"winner": winner_id, "losers_merged": [], "errors": []}

    for loser_id in loser_ids:
        try:
            ws.remediate_merge_issue_identity(winner_id, loser_id, reason_label="calendar-series remediation (step 7)")
            result["losers_merged"].append(loser_id)
        except Exception as e:
            result["errors"].append({"loser_id": loser_id, "error": str(e)})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually write changes (default: dry run only)")
    parser.add_argument("--group", type=int, default=None, help="Execute only this group index (from the dry-run listing)")
    parser.add_argument("--all", action="store_true", help="Execute every group found")
    args = parser.parse_args()

    groups = find_consolidation_groups()
    print_dry_run(groups)

    if not args.execute:
        print("Dry run only - nothing written. Re-run with --execute --group N or --execute --all to apply.")
        return
    if args.group is None and not args.all:
        print("--execute requires --group N or --all - refusing to guess which group(s) to apply.")
        return

    targets = groups if args.all else [groups[args.group]]
    for g in targets:
        result = execute_group(g)
        print(f"Executed {g['series_key']}: {result}")


if __name__ == "__main__":
    main()
