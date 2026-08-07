"""
workgraph_projects.py — deterministic Project auto-grouping. No LLM calls.

Per Marc's explicit call: grouping should be automatic on a STRONG signal,
and only surfaced for confirmation when the signal is weak. Correction
happens through conversation with a worker (e.g. "no, split that back out"),
not a required review-queue step - so every grouping/reassignment operation
here is just a call to workgraph_store.assign_issue_to_project, the same
function a worker would call on Marc's behalf after he corrects one in chat.

Narrowed 2026-07-31 per a real adversarial review of this module: only a
matching structured reference ID (a real PR/PO/contract number - see
_shared_reference_id) is a real project-identity proof, unambiguous enough
to auto-merge on its own. Sharing an external party, an external company,
or a subject/topic core is a RELATIONSHIP signal, not project identity - an
account manager, consultant, or supplier contact routinely spans many
concurrent, unrelated deals, so none of the three is trusted to auto-merge
by itself anymore (see group_issue()'s docstring for the full history:
this module originally treated a shared external party as sufficient on
its own, which is what this narrowing walks back).

Strong signal (candidate generation - suggested, not auto-merged, unless a
repeated CONFIRMED precedent already exists for this exact pattern): two
issues share an external party, an external company, or a matching
subject/topic core.

Weak signal (suggested, never auto-applied): same non-'other' category and
opened within a proximity window, but no shared external party - written to
pending_project_suggestions for Marc (or a worker relaying his answer) to
confirm or reject via workgraph_store.resolve_project_suggestion.
"""
from __future__ import annotations

import json
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import text_extract
import workgraph_store as ws
import workgraph_signals
import workgraph_nba
import workgraph_parties
import workgraph_discovery

# Fixed 2026-07-30 (Marc's direct catch): a real production project (proj-015)
# had merged 71 issues spanning 56+ genuinely distinct purchase requisitions,
# purely because their subjects all shared the boilerplate phrase "Action
# required: Approve the Requisition that [name] submitted" (45 shared
# characters, well past MIN_TOPIC_KEY_LEN) - the existing company-disjoint
# veto never fired because the ONLY external party on any of them is Ariba's
# own no-reply sender, which is correctly excluded from company
# identification, leaving both sides' company sets empty. Marc's explicit
# principle: project identity should track the actual sourcing transaction
# (a PR/PO number, a contract name/type) - the same supplier, or even the
# same automated boilerplate template, can legitimately cover many
# unrelated purchase requisitions. PR/PO numbers are a REAL, deterministic
# identifier (confirmed against real subjects: "PR1111865", "PR416079-V33",
# "PO4200703817") - unlike a shared company or a shared boilerplate phrase,
# so a disjoint reference-number pair is treated the same way the existing
# disjoint-company check already is: positively contradicting evidence,
# not just an absence of confirming evidence.
#
# Widened 2026-07-30 (enhancement #1): this used to re-scan every linked
# raw_item's subject+body text with its own regex on EVERY call - and this
# gets called for every pairwise candidate comparison during grouping, so
# the same unchanging text was rescanned repeatedly (O(issues^2) rescans in
# the worst case). raw_items.pr_number is now a real, persisted field
# (workgraph_classify.py computes it once at classify time, using the same
# pattern - see workgraph_signals.REFERENCE_ID_RE, the single shared
# source now instead of two separate copies of this regex) - this is a
# plain read of already-computed data instead of a live rescan.


def reference_ids_for_issue(issue_id: str) -> set:
    """Every real, persisted PR#/PO# across this issue's own raw_items,
    uppercased for comparison. A raw_item with no recognized reference
    contributes nothing (None), never a guess. Public (not underscore-
    prefixed): enhancement #86 (issue detail panel reference-ID chip)
    reuses this exact same set from server_lean.py rather than re-deriving
    it - one real source, not two copies of the same read.

    DISPLAY/AUDIT ONLY - keeps the full versioned string (e.g.
    "PR416079-V33"). Matching/grouping logic below uses
    reference_base_ids_for_issue instead (2026-07-31 fix) - see that
    function's own docstring for why the two must NOT be the same set."""
    return {
        item["pr_number"].upper() for item in ws.get_raw_items_for_issue(issue_id)
        if item.get("pr_number")
    }


