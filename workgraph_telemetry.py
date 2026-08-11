"""Accuracy telemetry (task #304, item #4, 2026-08-11, Marc's own build
authorization: "Accuracy telemetry, done alongside 1-3, not after - it's
the only way to actually know whether any of the above is working, it's
purely additive (doesn't touch the sensitive logic itself), and it would
have caught today's health-check blind spot pattern too.")

Every number here is read-only, computed fresh from EXISTING audit trails
(audit_log, identity_constraints, claim_events, raw_item_extractions) - no
new write path into grouping/extraction decisions, no new table this
module owns, nothing here can ever affect what pipeline2.py/workgraph_
projects.py/workgraph_claims.py actually decide.

Honest, deliberate scope limits per metric - a proxy that's real and
monitored over time beats a precise number that doesn't exist yet:

  false_merge_correction_rate: split_issue_from_project (Marc's own "this
    grouping was wrong, undo it" safety valve, task #178) is the ONLY
    producer of identity_constraints.constraint_type='cannot_merge' rows,
    so that count is a clean, unambiguous false-merge-CORRECTION signal.
    The denominator (merge_events) counts every merge-shaped audit_log
    row in the same window - a real proxy, not a perfect one: it can't
    perfectly disambiguate a genuine grouping merge from a rare manual
    reassignment sharing the same audit_log shape, and there's no shared
    key linking a specific split back to the specific merge it corrects,
    so this is a WINDOW-LEVEL rate, not a per-merge outcome.
    A rate near 0 across a growing merge count is the real "is this
    working" signal Marc asked for; a rising rate is the alarm.

  false_split_catches: the only clean, unambiguous "should have been one"
    signal available today is audit_log.field='absorbed_cluster' -
    written exclusively by workgraph_reconcile.py's two narrow stray-
    cluster sweeps (merge_stray_same_reference_clusters/merge_stray_
    signature_confirmation_clusters). Reported as a raw COUNT, not a
    rate - it is a detected-and-fixed LOWER BOUND on true false-splits,
    not the true count: a missed grouping outside those two sweeps'
    narrow patterns (shared pr_number_base, or signature-confirmation
    participant+filename overlap) is invisible here, the same honest gap
    those sweeps' own docstrings admit. Deliberately does NOT use
    workgraph_relationships.list_relationships_needing_review's count as
    a false-split proxy - that measures projects correctly kept separate
    while sharing a real relationship, the OPPOSITE signal, and using it
    here would conflate the two.

  claim_correction_rate: reconcile_extraction_claims is the only writer
    of claim_events rows whose note reads exactly "added by a corrected
    extraction" (workgraph_claims._reconcile_extraction_correction's
    real correction path) - a clean, unambiguous numerator. Denominator
    is every raw_item_extractions row ever materialized (materialized_
    hash IS NOT NULL) - the full population an extraction could ever
    have been corrected against, not windowed the same way the numerator
    is, since a correction can land long after the original extraction.

Deliberately NOT a live dashboard/UI - wired into health_check.py's
existing daily report (check_accuracy_telemetry) exactly because that's
the mechanism Marc pointed to ("today's health-check blind spot pattern")
- a periodic, always-ok observability check, never a pass/fail gate."""
from __future__ import annotations

import workgraph_store as ws


def compute_accuracy_metrics(*, window_start: float, window_end: float) -> dict:
    merge_events = ws.count_merge_events(window_start, window_end)
    false_merge_corrections = ws.count_identity_constraints("cannot_merge", window_start, window_end)
    false_split_catches = ws.count_audit_log_by_field("absorbed_cluster", window_start, window_end)
    claim_corrections = ws.count_claim_correction_events(window_start, window_end)
    materialized_extractions_total = ws.count_materialized_extractions()

    false_merge_correction_rate = (
        false_merge_corrections / merge_events if merge_events > 0 else None
    )
    claim_correction_rate = (
        claim_corrections / materialized_extractions_total if materialized_extractions_total > 0 else None
    )

    return {
        "window_start": window_start,
        "window_end": window_end,
        "merge_events": merge_events,
        "false_merge_corrections": false_merge_corrections,
        "false_merge_correction_rate": false_merge_correction_rate,
        "false_split_catches": false_split_catches,
        "claim_corrections_in_window": claim_corrections,
        "materialized_extractions_total": materialized_extractions_total,
        "claim_correction_rate": claim_correction_rate,
    }
