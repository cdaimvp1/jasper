"""
workgraph_reconcile.py — claim-resolution suggestions (2026-08-04, P1,
architecture-review follow-up, task #155).

The claims ledger materializes and dedupes correctly now (workgraph_
claims.py), but nothing in it ever proposes that an open claim IS
resolved - a checklist item only ever closes via a direct human action
(sync_checklist_action_to_claim). This module is a suggest-only layer on
top of that: it never changes a claim's status itself except through an
EXPLICIT human confirm (confirm_claim_suggestion) - every suggestion sits
in pending_claim_suggestions until reviewed, same shape as
pending_project_suggestions' merge/link queue.

Deliberately exactly two evidence types, both a closed enum on the
pending_claim_suggestions.evidence_type CHECK constraint - no fuzzy/
heuristic "this claim is probably done" scoring of any kind:

  explicit_resolution_signal (suggestion_kind='resolve') - the SAME
    curator-extraction step that already produces repeat_signals
    (SYNTHESIS_ROUTINE.md) also produces resolution_signals: populated
    ONLY when a raw_item's own content directly and unambiguously states
    that a SPECIFIC earlier open claim on this issue was fulfilled - "the
    signed SOW you asked for is attached," a clear "Approved." reply to a
    named ask. Never a guess, same discipline repeat_signals already
    established. generate_resolution_signal_suggestions turns each into
    one suggestion against the exact open claim it names (matched by
    byte-exact text, same find_open_claim_by_text used throughout the
    claims ledger - no fuzzy pairing here either).

  issue_closed_with_open_claims (suggestion_kind='contradiction') - a
    deliberate CONTRADICTION signal, never a completion inference: an
    issue moving to a closed state (done/dismissed/noise-archived) is
    NEVER treated as evidence that its still-open claims got resolved (a
    real human decision to close an issue can easily leave loose ends,
    and inferring completion from it would silently mark real
    outstanding asks/commitments as done with no evidence they actually
    happened). detect_issue_closed_with_open_claims_contradictions
    surfaces the mismatch instead, for a human to reconcile - confirming
    this kind of suggestion only acknowledges the mismatch; it never
    touches the claim.

Both paths dedupe against any existing PENDING suggestion for the same
(claim_id, evidence_type) pair (workgraph_store.create_claim_suggestion) -
re-running either sweep never produces a duplicate pending suggestion for
a claim already flagged.
"""
from __future__ import annotations

from typing import Optional

import workgraph_classify
import workgraph_store as ws

_RESOLUTION_SIGNAL_CLAIM_TYPES = ("ask", "decision", "commitment")

_ISSUE_CLOSED_STATES = ("done", "dismissed", "noise-archived")


def generate_resolution_signal_suggestions(raw_item_id: int) -> int:
    """Reads this raw_item's extraction for resolution_signals (curator-
    judged, same shape/discipline as repeat_signals) and turns each into
    a suggest-only 'resolve' suggestion against the specific open claim
    it names. A signal that doesn't match a currently-open claim (already
    resolved by the time this runs, or a bad match) is silently skipped -
    never an error, since there's nothing to suggest. Returns the number
    of signals that matched a real open claim (a signal that hits an
    already-pending duplicate still counts - the suggestion queue is
    correct either way, this return value is a processing count, not a
    strict new-rows count)."""
    raw_item = ws.get_raw_item(raw_item_id)
    if not raw_item or not raw_item.get("issue_id"):
        return 0
    issue_id = raw_item["issue_id"]

    extraction = ws.get_extraction(raw_item_id)
    if not extraction:
        return 0
    blob = extraction.get("extracted_json") or {}
    signals = blob.get("resolution_signals")
    if not isinstance(signals, list):
        return 0

    matched = 0
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        claim_type = signal.get("claim_type")
        claim_text = signal.get("claim_text")
        if claim_type not in _RESOLUTION_SIGNAL_CLAIM_TYPES or not claim_text:
            continue
        claim = ws.find_open_claim_by_text(issue_id, claim_type, claim_text)
        if claim is None:
            continue
        ws.create_claim_suggestion(
            claim_id=claim["id"], suggestion_kind="resolve",
            evidence_type="explicit_resolution_signal",
            evidence_note=signal.get("resolution_note"), raw_item_id=raw_item_id,
        )
        matched += 1
    return matched