def reference_base_ids_for_issue(issue_id: str) -> set:
    """Same shape as reference_ids_for_issue, but version-stripped (e.g.
    "PR416079-V33" -> "PR416079") - the set actually used for MATCHING.

    2026-07-31 fix: every matching function below used to compare the FULL
    versioned string, so "PR416079-V32" and "PR416079-V33" (confirmed real:
    PR1140347-V2/V3 both exist in production today) were treated as two
    entirely unrelated identities - and worse, _vetoed_by_reference_mismatch
    treated the mismatch as ACTIVELY CONTRADICTING evidence, which could
    veto an otherwise-valid party/company/topic match. reference_ids_for_
    issue (above) stays untouched for display/audit - server_lean.py's
    issue-detail reference-ID chip should keep showing the specific
    version, not the collapsed base.

    2026-08-06 fix (Marc's direct catch, Kinaxis grouping investigation):
    also scans attachments.extracted_text via the exact same REFERENCE_ID_RE
    + reference_base() already trusted for raw_item text - real live
    example that motivated this: marc-683's own signed-CR attachment PDF/DOCX
    both contain "IC-17255" in their extracted text (and even the filename),
    the same reference proj-006's synthesis already independently identified
    from body text, but pr_number_base (raw_item-text-only, computed once at
    classify time) never saw it since the reference only appears in the
    ATTACHMENT, not the email body/subject. Still deterministic - no LLM
    call, same regex, just a second, already-ingested text source. Does not
    persist anywhere (unlike pr_number_base) - recomputed live each call,
    which is what makes this retroactive for free: no reclassification or
    backfill needed for existing attachments already absorbed and text-
    extracted, only a stale signature CACHE (get_or_compute_work_object_
    signature) needs invalidating/recomputing to pick this up."""
    base_ids = {
        item["pr_number_base"].upper() for item in ws.get_raw_items_for_issue(issue_id)
        if item.get("pr_number_base")
    }
    for att in ws.list_attachments_for_issue(issue_id):
        text = att.get("extracted_text") or ""
        if not text:
            continue
        for match in workgraph_signals.REFERENCE_ID_RE.finditer(text):
            base = workgraph_signals.reference_base(match.group(0))
            if base:
                base_ids.add(base.upper())
    return base_ids


def find_reference_id_collisions_for_issue(issue_id: str, issue: Optional[dict] = None) -> list[dict]:
    """Enhancement idea panel #2: real visibility into a same-PR/PO-number
    pair that is NOT already in the same project - either a merge that
    hasn't caught up yet, or one deliberately blocked by a v2.4 cannot_
    merge/cannot_link constraint (both worth Marc seeing, for different
    reasons).

    Real perf fix, 2026-08-03: this used to loop over ws.list_issues(states=
    None, limit=10000) in Python and call get_or_compute_work_object_
    signature on every candidate - profiled live at ~1.5s per call on a
    345-issue board, and cockpit.html's pccLoadIssues() calls the issue-
    detail route (which calls this) once per issue on every page load, so
    that was ~345 x 1.5s of largely-serialized work behind workgraph_
    store.py's single global write lock. definitive_ids IS just reference_
    base_ids_for_issue's output (see compute_work_object_signature) - so
    this now goes straight to ws.list_issues_for_reference_any_state's
    idx_raw_pr_number_base-indexed query, one per reference this issue
    actually has (almost always 0 or 1), instead of a full-table scan."""
    if issue is None:
        issue = ws.get_issue(issue_id)
    my_refs = reference_base_ids_for_issue(issue_id)
    if not my_refs:
        return []
    my_project_id = issue.get("project_id")
    by_sibling: dict[str, dict] = {}
    for ref in sorted(my_refs):
        for row in ws.list_issues_for_reference_any_state(ref):
            sibling_id = row["issue_id"]
            if sibling_id == issue_id:
                continue
            if my_project_id and my_project_id == row.get("project_id"):
                continue  # already grouped together - not a collision worth flagging
            entry = by_sibling.setdefault(
                sibling_id, {"issue_id": sibling_id, "title": row.get("title"), "shared_reference_ids": set()}
            )
            entry["shared_reference_ids"].add(ref)
    return [
        {**entry, "shared_reference_ids": sorted(entry["shared_reference_ids"])}
        for entry in by_sibling.values()
    ]


_CLOSED_STATES = ("done", "dismissed", "noise-archived")


def find_all_reference_id_collisions() -> list[dict]:
    """Enhancement idea panel #14 (Reference-ID cross-check worker
    capability): the DB-wide sweep behind workgraph_alerts.py's proactive
    reference_id_collision alert - unlike find_reference_id_collisions_
    for_issue (panel #2, a per-issue, on-demand lookup that only surfaces
    anything if Marc is already looking at one of the two issues), this
    scans every issue at once and can surface a pair Marc would otherwise
    never think to cross-check manually.

    Deliberately restricted to issues BOTH currently open (not done/
    dismissed/noise-archived) - unlike panel #2's version, which keeps
    closed-issue collisions visible for audit on request, an unprompted
    alert about a thread Marc already closed out isn't something to act
    on right now. Same same-project skip as panel #2 (already grouped
    together is not a collision worth flagging)."""
    rows = ws.list_all_reference_base_id_pairs()
    by_ref: dict[str, dict[str, dict]] = {}
    for row in rows:
        if row["state"] in _CLOSED_STATES:
            continue
        by_ref.setdefault(row["ref"], {})[row["issue_id"]] = row

    pairs: dict[tuple[str, str], dict] = {}
    for ref, by_issue in by_ref.items():
        issue_ids = sorted(by_issue.keys())
        for i, a in enumerate(issue_ids):
            for b in issue_ids[i + 1:]:
                project_a, project_b = by_issue[a]["project_id"], by_issue[b]["project_id"]
                if project_a and project_a == project_b:
                    continue  # already grouped together - not a collision worth flagging
                key = (a, b)
                entry = pairs.setdefault(key, {
                    "issue_a": a, "title_a": by_issue[a]["title"],
                    "issue_b": b, "title_b": by_issue[b]["title"],
                    "shared_reference_ids": set(),
                })
                entry["shared_reference_ids"].add(ref)
    return [
        {**entry, "shared_reference_ids": sorted(entry["shared_reference_ids"])}
        for entry in pairs.values()
    ]


