"""
workgraph_self_audit.py — Jasper auditing its own representation of reality
(task #370, 2026-08-12).

WHAT THIS IS, AND ISN'T
------------------------
Every other periodic sweep in this codebase either watches the SYSTEM
(health_check.py: cursors advancing, disk growth, DB integrity, the pipeline
actually running) or ACTS on the graph (workgraph_lifecycle.py's dormant
sweep flips status; workgraph_noise.py's noise sweep flips status;
workgraph_relationships.py's sweeps write new Relationship rows). This module
does neither. It only asks: does Jasper's own DATA about the business make
internal sense right now? An active project nobody has touched in a month, a
"done" project with money still owed, two claims that can't both be true, a
"this was approved" notification with no matching ask on file, a "succeeded"
action with nothing to show for it, a row pointing at nothing, two
relationships that are secretly one company - none of these are system
failures. They're the graph disagreeing with itself. Strictly read-only:
nothing here ever changes a project/issue/claim/action/relationship - every
finding is surfaced for a human to look at, exactly the "notify, never
auto-correct" discipline Track B.8, the relationship-review queue, and
pending_claim_suggestions all already established. See each find_* function's
own docstring for its check's exact structural signal and honest scope
limits - none of these do NLP/semantic judgment; every one is a plain
structural/timestamp comparison over already-materialized fields.

THE SEVEN CHECKS
-----------------
  1. find_stale_active_projects              - active project, zero claim
     activity (any status) in the last 30 days.
  2. find_done_projects_with_open_commitments - done project, one or more
     open commitment claims still outstanding on a member issue/cluster.
  3. find_issues_with_contradictory_open_claims - an issue with 2+ open
     claims of the same type (commitment/decision) that share a real
     canonical_key (i.e. already recognized as restatements of the SAME
     underlying item) but name DIFFERENT real owners (marc vs.
     counterparty) at once. A deliberate, narrow structural proxy for
     "contradiction" - true NLP-level semantic contradiction detection is
     out of scope (that's claim_edges' edge_type='contradicts', whose real
     intended producer - Evidence Assembly's conflict detection - doesn't
     exist yet per that table's own schema comment in workgraph_store.py;
     this module doesn't add read-path plumbing for a column nothing has
     ever written, matching this codebase's own "don't build a consumer
     for a producer that doesn't exist yet" discipline). See this check's
     own function docstring for the FIRST, broader version of this
     heuristic (bare same-claim_type-plus-different-owner, no canonical_key
     requirement) that real-dev-DB testing caught flagging 123 ordinary
     bilateral-negotiation issues with zero actual contradictions -
     documented there rather than silently discarded.
  4. find_closure_signals_with_no_matching_request - a raw_item carrying a
     deterministic "closure" signal_type (workgraph_signals.classify_signal)
     whose issue has no raw_item whose OWN signal_type is the real request
     counterpart for it (workgraph_signals.REQUEST_TO_CLOSURE_SIGNAL) - the
     read-only, always-safe-to-run mirror of workgraph_claims_backfill.
     resolve_authoritative_closure_signals, which only ever ACTS on the one
     unambiguous case (exactly one correlated open claim) and is silent
     about every other shape. This surfaces those silently-skipped shapes
     instead of just letting them pass unseen.
  5. find_succeeded_actions_without_evidence - a prepared_action in
     state='succeeded' whose issue has no worker_action evidence row and no
     'output' attachment created since the action itself was created. Real,
     confirmed gap this check exists to catch: workgraph_proactive.
     dispatch_status_update_draft flips a prepared_action to 'succeeded'
     right after outlook_actions.draft_reply(save_only=True) with no
     evidence/attachment write of any kind - "succeeded" here is currently
     backed by nothing but the state string itself.
  6. find_orphaned_claims_and_evidence - a claims row whose issue_id, or an
     evidence_unit_links row whose work_object_id, resolves to no real
     work_objects row at all; or an evidence_units row linked to zero work
     objects. Never enforced by SQLite (foreign_keys is never turned on in
     workgraph_store.py) and genuinely reachable - see
     workgraph_store.list_orphaned_claims's own docstring for the exact
     unguarded DELETE path.
  7. find_duplicate_relationship_aliases - two ACTIVE relationships whose
     normalized_name values differ (so task #343's own UNIQUE index over
     normalized_name never caught them) but where the shorter one is a
     genuine prefix of the longer one (e.g. "sodalis" / "sodalis
     solutions") - deliberately prefix-only, not a fuzzy similarity score:
     company names are short enough that an edit-distance or substring-
     anywhere match risks real false positives ("Sodalis" vs. "Modalis"),
     the same "two independent, narrow, structural signals over one fuzzy
     score" discipline workgraph_reconcile.py's own filename-token matching
     already uses.

PERSISTENCE DECISION (the real judgment call this task asked for)
-------------------------------------------------------------------
Findings ARE persisted, to the new self_audit_findings table (see
workgraph_store.py's own schema comment for the table itself).

The two nearest existing precedents in this codebase - workgraph_
relationships.list_relationships_needing_review and workgraph_reconcile.
list_identity_conflicts_across_grouped_projects (Track B.8) - both compute
fresh, live, on every call, and explicitly document why that's fine: they
are PULL-ONLY, asked on demand via chat/MCP/an API route, with "nothing
stored, nothing that goes stale, nothing a human has to explicitly dismiss
to make it stop reappearing."

That reasoning does not transfer here. Task #370 requires this sweep be
wired into scheduled_refresh.py's unattended, periodic (5x/day) cadence -
nobody is necessarily looking at the moment it runs. A live-computed-only
design under THAT calling shape would recompute the identical seven
categories of findings every single cycle forever, with no way for a human
to ever say "I've seen this one, it's fine" and have that stick - exactly the
"reappear identically every run" failure mode this task's own brief called
out. That is a materially different problem from the two pull-only
precedents, and it's the same problem pending_claim_suggestions/
pending_issue_state_suggestions/capability_suggestions already solved for
their own periodic-sweep producers (workgraph_reconcile.py's own
detect_issue_closed_with_open_claims_contradictions et al. run daily too) -
so this module reuses THEIR shape (dedupe-then-insert-or-touch on a stable
key, a status column, an explicit human dismiss), not the two read-only
tools' shape.

Auto-resolution is bookkeeping-only, never data-correction: when a later
sweep no longer detects a still-open finding's exact condition, that
finding's own STATUS flips to 'resolved' (workgraph_store.
auto_resolve_missing_self_audit_findings) - nothing about the project/claim/
action/etc it pointed at is touched. This keeps the table from
accumulating permanently-stale rows for conditions that quietly fixed
themselves, without ever crossing into "the sweep corrected something" - it
only ever corrects ITS OWN prior finding about something.

WIRING
------
run_self_audit_sweep_daily_if_due() follows the exact same claim_daily_run
gate as workgraph_noise.run_noise_sweep_daily_if_due / workgraph_lifecycle.
run_dormant_sweep_daily_if_due - cheap and deterministic enough to run every
scheduled_refresh.py cycle, but daily is plenty for findings that describe
weeks/months-scale drift. Called from ingest/scheduled_refresh.py.

NOT built in this pass (explicit follow-ups, not silently dropped):
  - No new API route (server_lean.py untouched this round - another agent
    was concurrently working on that file). Once one exists, it should read
    workgraph_store.list_self_audit_findings/dismiss_self_audit_finding
    directly; no new logic would be needed in this module for that.
  - No UI surface - same deferred-until-the-mechanism-is-proven posture as
    every other review queue this table's shape was borrowed from.
"""
from __future__ import annotations