def detect_issue_closed_with_open_claims_contradictions() -> dict:
    """Batched sweep - one query for closed issue ids, one batched query
    for their open claims (list_open_claims_for_issues), never a
    per-issue query in a loop. Safe to re-run: dedup is per (claim_id,
    evidence_type), so an issue that's been closed for weeks with the
    same still-open claim only ever accumulates ONE pending
    suggestion."""
    closed_issue_ids = ws.list_issue_ids_by_state(list(_ISSUE_CLOSED_STATES))
    if not closed_issue_ids:
        return {"issues_scanned": 0, "suggestions_created": 0}
    open_by_issue = ws.list_open_claims_for_issues(closed_issue_ids)
    flagged = 0
    for issue_id, claims in open_by_issue.items():
        for claim in claims:
            ws.create_claim_suggestion(
                claim_id=claim["id"], suggestion_kind="contradiction",
                evidence_type="issue_closed_with_open_claims",
                evidence_note=f"issue {issue_id} is closed but this claim is still open",
            )
            flagged += 1
    return {"issues_scanned": len(closed_issue_ids), "suggestions_created": flagged}


_ISSUE_OPEN_STATES = ("active", "waiting")


def detect_issues_appear_resolved_but_still_open() -> dict:
    """Task #273 (the real Kinaxis bug) - the mirror direction of
    detect_issue_closed_with_open_claims_contradictions above: an issue
    still sitting in an OPEN state (active/waiting) whose own claims are
    ALL resolved (none left with status='open') - the structural signal
    that a project's synthesis narrative can say "closed" while the
    issue/claim layer never got updated to match, with nothing
    reconciling the mismatch. Requires at least one claim to exist at all
    (an issue with zero claims yet isn't "resolved," it's simply
    unprocessed - a materially different, unrelated case this sweep must
    not flag). Deliberately never touches issue.state itself - suggest-
    only, same discipline as every other mechanism in this module; a
    human confirms the actual close via the normal /status route.

    Batched, same discipline as its sibling above: one query for open
    issue ids, one batched query for ALL their claims (list_claims_for_
    issues), never a per-issue loop. Safe to re-run: create_issue_state_
    suggestion dedupes on (issue_id, status='pending')."""
    open_issue_ids = ws.list_issue_ids_by_state(list(_ISSUE_OPEN_STATES))
    if not open_issue_ids:
        return {"issues_scanned": 0, "suggestions_created": 0}
    claims_by_issue = ws.list_claims_for_issues(open_issue_ids)
    flagged = 0
    for issue_id, claims in claims_by_issue.items():
        if not claims:
            continue
        if any(c["status"] == "open" for c in claims):
            continue
        ws.create_issue_state_suggestion(
            issue_id=issue_id,
            evidence_note=f"issue {issue_id} is still open but all {len(claims)} of its claims are resolved",
        )
        flagged += 1
    return {"issues_scanned": len(open_issue_ids), "suggestions_created": flagged}