def related_open_issues_by_reference(issue_id: str) -> list[dict]:
    """Checklist rework (2026-08-01): the display-oriented counterpart to
    _shared_reference_id above - that one stops at the FIRST sibling found
    (a grouping decision only needs one); this returns EVERY other open
    issue sharing any of this issue's reference bases, for surfacing real
    relationships to the user (e.g. "also referenced on PR854779, split
    across 3 issues"). Deliberately NOT a claim about WHICH one blocks
    which - the same reference base on two issues today can mean either
    genuinely related-but-distinct threads or a grouping gap that should
    have merged them; that judgment isn't made here, just the real fact of
    the shared reference. {issue_id, title, shared_reference}, one row per
    (sibling, reference) pair, deduped by sibling."""
    my_refs = reference_base_ids_for_issue(issue_id)
    seen_siblings = set()
    out = []
    for ref in sorted(my_refs):
        for sibling_id in ws.list_open_issue_ids_for_reference(ref):
            if sibling_id == issue_id or sibling_id in seen_siblings:
                continue
            sibling = ws.get_issue(sibling_id)
            if not sibling:
                continue
            seen_siblings.add(sibling_id)
            out.append({
                "issue_id": sibling_id,
                "title": sibling.get("display_title") or sibling.get("title") or sibling_id,
                "shared_reference": ref,
            })
    return out


def _project_name_for(issue: dict, category: str, parties: list) -> str:
    external = [p for p in parties if p.get("affiliation") == "external" and p.get("company")
                and not workgraph_signals.is_automated_sender(p.get("primary_email") or "")]
    if external:
        # Fixed 2026-07-30 (hardening pass #2): workgraph_store.
        # list_parties_for_issue has no ORDER BY, so picking a bare [0]
        # from an unordered JOIN result was non-deterministic whenever an
        # issue has more than one identifiable external company (a vendor
        # cc'ing outside counsel, a three-way negotiation) - two
        # otherwise-identical runs could name the same project after a
        # different company. first_seen_ts ascending is a real, stable
        # tie-break (the earliest-known contact on this issue), not an
        # arbitrary one.
        external.sort(key=lambda p: p.get("first_seen_ts") or 0)
        return f"{external[0]['company']} — {category}"
    return f"{issue['title'][:50]} — {category}"


MIN_TOPIC_KEY_LEN = 15  # below this, a normalized subject core is too generic/short to trust alone


def _system_party_for_work_object(work_object_id: str) -> Optional[str]:
    """The real counterparty name (e.g. "AUTHENTICX INC", "Fullstory, Inc"),
    read from every raw_item linked to this work_object's own full body
    text - first labeled-field-bearing body wins. Generalized 2026-08-05
    (Marc's direct correction: "this has to be designed to work for
    everyone... identify system email addresses and apply the same process
    to all of them") - not tied to Ariba specifically; works for any
    automated system whose body labels its real party
    (workgraph_signals.extract_labeled_party_field's own docstring has the
    two independently-confirmed real shapes this covers). Without this, an
    automated-system-routed communication shared NO party signal at all:
    is_automated_sender correctly excludes the system's own sender address
    from party/company matching, and nothing else ever populated a real
    party.company for it."""
    for item in ws.get_raw_items_for_issue(work_object_id):
        party = workgraph_signals.extract_labeled_party_field(text_extract.resolve_item_text(item))
        if party:
            return party
    return None