import json
import time
from typing import Optional

import workgraph_signals
import workgraph_store as ws

_STALE_PROJECT_CLAIM_WINDOW_SECONDS = 30 * 24 * 3600  # 30 days - task #370's own stated window
_CONTRADICTION_CLAIM_TYPES = ("commitment", "decision")
_REAL_OWNERS = ("marc", "counterparty")
_MIN_ALIAS_PREFIX_LEN = 4  # shorter than this, a shared prefix is too generic to trust on its own


def _project_member_ids(project_id: str) -> list[str]:
    """Same member resolution workgraph_noise.classify_project_noise /
    workgraph_lifecycle._last_evidence_ts_for_project already use (clusters
    + real issues) - a project's own evidence/claims live on its members,
    never directly on the project row itself."""
    return (
        [c["id"] for c in ws.list_clusters_for_project(project_id)]
        + [i["id"] for i in ws.list_issues_for_project(project_id)]
    )


# --- Check 1: active project, zero claim activity in the last 30 days ------

def find_stale_active_projects(*, now: Optional[float] = None,
                                window_seconds: int = _STALE_PROJECT_CLAIM_WINDOW_SECONDS) -> list[dict]:
    """Flags an 'active' project where no member issue/cluster has had a
    claim (any status - this is about real business activity being
    materialized, not specifically about open work) touched in the last 30
    days, including a project with zero claims ever. Deliberately NOT the
    same signal as workgraph_lifecycle's dormant sweep: that one compares
    raw evidence recency (raw_items.occurred_ts / claims.last_seen_ts) at a
    60-day threshold across active/waiting/blocked and actually MOVES
    status to 'dormant'. This is a narrower, claims-only, 30-day, read-only
    early-warning signal for 'active' specifically - a project could easily
    still be well within the 60-day dormant threshold (so lifecycle leaves
    it alone) while genuinely having gone quiet on the claims layer for a
    month, which is exactly the gap this check exists to surface for a
    human, not to act on."""
    now = now if now is not None else time.time()
    cutoff = now - window_seconds
    findings = []
    for project in ws.list_projects(status=["active"]):
        member_ids = _project_member_ids(project["id"])
        claims_by_member = ws.list_claims_for_issues(member_ids)
        last_seen_values = [
            c["last_seen_ts"] for claims in claims_by_member.values() for c in claims
            if c.get("last_seen_ts")
        ]
        most_recent = max(last_seen_values) if last_seen_values else None
        if most_recent is not None and most_recent >= cutoff:
            continue
        title = project.get("display_title") or project.get("name")
        findings.append({
            "dedupe_key": project["id"],
            "subject_type": "project",
            "subject_id": project["id"],
            "description": (
                f"active project {project['id']} ({title}) has "
                + ("no claims at all" if most_recent is None
                   else "no claim activity in the last 30 days")
            ),
            "detail": {"most_recent_claim_last_seen_ts": most_recent, "member_count": len(member_ids)},
        })
    return findings