def merge_stray_same_reference_clusters() -> dict:
    """Found root-causing a real live drift (2026-08-08, proj-009/Bluefish):
    curator's project synthesis said the SOW was "fully signed" and the PO
    "fully approved," but the underlying issue (marc-4196) stayed active
    with "your move - 3 open asks," untouched since a day before. Traced
    to a concrete data-linking gap, not a missing reconciliation layer: the
    Ariba "fully approved" notification for PR1189827 (a raw_item with the
    same deterministic pr_number_base as the issue's own "approval needed"
    request) had landed in its OWN separate, never-promoted raw cluster -
    formed hours before curator's later issue-extraction pass created the
    real issue from a sibling cluster, with nothing ever re-checking for
    that stray sibling afterward. The synthesis routine's own broader
    evidence read picked up the real approval and wrote an accurate
    narrative; the issue/claims layer, scoped to only the raw_items
    actually linked to it, never saw it.

    Unlike every other function in this module, this one ACTS directly
    rather than creating a suggestion - a shared pr_number_base across
    open work objects is exactly the deterministic, already-trusted
    category cluster_and_link's own ingest-time reference auto-attach
    treats as safe to merge on sight (task #13's split of "safe reference
    match" from "risky scored-model match"); this sweep is that same
    trust applied retroactively instead of only at ingest time. Still
    conservative: a pr_number_base group is only acted on when it resolves
    to EXACTLY ONE real issue among its members - zero real issues (only
    clusters, left for normal promotion) or two-or-more real issues
    (a different, riskier duplicate-ISSUE case, not this sweep's job) are
    both skipped rather than guessed at.

    After absorbing each stray cluster, re-derives the issue's state
    (workgraph_classify.recompute_issue_state) so a newly-visible closure
    signal (like ariba_pr_fully_approved) can actually take effect instead
    of sitting there unread until the next unrelated state change happens
    to trigger a recompute.

    Does NOT fix every version of this drift - the Bluefish SOW's own
    Adobe Sign "you signed" confirmation lives in a THIRD, still-separate,
    unmerged project with no pr_number_base of its own to match on (an
    Adobe Sign notification, not an Ariba one) - a harder, different
    identity-matching gap this sweep doesn't touch. Flagging that
    separately rather than overclaiming this closes the whole story."""
    groups = ws.list_pr_number_base_groups_spanning_multiple_open_work_objects()
    groups_checked = len(groups)
    clusters_absorbed = 0
    issues_recomputed = set()
    for pr_number_base, members in groups.items():
        real_issues = [m["id"] for m in members if not m["is_raw_cluster"]]
        clusters = [m["id"] for m in members if m["is_raw_cluster"]]
        if len(real_issues) != 1 or not clusters:
            continue
        issue_id = real_issues[0]
        for cluster_id in clusters:
            result = ws.absorb_stray_reference_cluster(cluster_id, issue_id, actor="system")
            if result["status"] == "absorbed":
                clusters_absorbed += 1
                issues_recomputed.add(issue_id)
    for issue_id in issues_recomputed:
        workgraph_classify.recompute_issue_state(issue_id)
    return {
        "pr_number_base_groups_checked": groups_checked,
        "clusters_absorbed": clusters_absorbed,
        "issues_recomputed": len(issues_recomputed),
    }


def list_pending_claim_suggestions_for_issue(issue_id: str) -> list[dict]:
    return ws.list_pending_claim_suggestions(issue_id=issue_id)


def confirm_claim_suggestion(suggestion_id: int, *, actor: str) -> bool:
    """The ONLY path in this module that changes a claim's status, and
    only on an explicit human confirm of a 'resolve' suggestion.
    Confirming a 'contradiction' suggestion just acknowledges the
    mismatch - it deliberately never marks the claim done, since an
    issue closing is not evidence the claim was actually fulfilled (see
    module docstring); a human who wants that claim closed does so
    through the normal checklist action instead. Returns False if the
    suggestion doesn't exist or was already resolved."""
    suggestion = ws.get_claim_suggestion(suggestion_id)
    if suggestion is None or suggestion["status"] != "pending":
        return False
    ws.resolve_claim_suggestion(suggestion_id, "confirmed")
    if suggestion["suggestion_kind"] == "resolve":
        ws.update_claim_status(suggestion["claim_id"], "done", actor=actor)
        ws.log_claim_event(
            suggestion["claim_id"], "complete", actor=actor,
            note="confirmed via claim-resolution suggestion",
            raw_item_id=suggestion.get("raw_item_id"),
        )
    return True


def reject_claim_suggestion(suggestion_id: int, *, actor: str) -> bool:
    suggestion = ws.get_claim_suggestion(suggestion_id)
    if suggestion is None or suggestion["status"] != "pending":
        return False
    ws.resolve_claim_suggestion(suggestion_id, "rejected")
    return True