def compute_work_object_signature(work_object_id: str, issue: Optional[dict] = None) -> dict:
    """Design doc Section 12.7: the real content behind one work_object's
    cached signature, built from the same real data _issue_signal_snapshot
    already read (parties/companies/references/source_containers), plus
    identity_constraints (v2.4's cannot_merge/cannot_link) for
    cannot_link_ids - a check _issue_signal_snapshot/_pairwise_score never
    had at all - and artifact_lineages (v2.6) for accepted_lineages, a gap
    that stayed an honest [] until that table existed to answer it.

    positive_vocabulary now has a real producer (task #169/#170, 2026-08-04,
    Marc's direct design ask): Ariba requester/descriptor/PR fields (is_
    automated_sender already excludes the notification address itself from
    party/company matching, so without this, two different Ariba
    requisitions - or two versions of the same one - look identical to the
    grouping signature) plus a real dollar-amount extraction
    (workgraph_nba.value_amount_for_issue, task #24's heuristic, previously
    unused for grouping). system_party (2026-08-05, generalized same day
    from an Ariba-only ariba_supplier field per Marc's direct correction)
    is the real counterparty name out of the body's own labeled field,
    for ANY automated system, not just Ariba - see
    _system_party_for_work_object's own docstring for why this is a
    separate producer from ariba_fields, which only ever reads the title.
    negative_vocabulary stays None - still no real producer for it. Returns
    plain Python values (lists/sets as lists) -
    get_or_compute_work_object_signature is what JSON-encodes for the
    cache."""
    if issue is None:
        issue = ws.get_issue_or_cluster(work_object_id)
    ariba_fields = workgraph_signals.extract_ariba_requisition_fields(issue.get("title") or "") if issue else None
    value_amount = workgraph_nba.value_amount_for_issue(work_object_id)
    system_party = _system_party_for_work_object(work_object_id)
    positive_vocabulary = None
    if ariba_fields or value_amount or system_party:
        positive_vocabulary = {
            "ariba_requester": (ariba_fields or {}).get("requester"),
            "ariba_descriptor": (ariba_fields or {}).get("descriptor"),
            "value_amount": value_amount or None,
            "system_party": system_party,
        }
    parties = ws.list_parties_for_issue(work_object_id)
    real_parties = [
        p for p in parties
        if p.get("affiliation") != "external" or not workgraph_signals.is_automated_sender(p.get("primary_email") or "")
    ]
    participant_roles = [
        {"party_id": p["id"], "role": p.get("role"), "affiliation": p.get("affiliation")}
        for p in real_parties
    ]
    external_orgs = sorted({
        p["company"].lower() for p in real_parties
        if p.get("affiliation") == "external" and p.get("company")
    })
    definitive_ids = sorted(reference_base_ids_for_issue(work_object_id))
    containers = sorted(c["id"] for c in ws.list_source_containers(issue_id=work_object_id))
    evidence_ts = [e["ts"] for e in ws.list_evidence(work_object_id) if e.get("ts")]
    period_bounds = list(evidence_ts)
    if issue and issue.get("opened_at"):
        period_bounds.append(issue["opened_at"])
    cannot_link_ids = sorted({
        (c["subject_b"] if c["subject_a"] == work_object_id else c["subject_a"])
        for c in ws.list_identity_constraints_for_subject(work_object_id)
        if c["constraint_type"] in ("cannot_merge", "cannot_link") and c.get("subject_b")
    })
    return {
        "definitive_ids": definitive_ids,
        "accepted_lineages": sorted(ws.list_artifact_lineage_ids_for_work_object(work_object_id)),
        "containers": containers,
        "external_orgs": external_orgs,
        "participant_roles": participant_roles,
        "active_period_start": min(period_bounds) if period_bounds else None,
        "active_period_end": max(period_bounds) if period_bounds else None,
        "positive_vocabulary": positive_vocabulary,
        "negative_vocabulary": None,
        "cannot_link_ids": cannot_link_ids,
    }


_SIGNATURE_SCHEMA_VERSION = 1
# Bump this whenever compute_work_object_signature's real OUTPUT SHAPE
# changes (a new key, a changed meaning for an existing one) - see
# get_or_compute_work_object_signature's own docstring for the real bug
# this closes (2026-08-05): a cached row has no other way to know the
# CODE that produced it is now stale, as opposed to the DATA it was
# computed from. History: 1 = adds "system_party" to positive_vocabulary
# (generalized same day from an Ariba-only "ariba_supplier" before this
# version was ever written against the live DB, so no bump needed for
# that rename alone).


def get_or_compute_work_object_signature(work_object_id: str, issue: Optional[dict] = None) -> dict:
    """Cache-first read (design doc Section 12.7) - a cached row survives
    until the real write sites that change its content invalidate it (see
    workgraph_store.invalidate_work_object_signature's callers), so a
    given work_object's signature is normally computed ONCE, not on every
    single scored_grouping_decision call that happens to consider it as a
    candidate.

    Fixed 2026-08-05 (real live bug): a cached row whose schema_version
    doesn't match _SIGNATURE_SCHEMA_VERSION is now treated as a cache MISS,
    not a hit - without this, 355 of 361 real issues had a cached row from
    before compute_work_object_signature grew the system_party field, and
    every one of them was trusted forever, silently never recomputing.
    invalidate_work_object_signature's own callers still matter for real
    DATA changes (a new party linked, evidence added) - this is the
    orthogonal CODE-changed case those callers can't see."""
    cached = ws.get_work_object_signature(work_object_id)
    if cached is not None and cached.get("schema_version") == _SIGNATURE_SCHEMA_VERSION:
        return {
            "definitive_ids": json.loads(cached["definitive_ids"]),
            "accepted_lineages": json.loads(cached["accepted_lineages"]),
            "containers": json.loads(cached["containers"]),
            "external_orgs": json.loads(cached["external_orgs"]),
            "participant_roles": json.loads(cached["participant_roles"]),
            "active_period_start": cached["active_period_start"],
            "active_period_end": cached["active_period_end"],
            "positive_vocabulary": json.loads(cached["positive_vocabulary"]) if cached["positive_vocabulary"] else None,
            "negative_vocabulary": json.loads(cached["negative_vocabulary"]) if cached["negative_vocabulary"] else None,
            "cannot_link_ids": json.loads(cached["cannot_link_ids"]),
        }
    sig = compute_work_object_signature(work_object_id, issue)
    ws.upsert_work_object_signature(
        work_object_id,
        definitive_ids_json=json.dumps(sig["definitive_ids"]),
        accepted_lineages_json=json.dumps(sig["accepted_lineages"]),
        containers_json=json.dumps(sig["containers"]),
        external_orgs_json=json.dumps(sig["external_orgs"]),
        participant_roles_json=json.dumps(sig["participant_roles"]),
        active_period_start=sig["active_period_start"],
        active_period_end=sig["active_period_end"],
        positive_vocabulary_json=json.dumps(sig["positive_vocabulary"]) if sig["positive_vocabulary"] is not None else None,
        negative_vocabulary_json=json.dumps(sig["negative_vocabulary"]) if sig["negative_vocabulary"] is not None else None,
        cannot_link_ids_json=json.dumps(sig["cannot_link_ids"]),
        schema_version=_SIGNATURE_SCHEMA_VERSION,
    )
    return sig