# --- Check 2: done project, still-open commitments -------------------------

def find_done_projects_with_open_commitments() -> list[dict]:
    """Flags a 'done' project with 1+ OPEN claim_type='commitment' claims
    still outstanding on any member issue/cluster. Deliberately project-
    level and commitment-specific - NOT a duplicate of workgraph_reconcile.
    detect_issue_closed_with_open_claims_contradictions, which flags at the
    ISSUE level (any claim type) when an issue itself closes. A project can
    be marked done while its member issues are still sitting at active/
    waiting (project status and issue state are independent columns with no
    auto-cascade either direction) - that case is invisible to the issue-
    level check entirely, since none of those issues ever closed. This is
    the real, additive gap: money/deliverables still owed under a project
    Marc has already called finished."""
    findings = []
    for project in ws.list_projects(status=["done"]):
        member_ids = _project_member_ids(project["id"])
        if not member_ids:
            continue
        open_by_member = ws.list_open_claims_for_issues(member_ids, claim_type="commitment")
        open_commitments = [c for claims in open_by_member.values() for c in claims]
        if not open_commitments:
            continue
        title = project.get("display_title") or project.get("name")
        findings.append({
            "dedupe_key": project["id"],
            "subject_type": "project",
            "subject_id": project["id"],
            "description": (
                f"project {project['id']} ({title}) is marked done but still has "
                f"{len(open_commitments)} open commitment claim(s)"
            ),
            "detail": {"open_commitment_claim_ids": [c["id"] for c in open_commitments]},
        })
    return findings


