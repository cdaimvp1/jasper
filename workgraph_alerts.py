"""
workgraph_alerts.py — pure, deterministic alerts scanner. No LLM calls, no
clock reads beyond an explicit `now` passed in (same testability contract as
workgraph_nba.py).

Not a generic notification firehose — a short, curated surface for things
that need Marc's attention beyond what the issue list itself already shows.
Seven concrete kinds, each with its own pure `_evaluate_*` helper:

  stale                     — a waiting/blocked issue gone quiet too long.
  high_priority_ask         — a newly-classified ACTIONABLE-ASK on a high-priority issue.
  anomaly                   — an off-channel-style anomaly flagged during classification.
  stuck_action              — a pending_actions row that never resolved.
  unmet_prerequisite        — an Aristotle prerequisite (task #51) still unconfirmed
                               (task #55). Previously this only ever showed up buried in
                               nba_reason, seen only on the rare low-confidence "abstain"
                               card - this makes it visible for every issue, regardless
                               of confidence tier.
  reference_id_collision    — enhancement idea panel #14: a same-PR/PO pair across
                               two OPEN, not-yet-grouped issues, found by a DB-wide
                               sweep - unlike panel #2's per-issue lookup, this
                               surfaces a pair even when Marc never opens either
                               issue's own detail panel.
  conflicting_value_figures — enhancement idea panel #16: two or more messages
                               on the SAME open issue quoting different preferred-
                               tier dollar figures - workgraph_nba._extract_value_
                               amount silently picks the max and moves on; this
                               surfaces the disagreement itself.

Idempotent by construction: every alert kind is deduped against the set of
currently-undismissed alerts before `run()` creates anything, keyed by
(issue_id, kind) for issue-scoped kinds and by (kind, source_ref) for kinds
that key off a specific raw_item/pending_action/issue-pair instead. Once an
alert is dismissed, a genuinely new occurrence of the same condition is free
to raise a fresh one — dismissal means "seen", not "never alert on this
issue again".
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws
import workgraph_projects
import workgraph_nba

DAY = 86400.0

# MappingProxyType (fixed 2026-07-29): this dict is used directly as a mutable
# default argument on run() below - nothing mutates it today, but a caller doing
# `custom = DEFAULT_THRESHOLDS; custom["stale_warn_days"] = 3` (forgetting to
# .copy() first) would silently corrupt the shared default for every future call.
# A read-only view makes that TypeError immediately instead of silently wrong.
DEFAULT_THRESHOLDS = MappingProxyType({
    "stale_warn_days": 5,
    "stale_critical_days": 12,
    "stuck_action_minutes": 30,
})


def _last_activity_ts(issue: dict, evidence: list[dict], state_history: list[dict]) -> float:
    """Same staleness concept as cockpit.html's client-side staleness() — the
    max of an issue's evidence timestamps and state-history timestamps. Falls
    back to updated_at in the (shouldn't-happen) case both are empty."""
    candidates = [e["ts"] for e in evidence] + [h["changed_ts"] for h in state_history]
    return max(candidates) if candidates else issue["updated_at"]


def _evaluate_stale(issue: dict, now: float, last_activity_ts: float, thresholds: dict = DEFAULT_THRESHOLDS):
    """Pure. Returns (severity, summary) or None if not stale enough yet."""
    days = (now - last_activity_ts) / DAY
    if days < thresholds["stale_warn_days"]:
        return None
    severity = "critical" if days >= thresholds["stale_critical_days"] else "warn"
    verb = "Waiting" if issue["state"] == "waiting" else "Blocked"
    tail = "with no reply" if issue["state"] == "waiting" else "with no movement"
    summary = f"{verb} {int(days)}d {tail} — {issue['title']}"
    return severity, summary


def _evaluate_high_priority_ask(issue: dict):
    """Pure. Always fires (caller already filtered to ACTIONABLE-ASK + priority='high')."""
    return "warn", f"New high-priority ask needs a response — {issue['title']}"


def _evaluate_anomaly(raw_item: dict):
    """Pure. Always fires (caller already filtered to anomaly_flag=1)."""
    subject = raw_item.get("subject") or raw_item.get("body_preview") or "(no subject)"
    return "warn", f"Off-channel anomaly flagged — {subject}"


def _evaluate_stuck_action(pending: dict, now: float, thresholds: dict = DEFAULT_THRESHOLDS):
    """Pure. Returns (severity, summary) or None if not stuck long enough yet."""
    minutes = (now - pending["requested_ts"]) / 60.0
    if minutes < thresholds["stuck_action_minutes"]:
        return None
    summary = (f"{pending['action_kind']} action stuck {int(minutes)}m "
               f"({pending['status']}, worker {pending['worker']})")
    return "critical", summary


def _evaluate_unmet_prerequisite(issue: dict):
    """Pure. Always fires (caller already filtered to has_unmet_prerequisite=1).
    Re-uses the exact nba_reason text already computed by workgraph_nba.py -
    no second call into workgraph_aristotle.check_prerequisites()."""
    return "warn", issue.get("nba_reason") or f"Unmet prerequisite — {issue['title']}"


def _evaluate_reference_id_collision(pair: dict):
    """Pure. Always fires (caller already dropped same-project and closed-
    issue pairs)."""
    refs = ", ".join(pair["shared_reference_ids"])
    summary = f"Same reference ({refs}) on 2 open issues — {pair['title_a']} / {pair['title_b']}"
    return "warn", summary


def _evaluate_conflicting_value_figures(issue: dict, figures: list[dict]):
    """Pure. Always fires (caller already filtered to len(figures) >= 2)."""
    amounts = ", ".join(f"${f['amount']:,.0f}" for f in figures)
    summary = f"Different messages quote different totals ({amounts}) — {issue['title']}"
    return "warn", summary


def run(now: float | None = None, thresholds: dict = DEFAULT_THRESHOLDS) -> dict:
    """Scan all 4 conditions and create any new, non-duplicate alerts. Called
    right after workgraph_nba.recompute_all() everywhere that's called, so
    alerts always reflect the freshest priority_score/state."""
    if now is None:
        now = time.time()
    ws.init_workgraph()

    existing = ws.list_alerts(dismissed=False)
    existing_issue_kind = {(a["issue_id"], a["kind"]) for a in existing if a["issue_id"]}
    existing_source_ref = {(a["kind"], a["source_ref"]) for a in existing if a["source_ref"]}

    by_kind = {"stale": 0, "high_priority_ask": 0, "anomaly": 0, "stuck_action": 0,
               "unmet_prerequisite": 0, "reference_id_collision": 0, "conflicting_value_figures": 0}

    # 1. Stale waiting/blocked issues. Evidence + state history fetched ONCE for
    # the whole batch (fixed 2026-07-29: this used to be 2 queries per issue
    # inside the loop - up to ~2000 queries per tick at 1000 issues, every
    # periodic tick, even with zero new evidence).
    stale_candidates = ws.list_issues(states=["waiting", "blocked"], limit=1000)
    candidate_ids = [i["id"] for i in stale_candidates]
    evidence_by_issue = ws.list_evidence_for_issues(candidate_ids)
    history_by_issue = ws.list_issue_state_history_for_issues(candidate_ids)
    for issue in stale_candidates:
        if (issue["id"], "stale") in existing_issue_kind:
            continue
        evidence = evidence_by_issue.get(issue["id"], [])
        state_history = history_by_issue.get(issue["id"], [])
        last_ts = _last_activity_ts(issue, evidence, state_history)
        result = _evaluate_stale(issue, now, last_ts, thresholds)
        if result is None:
            continue
        severity, summary = result
        ws.create_alert(issue_id=issue["id"], kind="stale", severity=severity, summary=summary)
        existing_issue_kind.add((issue["id"], "stale"))
        by_kind["stale"] += 1

    # 2. Newly-classified high-priority actionable asks (dedup per issue, not
    #    per raw_item — one alert is enough even if several asks land on it).
    for item in ws.get_high_priority_actionable_items():
        issue_id = item.get("issue_id")
        if not issue_id or (issue_id, "high_priority_ask") in existing_issue_kind:
            continue
        issue = ws.get_issue(issue_id)
        if issue is None:
            continue
        severity, summary = _evaluate_high_priority_ask(issue)
        ws.create_alert(issue_id=issue_id, kind="high_priority_ask", severity=severity,
                         summary=summary, source_ref=str(item["id"]))
        existing_issue_kind.add((issue_id, "high_priority_ask"))
        by_kind["high_priority_ask"] += 1

    # 3. Anomaly-flagged items (not always issue-scoped, so dedup by source_ref).
    for item in ws.get_items_with_anomaly():
        source_ref = str(item["id"])
        if ("anomaly", source_ref) in existing_source_ref:
            continue
        severity, summary = _evaluate_anomaly(item)
        ws.create_alert(issue_id=item.get("issue_id"), kind="anomaly", severity=severity,
                         summary=summary, source_ref=source_ref)
        existing_source_ref.add(("anomaly", source_ref))
        by_kind["anomaly"] += 1

    # 4. Stuck pending actions (action-bridge requests that never resolved).
    for pending in ws.list_pending_actions():
        if pending["status"] not in ("requested", "in_progress"):
            continue
        source_ref = str(pending["id"])
        if ("stuck_action", source_ref) in existing_source_ref:
            continue
        result = _evaluate_stuck_action(pending, now, thresholds)
        if result is None:
            continue
        severity, summary = result
        ws.create_alert(issue_id=pending["issue_id"], kind="stuck_action", severity=severity,
                         summary=summary, source_ref=source_ref)
        existing_source_ref.add(("stuck_action", source_ref))
        by_kind["stuck_action"] += 1

    # 5. Unmet Aristotle prerequisites (task #55) - dedup per issue, same as
    #    high_priority_ask (one alert is enough even if it stays unmet across
    #    several ticks in a row).
    for issue in ws.list_issues_with_unmet_prerequisite():
        if (issue["id"], "unmet_prerequisite") in existing_issue_kind:
            continue
        severity, summary = _evaluate_unmet_prerequisite(issue)
        ws.create_alert(issue_id=issue["id"], kind="unmet_prerequisite", severity=severity, summary=summary)
        existing_issue_kind.add((issue["id"], "unmet_prerequisite"))
        by_kind["unmet_prerequisite"] += 1

    # 6. Cross-issue reference-ID collisions (enhancement idea panel #14) - a
    #    same-PR/PO pair Marc would otherwise only ever see if he happened to
    #    open one of the two issues' own detail panel (panel #2's on-demand
    #    lookup). Dedup keys off the canonical ISSUE PAIR, not issue_id alone -
    #    a single issue can legitimately collide with more than one other
    #    issue over time, and each is a distinct thing worth its own alert.
    for pair in workgraph_projects.find_all_reference_id_collisions():
        source_ref = f"{pair['issue_a']}:{pair['issue_b']}"
        if ("reference_id_collision", source_ref) in existing_source_ref:
            continue
        severity, summary = _evaluate_reference_id_collision(pair)
        ws.create_alert(issue_id=pair["issue_a"], kind="reference_id_collision", severity=severity,
                         summary=summary, source_ref=source_ref)
        existing_source_ref.add(("reference_id_collision", source_ref))
        by_kind["reference_id_collision"] += 1

    # 7. Conflicting dollar-figure flag across messages (enhancement idea
    #    panel #16) - 2+ raw_items on the SAME open issue disagreeing about
    #    a preferred-tier dollar figure. Batched: one raw_items fetch across
    #    every open issue, not one query per issue - same discipline as the
    #    stale-alert batching above.
    value_conflict_candidates = ws.list_issues(states=["active", "waiting", "blocked"], limit=1000)
    raw_items_for_value_conflict = ws.get_raw_items_for_issues([i["id"] for i in value_conflict_candidates])
    for issue in value_conflict_candidates:
        if (issue["id"], "conflicting_value_figures") in existing_issue_kind:
            continue
        figures = workgraph_nba.conflicting_value_figures_for_issue(
            raw_items_for_value_conflict.get(issue["id"], [])
        )
        if len(figures) < 2:
            continue
        severity, summary = _evaluate_conflicting_value_figures(issue, figures)
        ws.create_alert(issue_id=issue["id"], kind="conflicting_value_figures", severity=severity, summary=summary)
        existing_issue_kind.add((issue["id"], "conflicting_value_figures"))
        by_kind["conflicting_value_figures"] += 1

    return {"created": sum(by_kind.values()), "by_kind": by_kind}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