def _topic_key_for_signature(issue: dict, sig: dict) -> str:
    """Topic matching stays a direct title comparison, not a signature
    field - see compute_work_object_signature's own docstring on why
    positive_vocabulary has no real producer yet. Same has_external gate
    _issue_signal_snapshot used, derived from the signature's own
    participant_roles instead of a fresh parties query."""
    has_external = any(p.get("affiliation") == "external" for p in sig["participant_roles"])
    if not has_external:
        return ""
    key = ws.normalize_topic_key(issue.get("title") or "")
    return key if len(key) >= MIN_TOPIC_KEY_LEN else ""


def _matched_data_points(a_id: str, a_sig: dict, a_topic_key: str,
                          b_id: str, b_sig: dict, b_topic_key: str) -> list:
    """Task #184 (2026-08-04, Marc's direct redesign) - replaces
    _pairwise_score_from_signature (retired same day, along with the
    weighted-score model it belonged to). Returns the plain list of
    matched NORMALIZED DATA POINTS between two issues - no weights, no
    score. A real candidate is 2 or more of these; that gate lives in the
    caller (scored_grouping_decision), not here - this function only
    reports what actually matched.

    The point-type vocabulary, mapped from Marc's own list onto what
    Jasper's extraction actually produces today:
      - "reference": a shared real PR/PO number (definitive_ids overlap).
        Also its own separate, standalone-sufficient early return in
        scored_grouping_decision (_shared_reference_id) - included here
        too so it participates honestly in the count for any candidate
        that reaches this far without having triggered that early return.
      - "supplier": shared external company/org - either a tracked party's
        company in common, or a matching real counterparty name extracted
        from ANY automated system's body (workgraph_signals.
        extract_labeled_party_field, 2026-08-05, generalized per Marc's
        direct correction - not Ariba-specific), never the notification
        system's own address. Company-name comparison is normalized
        (workgraph_signals.normalize_company_name) so a tracked party's
        "Authenticx" and a system field's formal "AUTHENTICX INC" count as
        the same real vendor.
      - "stakeholder": a shared NAMED person - either a tracked external
        party in common, or a matching Ariba-extracted requester name.
        Deliberately one point type, not two, even when both fire - they
        answer the same question ("is there a shared named person"), and
        Marc's own list treats "named stakeholder" as one data point.
      - "product_service": a shared Ariba-extracted product/service
        description.
      - "amount": a shared dollar value (within 1%).
      - "document": shared attachment/document lineage.
      - "subject_entity": a shared, specific normalized subject/topic core.

    Deliberately NOT a point type here, per Marc's own explicit rule
    ("never used as the primary matching data points"): sender/participant
    overlap (a shared INTERNAL contact) - always extracted and available
    to whoever reviews a candidate's real content, but never counted
    toward the 2+-point gate itself, on purpose. Category is dropped
    entirely (not one of his listed data point types - an internal
    taxonomy tag, not extracted evidence).

    Honest gap, not silently pretended-covered: three of his listed types
    have no extractor yet - a contract identifier distinct from a PR/PO
    number, a business unit, and a date/deadline used as a MATCHING point
    (deadlines already exist elsewhere in Jasper, just not wired in here).

    Retracted 2026-08-05 (Marc's direct correction, live on the Authenticx
    case): a disjoint real reference used to veto EVERYTHING outright - two
    issues with different captured PR/PO numbers could never match on
    anything else either, no matter how much other real signal they shared.
    That's exactly what kept three genuinely-related Authenticx PRs (CMH
    Chatbots, Lilly Direct, Omvoh/Olumiant/Ebglyss - the same overall vendor
    relationship, three separate purchase transactions) from ever becoming
    candidates for the same project. Marc's own call: a disjoint reference
    is real evidence these are different TRANSACTIONS, not evidence they're
    unrelated - so it no longer blocks the pair from matching on other real
    points, it just means "reference" itself is never one of the counted
    points for this pair (the block below already only counts "reference"
    when the two id sets actually overlap - removing the veto didn't change
    that half). A pair that clears 2+ points this way still only ever
    reaches "candidate" (curator/human review), never "auto_merge" -
    _shared_reference_id/scored_grouping_decision's own auto_merge path
    only ever fires on a genuinely SHARED reference, never a disjoint one -
    so this can't silently merge two different real transactions on its
    own; it can only surface them together for a real judgment call. Per
    Marc's own explicit follow-up, THAT judgment (confirmed correct on
    Authenticx) is: group them into one project, then have curator extract
    each real transaction back out as its own separate issue INSIDE that
    project (see workgraph_projects.extract_issue_from_project, corrected
    pipeline Phase D) - "split them up within the project," his words -
    rather than ever collapsing them into one blob.

    The remaining absolute veto, unchanged: a cannot_merge/cannot_link
    identity_constraint is a real human override (an explicit past reject),
    not an inference - still returns [] outright, regardless of anything
    else that would otherwise match."""
    a_ids, b_ids = set(a_sig["definitive_ids"]), set(b_sig["definitive_ids"])
    if b_id in a_sig["cannot_link_ids"] or a_id in b_sig["cannot_link_ids"]:
        return []
    # NOTE (2026-08-04): Marc's broader "hard contradiction" concept (a
    # different PO/legal-entity/requester should "materially reduce
    # confidence or trigger an LLM review boundary," his own words - not
    # necessarily an absolute veto the way a disjoint reference ID used to
    # be) is deliberately NOT implemented as a new hard veto here either,
    # same reasoning that already applied to ariba_requester mismatches
    # below - a real bridging item sharing product_service+amount with a
    # DIFFERENT named requester (or a different PR/PO) is exactly the kind
    # of case that should reach curator's LLM review, not get silently
    # vetoed before anyone looks at it.

    points = []
    if a_ids and b_ids and not a_ids.isdisjoint(b_ids):
        points.append("reference")

    a_vocab, b_vocab = a_sig.get("positive_vocabulary") or {}, b_sig.get("positive_vocabulary") or {}
    # Real counterparty from ANY automated system's body field folds into
    # the SAME "supplier" signal as tracked party companies, normalized so
    # "Authenticx" (a party record) and "AUTHENTICX INC" (a system's own
    # field) compare equal - see normalize_company_name's own docstring.
    # Without this fold-in, a system-routed communication (whose only
    # sender is excluded from party/company matching by design) never
    # carried ANY supplier signal at all, real vendor or not.
    a_suppliers = {workgraph_signals.normalize_company_name(o) for o in a_sig["external_orgs"]}
    b_suppliers = {workgraph_signals.normalize_company_name(o) for o in b_sig["external_orgs"]}
    a_suppliers.add(workgraph_signals.normalize_company_name(a_vocab.get("system_party")))
    b_suppliers.add(workgraph_signals.normalize_company_name(b_vocab.get("system_party")))
    a_suppliers.discard("")
    b_suppliers.discard("")
    if a_suppliers and b_suppliers and not a_suppliers.isdisjoint(b_suppliers):
        points.append("supplier")

    a_external = {p["party_id"] for p in a_sig["participant_roles"] if p.get("affiliation") == "external"}
    b_external = {p["party_id"] for p in b_sig["participant_roles"] if p.get("affiliation") == "external"}
    a_req, b_req = a_vocab.get("ariba_requester"), b_vocab.get("ariba_requester")
    shared_named_person = (a_external and b_external and not a_external.isdisjoint(b_external)) or (
        a_req and b_req and a_req.lower().strip() == b_req.lower().strip())
    if shared_named_person:
        points.append("stakeholder")

    if a_topic_key and b_topic_key:
        m = SequenceMatcher(None, a_topic_key, b_topic_key).find_longest_match(
            0, len(a_topic_key), 0, len(b_topic_key))
        if m.size >= MIN_TOPIC_KEY_LEN:
            points.append("subject_entity")

    a_desc, b_desc = a_vocab.get("ariba_descriptor"), b_vocab.get("ariba_descriptor")
    if a_desc and b_desc:
        norm_a, norm_b = a_desc.lower().strip(), b_desc.lower().strip()
        m = SequenceMatcher(None, norm_a, norm_b).find_longest_match(0, len(norm_a), 0, len(norm_b))
        if m.size >= MIN_TOPIC_KEY_LEN:
            points.append("product_service")

    a_amt, b_amt = a_vocab.get("value_amount"), b_vocab.get("value_amount")
    if a_amt and b_amt and abs(a_amt - b_amt) <= max(a_amt, b_amt) * 0.01:
        points.append("amount")

    if (a_sig["accepted_lineages"] and b_sig["accepted_lineages"]
            and not set(a_sig["accepted_lineages"]).isdisjoint(b_sig["accepted_lineages"])):
        points.append("document")

    # Personalized data-point discovery retrofit (#215/#216, 2026-08-06):
    # purely additive - every hardcoded check above is completely
    # unchanged. Contributes extra points for any CONFIRMED, genuinely
    # discovered (non-fast-tracked) data point the two work objects share
    # a real value for. A no-op today (no non-fast-track definitions are
    # confirmed yet), real capability the moment Marc confirms his first
    # genuine discovery - see workgraph_discovery.matched_discovered_
    # points' own docstring for why this is safe to call unconditionally.
    points.extend(workgraph_discovery.matched_discovered_points(a_id, b_id))

    return points