# --- Check 3: contradictory current claims (structural owner-conflict) -----

def find_issues_with_contradictory_open_claims() -> list[dict]:
    """A deliberately narrow structural proxy for 'contradiction' - see this
    module's own docstring for why true semantic contradiction detection is
    out of scope.

    First cut of this check (kept here as a documented dead end, not
    silently rewritten) grouped OPEN commitment/decision claims by bare
    (issue_id, claim_type) and flagged any group naming 2+ different real
    owners. Tested against the live dev DB before shipping (task #370's own
    process step) and found to be badly miscalibrated: 123 issues flagged,
    and every sampled one turned out to be an ordinary bilateral
    negotiation thread with separate, entirely compatible commitments on
    each side ("Cori will set up the work order" / "Elizabeth sent the
    draft to Scriptly") - not a contradiction at all, just two different
    claims about two different things that happen to share a claim_type.
    Two open commitments on one issue is the NORMAL shape of active
    back-and-forth work, not a signal of anything wrong.

    Fixed by requiring the claims be recognized as the SAME underlying
    item in the first place: only claims that share a real, non-null
    canonical_key (workgraph_claims.canonical_key_for_claim - the exact
    signal this codebase already trusts to say "this is the same ask/
    commitment restated," used today for near-duplicate dedup) are grouped
    together at all. A shared canonical_key with two different owner
    values is a real, narrow, reviewable signal: the SAME tracked item now
    has two live claims disagreeing about who owns it. A canonical_key of
    None ("no definitive reference AND normalized text below the trust
    bar" per that function's own docstring) is never grouped on - two
    unrelated claims coincidentally both lacking one must not collide."""
    groups: dict[tuple, list[dict]] = {}
    for claim in ws.list_all_open_claims():
        if claim["claim_type"] not in _CONTRADICTION_CLAIM_TYPES:
            continue
        if claim.get("owner") not in _REAL_OWNERS:
            continue
        canonical_key = claim.get("canonical_key")
        if not canonical_key:
            continue
        groups.setdefault((claim["issue_id"], claim["claim_type"], canonical_key), []).append(claim)

    findings = []
    for (issue_id, claim_type, canonical_key), claims in groups.items():
        owners = sorted({c["owner"] for c in claims})
        if len(owners) < 2:
            continue
        findings.append({
            "dedupe_key": f"{issue_id}:{claim_type}:{canonical_key}",
            "subject_type": "issue",
            "subject_id": issue_id,
            "description": (
                f"issue {issue_id} has {len(claims)} open {claim_type} claims recognized as the same "
                f"underlying item (canonical_key={canonical_key!r}) but naming different owners "
                f"({', '.join(owners)})"
            ),
            "detail": {"claim_ids": [c["id"] for c in claims], "owners": owners, "canonical_key": canonical_key},
        })
    return findings


# --- Check 4: closure signal with no matching open request -----------------