def merge_issues(issue_id_a: str, issue_id_b: str, *, reason_label: str) -> dict:
    """The one place two issues actually become the same project - joins
    whichever of the two already has a project, or creates a new one.
    Shared by the deterministic strong-signal path and by a confirmed
    project-suggestion (Marc's own call, or curator's LLM judgment on the
    weak-signal residue).

    Returns {"status": "merged", "project_id": ...} - OR, since 2026-07-31
    (step 5, mandatory reconciliation), {"status": "deferred",
    "winner_project_id": ..., "loser_project_id": ...} when merging would
    collide two ALREADY-established projects (see workgraph_store.
    merge_issues_txn's own docstring). Every caller must check "status" -
    see workgraph_pipeline2.process_new_item's own "try the next candidate"
    handling of a deferred result.

    Fixed 2026-07-30 (hardening pass #2): previously only ever consulted
    issue_a's project_id, falling back to issue_b's only if issue_a had
    none - if BOTH already belonged to a project and they were DIFFERENT
    real projects, issue_b (and only issue_b) got silently reassigned out
    of its existing project with no warning, no merge of that project's
    OTHER members, and no cleanup of the now-partially-emptied loser.
    _weak_signal_candidates now refuses to propose a merge against an
    issue that already has a project at all (closing the common path
    that reaches this), but the deterministic strong-signal path can
    still land here directly, so this handles the collision correctly
    rather than assuming it can't happen: every member of the LOSING
    project moves to the winning one, and the emptied loser is archived.

    Fixed again 2026-07-31 (real adversarial review, meeting-grouping design
    pass): the whole multi-step reassign-and-archive sequence used to run as
    several independent autocommit connections (via ws.list_issues_for_
    project/assign_issue_to_project/set_project_status/create_project_with_
    new_id) - a crash partway through left the DB partially merged with no
    recovery path. This is now a thin wrapper: only the pre-computation that
    genuinely needs to happen before a transaction can begin (the new
    project's name/category, derived from issue_a's own parties) stays
    here; the actual merge is one all-or-nothing transaction in
    workgraph_store.merge_issues_txn (see its own docstring for why it has
    to talk to a single connection directly instead of calling back into
    other ws.* helpers).

    Corrected pipeline Phase C (2026-08-05): get_issue_or_cluster, not
    get_issue - issue_id_a is now routinely a cluster (Phase B), and a
    cluster clearing the bar is exactly what this function promotes into
    a real project."""
    issue_a = ws.get_issue_or_cluster(issue_id_a)
    parties = ws.list_parties_for_issue(issue_id_a)
    category = issue_a.get("category")
    return ws.merge_issues_txn(
        issue_id_a, issue_id_b, reason_label=reason_label,
        new_project_name=_project_name_for(issue_a, category, parties),
        new_project_category=category,
    )