def find_closure_signals_with_no_matching_request() -> list[dict]:
    """Read-only mirror of workgraph_claims_backfill.
    resolve_authoritative_closure_signals - that function only ever ACTS on
    the single unambiguous shape (exactly one open ask/commitment claim,
    correlated via workgraph_signals.REQUEST_TO_CLOSURE_SIGNAL) and is
    silently quiet about every other raw_item it scans (skipped_no_issue_id/
    skipped_ambiguous/skipped_uncorrelated in its own return dict, never
    surfaced anywhere per-item). This checks, per closure-treatment
    raw_item, whether its issue has ANY sibling raw_item at all whose OWN
    signal_type is the real request counterpart for this specific closure
    type - not just whether exactly one OPEN claim currently correlates
    (that would wrongly flag an already-successfully-auto-resolved case,
    where the claim is correctly 'done' by the time this runs). A raw_item
    with no issue_id at all (never even linked) is its own, simpler finding
    shape."""
    closure_types = [
        t for t in workgraph_signals.known_signal_types()
        if workgraph_signals.treatment_for_signal_type(t) == "closure"
    ]
    raw_item_ids = ws.list_raw_item_ids_with_signal_type_in(closure_types)
    findings = []
    for rid in raw_item_ids:
        raw_item = ws.get_raw_item(rid)
        if not raw_item:
            continue
        closure_signal_type = raw_item.get("signal_type")
        issue_id = raw_item.get("issue_id")
        if not issue_id:
            findings.append({
                "dedupe_key": str(rid),
                "subject_type": "raw_item",
                "subject_id": str(rid),
                "description": (
                    f"raw_item {rid} carries a closure signal ({closure_signal_type}) but was never "
                    f"linked to any issue/cluster at all"
                ),
                "detail": {"signal_type": closure_signal_type, "issue_id": None},
            })
            continue
        sibling_items = ws.get_raw_items_for_issue(issue_id)
        has_matching_request = any(
            workgraph_signals.REQUEST_TO_CLOSURE_SIGNAL.get(item.get("signal_type")) == closure_signal_type
            for item in sibling_items
        )
        if has_matching_request:
            continue
        findings.append({
            "dedupe_key": str(rid),
            "subject_type": "raw_item",
            "subject_id": str(rid),
            "description": (
                f"raw_item {rid} on issue {issue_id} carries a closure signal ({closure_signal_type}) "
                f"but that issue has no raw_item whose own signal_type is the real request counterpart "
                f"for it"
            ),
            "detail": {"signal_type": closure_signal_type, "issue_id": issue_id},
        })
    return findings


# --- Check 5: 'succeeded' action with no artifact/evidence backing it ------

def _issue_id_for_prepared_action(action: dict) -> Optional[str]:
    """Resolves the issue a prepared_action is really about, the same two
    real shapes every dispatcher in this codebase uses: via its claim
    (claim_id -> claims.issue_id) when it has one, or via its own
    proposed_parameters JSON's "issue_id" key (the shape workgraph_
    proactive.py's dispatch_* functions write for claim_id=None actions)
    when it doesn't. Returns None - never a guess - when neither resolves;
    callers must treat that as 'cannot verify', not as a finding either
    way."""
    if action.get("claim_id"):
        claim = ws.get_claim(action["claim_id"])
        if claim and claim.get("issue_id"):
            return claim["issue_id"]
    try:
        params = json.loads(action.get("proposed_parameters") or "{}")
    except (ValueError, TypeError):
        params = {}
    return params.get("issue_id") if isinstance(params, dict) else None


def find_succeeded_actions_without_evidence(*, now: Optional[float] = None) -> list[dict]:
    """Real, confirmed gap this check exists to catch: workgraph_proactive.
    dispatch_status_update_draft flips a prepared_action to state=
    'succeeded' immediately after outlook_actions.draft_reply(save_only=
    True) returns, with no evidence row and no attachment ever written for
    that draft - 'succeeded' today is backed by nothing but the state
    string itself for that path. Checks, for every action in state=
    'succeeded' whose issue is resolvable, whether the issue has picked up
    ANY worker_action evidence row OR any 'output' attachment with a
    timestamp at or after the action's own created_ts - either is treated
    as 'backed'; this deliberately doesn't try to prove the SPECIFIC
    artifact came from THIS action (no reliable link exists for that), only
    that something showed up since. An action whose issue can't be resolved
    at all (see _issue_id_for_prepared_action) is skipped, never guessed at
    either way."""
    now = now if now is not None else time.time()
    findings = []
    for action in ws.list_prepared_actions_by_state(["succeeded"]):
        issue_id = _issue_id_for_prepared_action(action)
        if not issue_id:
            continue
        created_ts = action.get("created_ts") or 0.0
        has_worker_action_evidence = any(
            e.get("type") == "worker_action" and (e.get("ts") or 0) >= created_ts
            for e in ws.list_evidence(issue_id)
        )
        has_output_attachment = any(
            a.get("kind") == "output" and (a.get("uploaded_ts") or 0) >= created_ts
            for a in ws.list_attachments_for_issue(issue_id)
        )
        if has_worker_action_evidence or has_output_attachment:
            continue
        findings.append({
            "dedupe_key": str(action["id"]),
            "subject_type": "prepared_action",
            "subject_id": str(action["id"]),
            "description": (
                f"prepared_action {action['id']} ({action['action_type']}) on issue {issue_id} is "
                f"marked succeeded but has no worker_action evidence or output attachment since it "
                f"was created"
            ),
            "detail": {"action_type": action["action_type"], "issue_id": issue_id, "created_ts": created_ts},
        })
    return findings


# --- Check 6: orphaned claim or evidence row --------------------------------

def find_orphaned_claims_and_evidence() -> list[dict]:
    """See workgraph_store.list_orphaned_claims/list_orphaned_evidence's own
    docstrings for exactly what each orphan shape means and the real,
    unguarded DELETE path that makes them structurally reachable (foreign
    keys are never enforced anywhere in this database)."""
    findings = []
    for claim in ws.list_orphaned_claims():
        findings.append({
            "dedupe_key": f"claim:{claim['id']}",
            "subject_type": "claim",
            "subject_id": str(claim["id"]),
            "description": (
                f"claim {claim['id']} ({claim['claim_type']}) points at issue_id "
                f"{claim['issue_id']!r}, which no longer resolves to any real work_object"
            ),
            "detail": {"issue_id": claim["issue_id"], "claim_type": claim["claim_type"]},
        })

    orphaned_evidence = ws.list_orphaned_evidence()
    for eu in orphaned_evidence["unlinked_evidence_units"]:
        findings.append({
            "dedupe_key": f"evidence_unit:{eu['id']}",
            "subject_type": "evidence_unit",
            "subject_id": str(eu["id"]),
            "description": f"evidence_unit {eu['id']} ({eu['type']}) is linked to zero work objects",
            "detail": {"raw_item_id": eu.get("raw_item_id"), "type": eu["type"]},
        })
    for link in orphaned_evidence["dangling_links"]:
        findings.append({
            "dedupe_key": f"evidence_link:{link['evidence_unit_id']}:{link['work_object_id']}",
            "subject_type": "evidence_unit_link",
            "subject_id": str(link["evidence_unit_id"]),
            "description": (
                f"evidence_unit {link['evidence_unit_id']} is linked to work_object "
                f"{link['work_object_id']!r}, which no longer exists"
            ),
            "detail": {"work_object_id": link["work_object_id"]},
        })
    return findings


# --- Check 7: duplicate relationship aliases --------------------------------