def split_issue_from_project(issue_id: str, *, actor: str = "marc", reason: Optional[str] = None) -> dict:
    """Task #178 - the safety valve Marc asked for alongside the more
    aggressive matching model this whole grouping-v3 phase builds ("yes
    you'd need to be able to split them out again/reverse it if it is
    wrong... but still"). Detaches ONE issue from its current project and
    durably vetoes it from drifting straight back in.

    Two things happen, both necessary:
    1. assign_issue_to_project(issue_id, None) - the issue goes back to
       being a standalone issue, exactly like one that never matched
       anything (list_standalone_issue_ids/workgraph_synthesis already
       treats this as a normal, first-class state).
    2. A cannot_merge identity_constraint against EVERY other current
       member of the project being left - not just whichever single
       sibling originally triggered the grouping. Without this, the very
       next classify/grouping cycle would just re-score this issue against
       those same members and merge it right back in, since nothing about
       the underlying signature changed - only Marc's judgment that the
       grouping was wrong did. Scoped to the CURRENT members only (not the
       whole project's future), same reasoning as reject_suggestion's own
       durable-veto: a wrongly-blocked pair costs nothing (it just can't
       auto-merge again, still visible as a suggestion a human could
       force), but a wrongly-allowed one is the exact fragmentation/false-
       merge problem this phase exists to fix.

    membership_state resets to 'provisional' (reset_work_object_membership_
    to_provisional) - if this issue lands somewhere else later, that's a
    NEW grouping nobody has reviewed yet, regardless of whether the OLD one
    (now reversed) had been confirmed."""
    issue = ws.get_issue(issue_id)
    if issue is None:
        return {"action": "not_found"}
    old_project_id = issue.get("project_id")
    if not old_project_id:
        return {"action": "not_grouped"}

    siblings = [i for i in ws.list_issues_for_project(old_project_id) if i["id"] != issue_id]
    reason = reason or f"split off from {old_project_id} (Marc reversed an incorrect grouping)"
    ws.assign_issue_to_project(issue_id, None, reason=reason)
    ws.reset_work_object_membership_to_provisional(issue_id)

    constraints_created = []
    for sibling in siblings:
        if ws.find_identity_constraint("cannot_merge", issue_id, sibling["id"]) is not None:
            continue
        ws.create_identity_constraint(
            "cannot_merge", issue_id, sibling["id"], reason=reason, actor=actor,
        )
        constraints_created.append(sibling["id"])
    ws.invalidate_work_object_signature(issue_id)
    for sibling in siblings:
        ws.invalidate_work_object_signature(sibling["id"])

    return {
        "action": "split", "issue_id": issue_id, "old_project_id": old_project_id,
        "constraints_created": constraints_created,
    }


# Corrected pipeline Phase D data-point mapping (2026-08-05): a small local
# copy of workgraph_classify._evidence_type's own {source: type} mapping,
# not an import - workgraph_classify already imports THIS module, so
# importing it back here would be circular.
_EVIDENCE_TYPE_BY_SOURCE = {
    "outlook_mail": "email", "teams_chat": "teams", "calendar": "calendar", "sharepoint": "sharepoint",
}