def find_duplicate_relationship_aliases() -> list[dict]:
    """Two ACTIVE relationships whose normalized_name values genuinely
    differ (task #343's own UNIQUE index over relationships.normalized_name
    already guarantees no two active rows share the IDENTICAL normalized
    name, so this is only ever comparing rows that index already let
    through) but where the shorter name is a real PREFIX of the longer one
    - "sodalis" / "sodalis solutions", not "sodalis" / "modalis". Prefix-
    only, deliberately not a fuzzy edit-distance or substring-anywhere
    score: workgraph_signals.normalize_company_name only strips a trailing
    corporate suffix, so two names that are genuinely the same company at
    different degrees of formality reliably share a common START, while
    unrelated companies coincidentally sharing letters in the MIDDLE or END
    of a short name is a real false-positive risk this check has no reason
    to take on. O(n^2) over active relationships, which is fine at this
    table's real scale (dozens, not thousands)."""
    relationships = ws.list_relationships(status="active")
    findings = []
    for i, a in enumerate(relationships):
        a_norm = (a.get("normalized_name") or "").strip()
        if len(a_norm) < _MIN_ALIAS_PREFIX_LEN:
            continue
        for b in relationships[i + 1:]:
            b_norm = (b.get("normalized_name") or "").strip()
            if len(b_norm) < _MIN_ALIAS_PREFIX_LEN or a_norm == b_norm:
                continue
            shorter, longer = (a_norm, b_norm) if len(a_norm) <= len(b_norm) else (b_norm, a_norm)
            if not longer.startswith(shorter):
                continue
            id_a, id_b = sorted([a["id"], b["id"]])
            findings.append({
                "dedupe_key": f"{id_a}:{id_b}",
                "subject_type": "relationship_pair",
                "subject_id": f"{id_a},{id_b}",
                "description": (
                    f"relationships {a['id']} ({a['name']!r}) and {b['id']} ({b['name']!r}) may be the "
                    f"same real entity under two different names ({shorter!r} is a prefix of {longer!r})"
                ),
                "detail": {"relationship_ids": [a["id"], b["id"]], "names": [a["name"], b["name"]]},
            })
    return findings


# --- runner + wiring ---------------------------------------------------------

_CHECKS = {
    "stale_active_project_zero_claims_30d": find_stale_active_projects,
    "done_project_with_open_commitments": find_done_projects_with_open_commitments,
    "issue_contradictory_open_claims": find_issues_with_contradictory_open_claims,
    "closure_signal_no_matching_open_request": find_closure_signals_with_no_matching_request,
    "prepared_action_succeeded_no_evidence": find_succeeded_actions_without_evidence,
    "orphaned_claim_or_evidence_row": find_orphaned_claims_and_evidence,
    "duplicate_relationship_alias": find_duplicate_relationship_aliases,
}


def run_self_audit_sweep(*, now: Optional[float] = None) -> dict:
    """Runs all seven checks, persists/touches a self_audit_findings row per
    currently-true finding, and auto-resolves (bookkeeping only - see this
    module's own docstring) any previously-open finding no longer detected.
    Never raises for one check's own failure taking down the rest - same
    per-step isolation scheduled_refresh.py's own run() already gives every
    sweep it calls, applied one level down since all seven checks share one
    call here."""
    now = now if now is not None else time.time()
    summary = {}
    for check_name, check_fn in _CHECKS.items():
        try:
            findings = check_fn()
        except Exception as e:
            summary[check_name] = {"error": str(e)}
            continue
        active_keys = set()
        for f in findings:
            ws.record_self_audit_finding(
                check_name=check_name, dedupe_key=f["dedupe_key"], subject_type=f["subject_type"],
                subject_id=f["subject_id"], description=f["description"],
                detail_json=json.dumps(f["detail"], default=str) if f.get("detail") is not None else None,
                now=now,
            )
            active_keys.add(f["dedupe_key"])
        resolved = ws.auto_resolve_missing_self_audit_findings(check_name, active_keys, now=now)
        summary[check_name] = {"active_findings": len(findings), "auto_resolved": resolved}
    return summary


def run_self_audit_sweep_daily_if_due(now: Optional[float] = None) -> Optional[dict]:
    """Gated the exact same way workgraph_noise.run_noise_sweep_daily_if_due
    / workgraph_lifecycle.run_dormant_sweep_daily_if_due are - cheap and
    deterministic enough to run every scheduled_refresh.py cycle, but daily
    is plenty for findings that describe weeks/months-scale drift, and
    keeps this mechanism's own cursor independent of any other sweep's."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not ws.claim_daily_run("self_audit_sweep", today):
        return None
    return run_self_audit_sweep(now=now)


if __name__ == "__main__":
    ws.init_workgraph()
    print(json.dumps(run_self_audit_sweep(), indent=2, default=str))