def extract_issue_from_project(project_id: str, *, title: str, category: Optional[str] = None,
                                claim_ids: list) -> dict:
    """Corrected pipeline Phase D (2026-08-05) - curator's real content-
    extraction step, Marc's own words: 'only then does the LLM read the
    project's real content and extract the actual issues/asks/deliverables
    from inside it.' The real judgment (which of a confirmed project's
    already-materialized claims - written the normal way, via POST /api/
    workgraph/raw_items/{id}/extraction against its cluster members'
    raw_items - genuinely belong together as ONE trackable issue) happens
    in curator's head, not here; this function is the deterministic
    mechanics once that call is made: create a real issue, join it to the
    project, and move exactly the cited claims (and the evidence for their
    underlying raw_items) onto it.

    Deliberately moves only the CITED claims, not everything on their
    source cluster(s) - a single meeting-series cluster can carry the
    material for more than one real issue (Marc's own Authenticz example:
    a pricing-negotiation ask and a separate onboarding-scope ask living
    on the same set of recurring-meeting clusters), so a whole-cluster move
    (the shape merge_issue_into already provides, for a different case)
    would be too coarse here.

    Every claim_id must currently belong to a work object that's a member
    of THIS project (a cluster or an already-real issue) - a real safety
    check against citing a claim from somewhere unrelated, never a guess.
    Raises ValueError (not a silent no-op) if the project doesn't exist, if
    any claim_id doesn't exist, or if any claim doesn't belong to one of
    this project's members."""
    if ws.get_project(project_id) is None:
        raise ValueError(f"no such project: {project_id}")
    member_ids = {c["id"] for c in ws.list_clusters_for_project(project_id)} | \
                 {i["id"] for i in ws.list_issues_for_project(project_id)}
    claims = []
    for claim_id in claim_ids:
        claim = ws.get_claim(claim_id)
        if claim is None:
            raise ValueError(f"no such claim: {claim_id}")
        if claim["issue_id"] not in member_ids:
            raise ValueError(f"claim {claim_id} does not belong to a member of project {project_id}")
        claims.append(claim)

    new_issue_id = ws.create_issue_with_new_id(title=title, category=category, state="active")
    ws.assign_issue_to_project(new_issue_id, project_id, reason="extracted by curator from confirmed project content")

    raw_item_ids_cited = set()
    for claim in claims:
        ws.reassign_claim(claim["id"], new_issue_id)
        if claim.get("raw_item_id"):
            raw_item_ids_cited.add(claim["raw_item_id"])

    evidence_added = 0
    for raw_item_id in sorted(raw_item_ids_cited):
        raw_item = ws.get_raw_item(raw_item_id)
        if raw_item is None:
            continue
        summary = raw_item.get("subject") or raw_item.get("body_preview") or "(no summary)"
        ws.add_evidence(
            issue_id=new_issue_id,
            type=_EVIDENCE_TYPE_BY_SOURCE.get(raw_item.get("source"), "email"),
            summary=f"{summary} [extracted from project {project_id} by curator]",
            raw_item_id=raw_item_id,
        )
        evidence_added += 1
        # Real bug found live (2026-08-06, add-in demo prep): this loop cites
        # each raw_item's claims/evidence onto new_issue_id but never moved
        # raw_items.issue_id itself - workgraph_parties.extract_and_link_
        # parties_for_issue reads ws.get_raw_items_for_issue(issue_id), i.e.
        # raw_items WHERE issue_id = ?, so every issue created via this
        # function (the ONLY path that creates real 'request' work objects
        # in the corrected pipeline) ended up with zero linked raw_items by
        # this column and therefore zero parties - confirmed live against
        # the post-wipe corpus: 3478 real issues, 0 rows in `parties`.
        # link_raw_item_to_issue is the same primitive workgraph_classify.py
        # already uses to establish this exact ownership; using it here
        # keeps this cited raw_item consistent with every other reader that
        # trusts raw_items.issue_id (parties, get_raw_items_for_issue,
        # thread_map-style lookups), not just the newer evidence/claims path.
        ws.link_raw_item_to_issue(raw_item_id, new_issue_id)

    workgraph_parties.extract_and_link_parties_for_issue(new_issue_id)

    return {"issue_id": new_issue_id, "project_id": project_id,
            "claims_moved": len(claims), "evidence_added": evidence_added}


def aggregate_parties_for_project(project_id: str) -> list[dict]:
    """Project-detail redesign (2026-07-31, Marc's own design brief -
    "internal and external relationships"): every real party across ALL
    of this project's member issues, deduped by party id, annotated with
    issue_count (how many member issues they appear on - real signal of
    how central they are, not guessed) and is_primary (the most-linked
    contact, first_seen_ts ascending as the tie-break - same stable
    convention used everywhere else in this module). is_primary is
    marked separately per affiliation, so a project can have both a
    primary external contact and a primary internal one."""
    issues = ws.list_issues_for_project(project_id)
    by_party: dict = {}
    for issue in issues:
        for p in ws.list_parties_for_issue(issue["id"]):
            pid = p["id"]
            if pid not in by_party:
                entry = dict(p)
                entry["issue_count"] = 0
                by_party[pid] = entry
            by_party[pid]["issue_count"] += 1

    parties = list(by_party.values())
    for affiliation in ("external", "internal"):
        group = [p for p in parties if p.get("affiliation") == affiliation]
        group.sort(key=lambda p: (-p["issue_count"], p.get("first_seen_ts") or 0))
        for i, p in enumerate(group):
            p["is_primary"] = (i == 0)
    for p in parties:
        p.setdefault("is_primary", False)
    parties.sort(key=lambda p: (p.get("affiliation") != "external", -p["issue_count"], p.get("first_seen_ts") or 0))
    return parties
