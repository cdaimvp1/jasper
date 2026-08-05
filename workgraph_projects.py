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
import re
import sys
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import text_extract
import workgraph_store as ws
import workgraph_lessons
import workgraph_signals
import workgraph_nba

WEAK_SIGNAL_WINDOW_DAYS = 45

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
    version, not the collapsed base."""
    return {
        item["pr_number_base"].upper() for item in ws.get_raw_items_for_issue(issue_id)
        if item.get("pr_number_base")
    }


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


def _vetoed_by_reference_mismatch(issue_id: str, sibling_id: str) -> bool:
    """True when BOTH issues have at least one identified PR/PO reference
    BASE and the sets are disjoint - a real, structured signal that
    overrides ANY of the strong-signal checks below (shared party/company/
    subject), since two different purchase requisitions are two different
    transactions no matter how similar their surrounding text looks.
    Matches on the version-stripped base (see reference_base_ids_for_issue)
    so a version bump (V32 -> V33 on the same requisition) is never
    mistaken for a contradiction."""
    my_refs = reference_base_ids_for_issue(issue_id)
    if not my_refs:
        return False
    sibling_refs = reference_base_ids_for_issue(sibling_id)
    return bool(sibling_refs) and my_refs.isdisjoint(sibling_refs)


def _shared_reference_id(issue_id: str):
    """Part A1 of the grouping/NBA redesign (2026-07-30): a matching PR/PO
    reference number is a real, structured identifier - the strongest,
    least ambiguous signal available here, stronger than a shared external
    party/company/subject-text (all three are heuristics; this is a fact).
    Until now this field was used ONLY as a veto (_vetoed_by_reference_
    mismatch above); this is its positive counterpart, checked FIRST in
    _strong_signal_match. Real production case this closes: PR854779-V4
    split across 3 separately-sent automated reminders (a different sender
    each time, no Outlook reply-chain/ConversationID linking them - the
    reference number is the only thing they actually share). Returns
    (reference_id, sibling_issue_id) for the first other OPEN issue
    sharing at least one of this issue's reference ids, or None. No veto
    check needed on this branch - a shared reference and a disjoint
    reference are mutually exclusive by construction.

    Matches on the version-stripped base (2026-07-31 fix) - so an issue
    whose only raw_item says "PR416079-V32" now correctly finds a sibling
    whose raw_item says "PR416079-V33", instead of the two never matching
    at all.

    Corrected pipeline Phase C (2026-08-05): list_open_work_objects_for_
    reference (cluster-aware), not list_open_issue_ids_for_reference - a
    cluster clearing this exact-reference bar is one of the two things
    that promotes it into a real project (see this module's merge_issues),
    so the sibling search must see clusters too, not just real issues."""
    my_refs = reference_base_ids_for_issue(issue_id)
    for ref in sorted(my_refs):
        for sibling_id in ws.list_open_work_objects_for_reference(ref):
            if sibling_id != issue_id:
                return ref, sibling_id
    return None


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


def _shared_external_party(issue_id: str):
    """Returns (party_id, sibling_issue_id) for the first external party this
    issue shares with any other issue, or None. A shared external contact is
    sufficient signal on its own (see module docstring) - EXCEPT a no-reply/
    system sender (Ariba, Adobe Sign, etc.): a shared automated notification
    address proves nothing about the two threads being related (confirmed
    empirically - two completely unrelated Ariba requisition approvals, from
    different requestors, for different purchases, were merging into one
    fake "project" purely because both came through no-reply@ansmtp.ariba.com).
    Scans list_issues_for_party per external party - cheap at personal-inbox
    scale (dozens to low hundreds of issues)."""
    parties = ws.list_parties_for_issue(issue_id)
    for party in parties:
        if party.get("affiliation") != "external" or workgraph_signals.is_automated_sender(party.get("primary_email") or ""):
            continue
        for sibling_id in ws.list_issues_for_party(party["id"]):
            if sibling_id == issue_id:
                continue
            sibling = ws.get_issue(sibling_id)
            if sibling:
                return party["id"], sibling_id
    return None


def _shared_external_company(issue_id: str):
    """Broader than _shared_external_party: two DIFFERENT people at the same
    external company, each on a different thread, still means the same
    supplier relationship - real case Marc flagged (e.g. Anthony Allan on one
    Elanco thread, Calum Bell on another, same underlying deal). Same
    system-sender exclusion as _shared_external_party. Returns (company,
    sibling_issue_id) or None."""
    parties = ws.list_parties_for_issue(issue_id)
    for party in parties:
        if (party.get("affiliation") != "external" or not party.get("company")
                or workgraph_signals.is_automated_sender(party.get("primary_email") or "")):
            continue
        for sibling_id in ws.list_issues_for_company(party["company"]):
            if sibling_id == issue_id:
                continue
            if ws.get_issue(sibling_id):
                return party["company"], sibling_id
    return None


MIN_TOPIC_KEY_LEN = 15  # below this, a normalized subject core is too generic/short to trust alone


def _has_external_party(issue_id: str) -> bool:
    return any(p.get("affiliation") == "external" for p in ws.list_parties_for_issue(issue_id))


def _external_companies_for_issue(issue_id: str) -> set:
    """Every identifiable external company on this issue's parties, lowercased.
    Used by _shared_topic_key to veto a boilerplate-phrase match - see there."""
    return {
        p["company"].lower() for p in ws.list_parties_for_issue(issue_id)
        if p.get("affiliation") == "external" and p.get("company")
        and not workgraph_signals.is_automated_sender(p.get("primary_email") or "")
    }


def _shared_topic_key(issue: dict):
    """Two issues whose normalized subjects share a long contiguous run are
    almost always the same underlying thread split across channels or
    re-titled along the way - e.g. 'NICK/JONATHAN APPROVAL REQUESTED: Veeva
    CRM press release quote' and 'MARC REVIEW REQUESTED: Veeva CRM press
    release' share neither a prefix nor a thread_key/external party, but both
    contain 'requested veeva crm press release' verbatim. A prefix/full-
    containment check misses this (the shared span isn't at the START of
    either string once each has ITS OWN lead-in words) - longest common
    substring does not.

    Gated on BOTH issues having a real external party (confirmed empirically
    necessary: without this gate, identically-titled recurring personal/
    internal calendar entries - "school drop off," a standing weekly 1:1 -
    were getting grouped into fake "projects" purely because their subjects
    repeat verbatim every occurrence. A shared literal subject only means
    "same underlying deal" when there's an actual external counterparty on
    both sides, same trust bar as the other two signals in this module.
    Returns the sibling issue id, or None."""
    if not _has_external_party(issue["id"]):
        return None
    key = ws.normalize_topic_key(issue.get("title") or "")
    if len(key) < MIN_TOPIC_KEY_LEN:
        return None
    # Fixed 2026-07-29: confirmed false-positive - two issues about
    # VERIFIABLY DIFFERENT companies (e.g. procurement-template boilerplate:
    # "please review and approve the attached statement of work for [X]")
    # were still merging on shared boilerplate phrasing alone, exactly the
    # domain most prone to it. If this issue has an identifiable company,
    # a candidate whose OWN identifiable company set is non-empty and
    # disjoint from it is positively contradicting evidence, not just an
    # absence of confirming evidence - veto the match rather than let a
    # long shared phrase override it.
    my_companies = _external_companies_for_issue(issue["id"])
    # Fixed 2026-07-30 (hardening pass #2): no limit override here used to
    # mean workgraph_store.list_issues' own default (200) silently capped
    # the search - the real install is already past that count, so issues
    # outside the top 200 by priority/recency were invisible to this check
    # with no error or log. Every other module added this session that
    # needs "every issue" passes an explicit high limit; this one hadn't.
    for other in ws.list_issues(states=None, limit=10000):
        if other["id"] == issue["id"]:
            continue
        if issue.get("project_id") and issue.get("project_id") == other.get("project_id"):
            continue
        if not _has_external_party(other["id"]):
            continue
        other_key = ws.normalize_topic_key(other.get("title") or "")
        if len(other_key) < MIN_TOPIC_KEY_LEN:
            continue
        match = SequenceMatcher(None, key, other_key).find_longest_match(0, len(key), 0, len(other_key))
        if match.size < MIN_TOPIC_KEY_LEN:
            continue
        other_companies = _external_companies_for_issue(other["id"])
        if my_companies and other_companies and my_companies.isdisjoint(other_companies):
            continue  # known different suppliers - a shared template phrase doesn't override that
        return other["id"]
    return None


def _topic_keys_match(issue_id: str, sibling_id: str) -> bool:
    """Pairwise topic-key overlap between two SPECIFIC issues - unlike
    _shared_topic_key (which SEARCHES the whole corpus for a candidate),
    this just answers the yes/no question for an already-found pair.

    2026-07-31 (related-vs-same-project verdict): the merge-vs-link
    discriminator for a party/company match. Real cases this decides
    correctly: marc-166 ("Dragonfly 2.0 SOW's") and marc-063 ("H1/Lilly SOW
    Review") share an external party (H1) but their topic keys don't
    meaningfully overlap - correctly routes to link, not merge. Uses the
    SAME SequenceMatcher longest-match check _shared_topic_key already
    uses, not category (issue.category is 39% 'other' in real data and too
    coarse a taxonomy to reliably distinguish two transaction types with
    the same counterparty)."""
    issue = ws.get_issue(issue_id)
    sibling = ws.get_issue(sibling_id)
    if not issue or not sibling:
        return False
    key_a = ws.normalize_topic_key(issue.get("title") or "")
    key_b = ws.normalize_topic_key(sibling.get("title") or "")
    if len(key_a) < MIN_TOPIC_KEY_LEN or len(key_b) < MIN_TOPIC_KEY_LEN:
        return False
    match = SequenceMatcher(None, key_a, key_b).find_longest_match(0, len(key_a), 0, len(key_b))
    return match.size >= MIN_TOPIC_KEY_LEN


def _strong_signal_match(issue_id: str, issue: dict):
    """First strong deterministic signal found, checked in order of
    confidence: matching reference ID (Part A1, 2026-07-30 - a real
    structured identifier, checked first since it's the least ambiguous
    signal available) > exact external party > shared external company >
    matching normalized subject/topic core, vetoed by a disjoint PR/PO
    reference number (see _vetoed_by_reference_mismatch above) for the
    latter three (a shared-reference match can't itself be vetoed by a
    reference mismatch - see _shared_reference_id's own docstring).
    A candidate rejected on reference grounds is not retried against a
    weaker signal for the same pair; the safer failure mode is no_match,
    not risking a second wrong merge via a looser check.

    Narrowed 2026-07-31 per real adversarial review of this module: only
    "reference" is a real structured identity, unambiguous enough for
    group_issue() to auto-merge on its own. Party/company/topic each only
    prove that a person/company/subject is shared, not that it's the SAME
    transaction (an account manager, consultant, or supplier contact
    routinely spans many concurrent, unrelated deals) - group_issue() now
    routes those three through the same precedent-check + suggestion path
    as weak signals instead of merging on them directly. Kept as one
    function (rather than splitting into "auto" vs "suggest" checks)
    because the ordering/veto logic is shared and must stay in sync.

    Extended 2026-07-31 (related-vs-same-project verdict) with a 4th
    element, verdict ("merge" or "link"), for the party/company/topic
    kinds - reference stays unconditionally "merge" (a real structured
    identifier IS the same transaction, no discriminator needed):
    - "party": merge if the two issues' topic keys ALSO overlap (same
      literal thread, just also sharing a contact); link otherwise (same
      contact, different transaction - the marc-166/marc-063 shape).
    - "company": always link (same_supplier) - a shared company with
      DIFFERENT people never proves the same transaction (the two-distinct-
      PwC-meeting-series shape).
    - "topic": always merge (unchanged) - _shared_topic_key already
      internally vetoes a disjoint-company candidate, so reaching this
      kind at all means companies are absent or non-contradicting.
    Returns (kind, detail, sibling_issue_id, verdict) or None."""
    m = _shared_reference_id(issue_id)
    if m:
        ref, sibling_id = m
        return "reference", ref, sibling_id, "merge"
    m = _shared_external_party(issue_id)
    if m:
        party_id, sibling_id = m
        if not _vetoed_by_reference_mismatch(issue_id, sibling_id):
            verdict = "merge" if _topic_keys_match(issue_id, sibling_id) else "link"
            return "party", party_id, sibling_id, verdict
    m = _shared_external_company(issue_id)
    if m:
        company, sibling_id = m
        if not _vetoed_by_reference_mismatch(issue_id, sibling_id):
            return "company", company, sibling_id, "link"
    sibling_id = _shared_topic_key(issue)
    if sibling_id and not _vetoed_by_reference_mismatch(issue_id, sibling_id):
        return "topic", ws.normalize_topic_key(issue.get("title") or ""), sibling_id, "merge"
    return None


def _weak_signal_candidates(issue: dict) -> list:
    """Same category, opened within the proximity window, no shared external
    party (that case was already handled as a strong-signal merge before
    this is called) - a real but softer hint worth asking about, not acting
    on unprompted. Same disjoint-PR/PO veto as the strong-signal path -
    even a SUGGESTED merge shouldn't propose combining two issues that
    already look like different purchase requisitions.

    Fixed 2026-07-30 (hardening pass #2): also excludes a candidate that
    already belongs to a DIFFERENT real project, not just the redundant
    "already the same project" case - proposing (and later confirming) a
    merge across two already-established, different projects is exactly
    what let merge_issues silently detach an issue from one project into
    another with no warning. That case now never becomes a suggestion in
    the first place; see merge_issues' own defense-in-depth fix for the
    case where a strong-signal match still reaches it directly."""
    category = issue.get("category")
    if not category or category == "other":
        return []
    out = []
    # Same limit fix as _shared_topic_key above - ws.list_issues' 200-row
    # default was silently capping this search on the real, larger dataset.
    for other in ws.list_issues(states=None, limit=10000):
        if other["id"] == issue["id"] or other.get("category") != category:
            continue
        if other.get("project_id"):
            # Already belongs to a project - either the same one as `issue`
            # (pointless to suggest, the original behavior) or a genuinely
            # DIFFERENT one (dangerous to suggest - see the fix note above).
            # Either way, skip: an issue with no project of its own is a
            # fine target to grow an EXISTING project into, but an issue
            # that already has a home is never proposed as a merge target.
            continue
        if _vetoed_by_reference_mismatch(issue["id"], other["id"]):
            continue
        gap_days = abs((issue.get("opened_at") or 0) - (other.get("opened_at") or 0)) / 86400
        if gap_days <= WEAK_SIGNAL_WINDOW_DAYS:
            out.append(other)
    return out


# --- Task #184 (2026-08-04, Marc's direct redesign, replacing the entire ---
# --- weighted-score system this section used to hold): one deterministic --
# --- candidate pipeline, no weights, no numeric threshold. -----------------
#
# Marc's own words, verbatim, after watching the weighted version in
# practice: "I'm concerned about what these 69 link and 16 bridge groups
# are... if you take only the logic I just gave you... it should be
# significantly accurate... but now there are a bunch of competing systems
# in here that are blocking one another." His replacement design (also
# verbatim, across several messages the same day): normalize every email/
# Teams message down to a fixed vocabulary of real data points (supplier,
# a PR/PO number, a dollar amount, a contract identifier, a product/
# service, a business unit, a named stakeholder, an attachment/document, a
# date/deadline, a meaningful subject/content entity - sender/participant
# and the Outlook thread id are ALWAYS extracted too, but "never used as
# the primary matching data points"). Two issues are a CANDIDATE - worth a
# real LLM/human look at the actual content - when 2 or more of those
# points match, regardless of which two. More matches means a higher-
# ranked candidate, but the gate itself is a plain count, never a weighted
# score: "the whole weighting and scoring is getting in the way."
#
# What this replaces: SCORE_WEIGHTS (per-signal decimal weights),
# AUTO_MERGE_THRESHOLD/WEAK_SUGGESTION_FLOOR (a tunable numeric cutoff),
# the strong/medium/weak anchor tiers _suggestion_kind_for_scored_signals
# used for barely half a day, and confidence-spine damping
# (context_accuracy/effective_score) being part of this decision at all -
# four separate, independently-evolved mechanisms that had accumulated
# into exactly the "competing systems blocking one another" Marc is
# calling out. None of the underlying content-extraction work from earlier
# today (Ariba descriptor/requester/amount, attachment lineage, company/
# party/topic matching) goes away - it's the SAME extracted facts, just
# counted instead of weighted and summed.
#
# One deliberate, flagged exception to "always 2+": an exact shared
# reference ID (PR/PO - see _shared_reference_id) stays sufficient ALONE,
# same as it's always been in this module, unchanged by this redesign.
# That's a real shared transaction key Jasper already tracks with high
# confidence, not an inferred signal like the others in Marc's list - if
# he wants that folded into the same "always 2+" rule too, that's a real,
# separate correction to flag, not an assumption to make silently here.
#
# Also honest about what's NOT built: three of Marc's listed data points
# (a distinct contract identifier separate from PR/PO, business unit, and
# date/deadline as a MATCHING point rather than just an NBA signal) have no
# extractor yet. They're real gaps, not silently pretended-covered - see
# _matched_data_points' own docstring for the exact point-type list this
# actually checks today.

# Task #177 (2026-08-04, Marc's direct design ask): the scored model's own
# candidate search used to scan EVERY issue ever created, forever, with no
# time restriction at all - exactly the unscoped-corpus-scan Marc flagged
# ("put a time restriction on it... never look back longer than x amount of
# time... this significantly reduces the amount it needs to read through").
# Default scope is "open, plus a short grace period after close" (his own
# framing, which he confirmed: "your suggestion here... works") - an issue
# still open is always in scope regardless of age; a CLOSED issue only stays
# in scope for GROUPING_LOOKBACK_GRACE_DAYS after its last update. His
# explicit follow-up ("I do want to be able to use a worker via chat to
# look back further if necessary") is scored_grouping_decision's/
# group_issue's own lookback_days override below - a real per-call escape
# hatch, not a config toggle, since this is meant to be an occasional,
# conversational "check further back for this one" ask, not a standing
# setting.
GROUPING_LOOKBACK_GRACE_DAYS = 45
_OPEN_ISSUE_STATES = {"active", "waiting", "blocked"}


def _candidate_pool(*, lookback_days: Optional[int] = None) -> list:
    """The scored model's own candidate search space - every OPEN issue
    (regardless of age) plus every CLOSED issue updated within the lookback
    window (default GROUPING_LOOKBACK_GRACE_DAYS, override via
    lookback_days for a deliberate deeper look). Still one full table scan
    per call (ws.list_issues' own cost) - this doesn't make the query
    cheaper, it makes the RESULT SET smaller, which is what actually cuts
    the O(n) pairwise scoring work scored_grouping_decision does per
    candidate."""
    window_seconds = (lookback_days if lookback_days is not None else GROUPING_LOOKBACK_GRACE_DAYS) * 86400
    cutoff = time.time() - window_seconds
    out = []
    # Corrected pipeline Phase C (2026-08-05): includes clusters, not just
    # real issues - pass-2 (2+ matched data points) has to be able to find
    # a cluster as a candidate sibling, since a fresh, unmatched
    # communication now becomes a cluster (Phase B), never a real issue,
    # and the whole point of this search is to let a cluster group clear
    # the bar and get promoted into a project.
    for other in ws.list_issues(states=None, limit=10000) + ws.list_clusters(limit=10000):
        if other.get("state") in _OPEN_ISSUE_STATES:
            out.append(other)
            continue
        updated = other.get("updated_at")
        if updated and updated >= cutoff:
            out.append(other)
    return out


def _issue_signal_snapshot(issue_id: str, issue: Optional[dict] = None) -> dict:
    """Precomputed signal data for ONE issue, shared by both the live
    per-issue path (scored_grouping_decision) and the bulk backtest
    (backtest_scored_model) - one real implementation, so the two can
    never silently disagree about what a signal means."""
    if issue is None:
        issue = ws.get_issue(issue_id)
    parties = ws.list_parties_for_issue(issue_id)
    companies = {
        p["company"].lower() for p in parties
        if p.get("affiliation") == "external" and p.get("company")
        and not workgraph_signals.is_automated_sender(p.get("primary_email") or "")
    }
    internal = {p["id"] for p in parties if p.get("affiliation") == "internal"}
    party_ids = {
        p["id"] for p in parties
        if p.get("affiliation") == "external" and not workgraph_signals.is_automated_sender(p.get("primary_email") or "")
    }
    has_external = any(p.get("affiliation") == "external" for p in parties)
    topic_key = ws.normalize_topic_key(issue.get("title") or "") if has_external else ""
    return {
        "id": issue_id,
        "companies": companies,
        "internal": internal,
        "party_ids": party_ids,
        "topic_key": topic_key if len(topic_key) >= MIN_TOPIC_KEY_LEN else "",
        "references": reference_base_ids_for_issue(issue_id),  # base, not full - see that function's docstring
        "category": issue.get("category"),
        "project_id": issue.get("project_id"),
    }


def _ariba_supplier_for_work_object(work_object_id: str) -> Optional[str]:
    """The real vendor name (e.g. "AUTHENTICX INC"), read from every
    raw_item linked to this work_object's own full body text - first
    supplier-bearing body wins. 2026-08-05 (Marc's direct design ask, live
    on the Authenticx case): the Ariba line-item table's "Supplier" field
    only ever appears in the BODY, never the subject/title
    compute_work_object_signature's ariba_fields already reads - and it
    names the real vendor, not the notification system that carried it
    (workgraph_signals.extract_ariba_supplier_field's own guard). Without
    this, two different Authenticx PRs shared NO supplier signal at all:
    is_automated_sender correctly excludes the ansmtp.ariba.com sender from
    party/company matching, and nothing else ever populated a real
    party.company for an Ariba-routed requisition."""
    for item in ws.get_raw_items_for_issue(work_object_id):
        supplier = workgraph_signals.extract_ariba_supplier_field(text_extract.resolve_item_text(item))
        if supplier:
            return supplier
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
    unused for grouping). ariba_supplier (2026-08-05) is the real vendor
    name out of the body's own line-item table - see
    _ariba_supplier_for_work_object's own docstring for why this is a
    separate producer from ariba_fields, which only ever reads the title.
    negative_vocabulary stays None - still no real producer for it. Returns
    plain Python values (lists/sets as lists) -
    get_or_compute_work_object_signature is what JSON-encodes for the
    cache."""
    if issue is None:
        issue = ws.get_issue_or_cluster(work_object_id)
    ariba_fields = workgraph_signals.extract_ariba_requisition_fields(issue.get("title") or "") if issue else None
    value_amount = workgraph_nba.value_amount_for_issue(work_object_id)
    ariba_supplier = _ariba_supplier_for_work_object(work_object_id)
    positive_vocabulary = None
    if ariba_fields or value_amount or ariba_supplier:
        positive_vocabulary = {
            "ariba_requester": (ariba_fields or {}).get("requester"),
            "ariba_descriptor": (ariba_fields or {}).get("descriptor"),
            "value_amount": value_amount or None,
            "ariba_supplier": ariba_supplier,
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


def get_or_compute_work_object_signature(work_object_id: str, issue: Optional[dict] = None) -> dict:
    """Cache-first read (design doc Section 12.7) - a cached row survives
    until the real write sites that change its content invalidate it (see
    workgraph_store.invalidate_work_object_signature's callers), so a
    given work_object's signature is normally computed ONCE, not on every
    single scored_grouping_decision call that happens to consider it as a
    candidate."""
    cached = ws.get_work_object_signature(work_object_id)
    if cached is not None:
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
        company in common, or a matching Ariba-extracted real vendor name
        (workgraph_signals.extract_ariba_supplier_field, 2026-08-05, Marc's
        direct design ask) read from the requisition's own line-item table,
        never the notification system's own address. Company-name
        comparison is normalized (workgraph_signals.normalize_company_name)
        so a tracked party's "Authenticx" and the Ariba field's formal
        "AUTHENTICX INC" count as the same real vendor.
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
    # Real vendor from the Ariba body field folds into the SAME "supplier"
    # signal as tracked party companies, normalized so "Authenticx" (a
    # party record) and "AUTHENTICX INC" (the requisition's own field)
    # compare equal - see normalize_company_name's own docstring. Without
    # this fold-in, an Ariba-routed requisition (whose only sender is
    # excluded from party/company matching by design) never carried ANY
    # supplier signal at all, real vendor or not.
    a_suppliers = {workgraph_signals.normalize_company_name(o) for o in a_sig["external_orgs"]}
    b_suppliers = {workgraph_signals.normalize_company_name(o) for o in b_sig["external_orgs"]}
    a_suppliers.add(workgraph_signals.normalize_company_name(a_vocab.get("ariba_supplier")))
    b_suppliers.add(workgraph_signals.normalize_company_name(b_vocab.get("ariba_supplier")))
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

    return points


# Fixed 2026-08-05 (real regression, found via the 4 tests that failed once
# scored_model_enabled went live): the OLD ordered model (_strong_signal_
# match, retired from group_issue's live path but still tested directly)
# deliberately distinguished a "link" suggestion (party/company overlap
# alone - proves a relationship, not the same transaction - e.g. the real
# marc-166/marc-063 shape: same H1 contact, exiting one contract vs.
# negotiating a different new one) from a "merge" suggestion (party overlap
# PLUS real content overlap - same topic/product/amount/document - which
# actually looks like the same transaction). group_issue's new count-based
# "candidate" branch (task #184) never carried this distinction forward -
# it hardcoded suggestion_kind="merge" for every candidate regardless of
# which points matched, silently dropping a real, validated design
# decision, not superseding it.
#
# "reference" is deliberately excluded from _CONTENT_OVERLAP_SIGNALS - a
# genuinely shared reference ID never reaches a "candidate" verdict at all
# (scored_grouping_decision's _shared_reference_id check turns it into an
# immediate auto_merge before this ever runs), so it can't appear here.
_CONTENT_OVERLAP_SIGNALS = {"subject_entity", "product_service", "amount", "document"}


def _suggestion_kind_for_matched_signals(matched_signals: list) -> str:
    """"merge" when real CONTENT overlaps (same topic/product/amount/
    document - looks like the same transaction), "link" when only
    party/company overlap ("supplier"/"stakeholder") matched - a real
    relationship, not proof of the same transaction. Same semantics
    _strong_signal_match's own party-with-topic-overlap-vs-without split
    used, just re-derived from the count-based point vocabulary instead of
    a bespoke check."""
    return "merge" if any(s in _CONTENT_OVERLAP_SIGNALS for s in matched_signals) else "link"


def scored_grouping_decision(issue_id: str, issue: dict, *, lookback_days: Optional[int] = None) -> dict:
    """The deterministic candidate-detection verdict for ONE issue - always
    computed by group_issue() regardless of whether config('grouping',
    'scored_model_enabled') is on, so it can be shadow-logged for
    comparison against the real (ordered-model) decision before ever being
    trusted to act. Returns {verdict: auto_merge|candidate|bridge|no_match,
    match_count, sibling_id, matched_signals, bridged_projects}.

    Task #184 (2026-08-04, Marc's direct redesign): no more weighted score,
    no more AUTO_MERGE_THRESHOLD/WEAK_SUGGESTION_FLOOR, no more confidence-
    spine damping in this decision at all - see _matched_data_points' own
    docstring and this module's top-of-file comment for the full "why".
    "candidate" (2+ matched data points against ANY issue - a fresh
    ungrouped one, or a member of an existing project) replaces the old
    auto_merge/suggest split entirely: every candidate is real judgment
    territory now, not something the deterministic layer half-decides via
    a numeric cutoff. Only an exact shared reference ID (PR/PO) stays
    standalone-sufficient for an immediate auto_merge - a real shared
    transaction key, not an inferred signal like the rest of Marc's list -
    flagged in this module's own top comment as a deliberate, open-to-
    correction exception to "always 2+".

    lookback_days (task #177): the candidate search below defaults to
    _candidate_pool's own scope (open issues, plus closed ones within
    GROUPING_LOOKBACK_GRACE_DAYS) - pass an explicit override for the "look
    back further" case Marc asked for as a real, occasional, worker-
    triggered action (see group_issue's own lookback_days passthrough and
    POST /api/workgraph/issues/{id}/regroup), not a standing setting."""
    m = _shared_reference_id(issue_id)
    if m:
        ref, sibling_id = m
        return {"verdict": "auto_merge", "match_count": 1, "sibling_id": sibling_id,
                "matched_signals": ["reference"], "bridged_projects": {}}

    my_project_id = issue.get("project_id")
    my_sig = get_or_compute_work_object_signature(issue_id, issue)
    my_topic_key = _topic_key_for_signature(issue, my_sig)
    best_points, best_sibling = [], None
    # best-per-project (task #169/#170, 2026-08-04, Marc's direct design ask):
    # tracks the best (most data points matched) candidate PER DISTINCT
    # existing project so group_issue() below can tell a clean single-
    # project match from a genuine bridge between two already-established
    # projects - unchanged by task #184's count-based rewrite, just now
    # keyed by point-count instead of score.
    project_best: dict[str, tuple[list, str]] = {}
    # Task #177: scoped to _candidate_pool's open-plus-grace-period window
    # by default (was every issue ever created, no time bound at all).
    for other in _candidate_pool(lookback_days=lookback_days):
        if other["id"] == issue_id:
            continue
        if my_project_id and my_project_id == other.get("project_id"):
            continue  # already the same project - nothing to decide
        other_sig = get_or_compute_work_object_signature(other["id"], other)
        other_topic_key = _topic_key_for_signature(other, other_sig)
        points = _matched_data_points(
            issue_id, my_sig, my_topic_key,
            other["id"], other_sig, other_topic_key,
        )
        if len(points) < 2:
            continue  # Marc's floor: fewer than 2 real data points is never a candidate, period
        # Task #184 Phase D (2026-08-05): every pair clearing the 2+-point
        # bar gets persisted into the relationship graph as a byproduct of
        # detection - not just returned to this call's one caller. This is
        # what lets a FUTURE pass check "have I already decided this pair"
        # (ws.get_work_object_relationship) instead of recomputing/re-
        # suggesting against the whole corpus again - the "don't keep
        # mapping the whole backlog over and over" requirement. Silently a
        # no-op against an already-confirmed/rejected pair (upsert_work_
        # object_relationship's own guarantee) - a fresh detection here
        # never re-litigates a real prior judgment.
        ws.upsert_work_object_relationship(
            a_id=issue_id, b_id=other["id"], relationship_type="candidate",
            match_count=len(points), matched_signals=points,
        )
        other_project_id = other.get("project_id")
        if other_project_id:
            current = project_best.get(other_project_id)
            if current is None or len(points) > len(current[0]):
                project_best[other_project_id] = (points, other["id"])
        if len(points) > len(best_points):
            best_points, best_sibling = points, other["id"]

    # Bridge detection: 2+ DISTINCT already-established projects each with
    # their own qualifying (2+-point) member means this item connects two
    # real, separate groups - exactly the case that needs a real judgment
    # call (which one, both, or neither), not a blind pick of whichever
    # matched the most points.
    bridged_projects = {pid: (pts, sib) for pid, (pts, sib) in project_best.items()}

    if len(bridged_projects) >= 2:
        verdict = "bridge"
        # Re-tag each bridged pair's already-persisted 'candidate' row as
        # 'bridge' - a real distinct shape curator's review queue needs to
        # see (this issue connects 2+ established projects at once, not
        # just one ambiguous pair), not something to reconstruct at read
        # time from a flat candidate list.
        for pts, sib in bridged_projects.values():
            ws.upsert_work_object_relationship(
                a_id=issue_id, b_id=sib, relationship_type="bridge",
                match_count=len(pts), matched_signals=pts,
            )
    elif best_sibling:
        verdict = "candidate"
    else:
        verdict = "no_match"

    return {"verdict": verdict, "match_count": len(best_points),
            "bridged_projects": {pid: {"match_count": len(pts), "sibling_id": sib, "matched_signals": pts}
                                  for pid, (pts, sib) in bridged_projects.items()},
            "sibling_id": best_sibling if verdict != "no_match" else None, "matched_signals": best_points}


def backtest_scored_model() -> dict:
    """The required gate before config('grouping','scored_model_enabled')
    is ever set to true - READ-ONLY, changes nothing. Runs the deterministic
    candidate-detection model (task #184, 2026-08-04) against every real
    pair in the current corpus and reports the two things that matter:
    (a) same-project pairs that DON'T clear the 2+-data-point candidate bar
        - informational (a real merge the new model wouldn't have found on
        its own; not necessarily wrong, just worth knowing).
    (b) different-project (or one/both ungrouped) pairs that DO clear it -
        the actual false-positive-risk class that matters (the exact task
        #81 shape) and needs human review before the flag is ever enabled.
    No more "actual_verdict" distinction between raw-score and real
    composition - there's no score left to diverge from the composition;
    clearing the bar (2+ matched data points) and being a real candidate
    are now the same fact, not two things that could disagree.

    Every issue's signature is computed ONCE up front (real queries, O(n)),
    then compared pairwise purely in memory (O(n^2) cheap set ops) - fast
    even at real corpus scale. Section 12.7: reads/writes the same cached
    work_object_signatures table the live path does (a full backtest run
    is itself a real, honest way to warm the cache for every issue in the
    corpus), scored via _matched_data_points - including its cannot_merge/
    cannot_link veto, so a backtest run now also reports whether that veto
    changes any real pair's verdict."""
    issues = ws.list_issues(states=None, limit=10000)
    sigs = {}
    topic_keys = {}
    projects = {}
    for i in issues:
        sigs[i["id"]] = get_or_compute_work_object_signature(i["id"], i)
        topic_keys[i["id"]] = _topic_key_for_signature(i, sigs[i["id"]])
        projects[i["id"]] = i.get("project_id")
    ids = list(sigs.keys())

    same_project_below_bar = []
    different_project_candidates = []
    for idx, a_id in enumerate(ids):
        for b_id in ids[idx + 1:]:
            points = _matched_data_points(
                a_id, sigs[a_id], topic_keys[a_id],
                b_id, sigs[b_id], topic_keys[b_id],
            )
            same_project = bool(projects[a_id] and projects[a_id] == projects[b_id])
            if same_project and len(points) < 2:
                same_project_below_bar.append(
                    {"a": a_id, "b": b_id, "matched_signals": points, "project_id": projects[a_id]})
            elif not same_project and len(points) >= 2:
                different_project_candidates.append({
                    "a": a_id, "b": b_id, "match_count": len(points), "matched_signals": points,
                    "a_project": projects[a_id], "b_project": projects[b_id],
                })
    return {
        "issues_checked": len(ids),
        "same_project_pairs_below_threshold": same_project_below_bar,
        "different_project_pairs_at_or_above_threshold": different_project_candidates,
    }


RETROACTIVE_REPROCESS_LOOKBACK_DAYS = 3650


def run_retroactive_scored_reprocess(*, apply: bool = False, lookback_days: int = RETROACTIVE_REPROCESS_LOOKBACK_DAYS) -> dict:
    """Task #180 - Marc's explicit request once grouping v3 was built and
    reviewed: "once the build is complete, you need to go back over
    everything in the db with this new process." group_issue() can't do
    this on its own - it early-returns "already_grouped" for any issue that
    already has a project_id, which is exactly the structural gap tasks
    #170-173 fixed for NEW items joining an existing project but never
    applied to the EXISTING corpus. The whole point of a retroactive pass
    is reconsidering issues that already have a home, since that's where
    the real fragmentation lives (SAP split across 8 projects, LEAH across
    3 - Marc's own original complaint that started this phase).

    Requires config('grouping','scored_model_enabled') already on - this
    reprocesses the corpus AS the now-live model would act on it, not as a
    second, different simulation; enable the flag first, same order Marc
    asked for ("enable it" then "go back over everything").

    apply=False (default) is the required dry run - computes and reports
    every verdict, writes nothing. Only apply=True actually merges/creates
    suggestions. lookback_days defaults to a decade, NOT the live 45-day
    grace period (GROUPING_LOOKBACK_GRACE_DAYS) - a retroactive pass exists
    specifically to reconnect OLD, possibly-closed history; the live
    default would defeat its own purpose.

    Every real decision still goes through the SAME safety machinery the
    live path uses - merge_issues' deferred-reconciliation guard (a
    contested collision between two already-multi-member projects becomes
    a reviewable merge_projects suggestion, never a silent double-merge),
    create_project_suggestion's idempotent reuse, and the bridge-candidate
    suggestion shape curator's routine already knows how to judge. Nothing
    here bypasses human review for anything short of a clean, uncontested
    match.

    Processed oldest-first (opened_at) so an established project's own
    long history is considered before whatever fragment split off it
    later, and each issue is re-fetched fresh right before scoring (not
    read from the initial snapshot) so an earlier merge in the SAME pass
    is reflected in later decisions - deliberate, since reconnecting one
    fragment can change what the next one should now match too."""
    if apply and not bool(config.get("grouping", "scored_model_enabled")):
        raise RuntimeError(
            "run_retroactive_scored_reprocess(apply=True) refuses to run while "
            "grouping.scored_model_enabled is off - enable the flag first."
        )
    stubs = sorted(ws.list_issues(states=None, limit=10000), key=lambda i: i.get("opened_at") or 0)
    auto_merged, deferred, suggested, bridged, errors = [], [], [], [], []
    no_match_count = 0

    for stub in stubs:
        issue_id = stub["id"]
        issue = ws.get_issue(issue_id)
        if issue is None:
            continue
        try:
            decision = scored_grouping_decision(issue_id, issue, lookback_days=lookback_days)
        except Exception as e:
            errors.append({"issue_id": issue_id, "error": str(e)})
            continue

        if decision["verdict"] == "bridge":
            entry = {"issue_id": issue_id, "bridges": []}
            for pid, info in decision["bridged_projects"].items():
                reason = (f"retroactive reprocess: bridge candidate - connects to project {pid} via "
                          f"{info['match_count']} matching data points ({','.join(info['matched_signals'])})")
                if apply:
                    ws.create_project_suggestion(
                        issue_id_a=issue_id, issue_id_b=info["sibling_id"], reason=reason, suggestion_kind="merge",
                    )
                entry["bridges"].append({"project_id": pid, "sibling_id": info["sibling_id"]})
            bridged.append(entry)
        elif decision["verdict"] == "auto_merge":
            sibling_id = decision["sibling_id"]
            sibling = ws.get_issue(sibling_id)
            if issue.get("project_id") and sibling and issue["project_id"] == sibling.get("project_id"):
                continue  # already the same project - nothing to do, don't count as a change
            reason_label = "retroactive reprocess: shared reference ID"
            record = {"issue_id": issue_id, "sibling_id": sibling_id, "signals": decision["matched_signals"]}
            if apply:
                result = merge_issues(issue_id, sibling_id, reason_label=reason_label)
                if result["status"] == "deferred":
                    deferred.append({**record, "suggestion_id": result["suggestion_id"]})
                else:
                    auto_merged.append({**record, "project_id": result["project_id"]})
            else:
                auto_merged.append(record)
        elif decision["verdict"] == "candidate":
            sibling_id = decision["sibling_id"]
            reason = (f"retroactive reprocess: {decision['match_count']} matching data points "
                      f"({','.join(decision['matched_signals'])})")
            if apply:
                ws.create_project_suggestion(issue_id_a=issue_id, issue_id_b=sibling_id, reason=reason, suggestion_kind="merge")
            suggested.append({"issue_id": issue_id, "sibling_id": sibling_id,
                               "signals": decision["matched_signals"], "match_count": decision["match_count"]})
        else:
            no_match_count += 1

    return {
        "apply": apply, "issues_checked": len(stubs),
        "auto_merged": auto_merged, "deferred_reconciliation": deferred,
        "suggested": suggested, "bridged": bridged,
        "no_match_count": no_match_count, "errors": errors,
    }


def merge_issues(issue_id_a: str, issue_id_b: str, *, reason_label: str) -> dict:
    """The one place two issues actually become the same project - joins
    whichever of the two already has a project, or creates a new one.
    Shared by the deterministic strong-signal path and by a confirmed
    project-suggestion (Marc's own call, or curator's LLM judgment on the
    weak-signal residue).

    Returns {"status": "merged", "project_id": ...} - OR, since 2026-07-31
    (step 5, mandatory reconciliation), {"status": "deferred",
    "suggestion_id": ..., "winner_project_id": ..., "loser_project_id": ...}
    when merging would collide two ALREADY-established projects (see
    workgraph_store.merge_issues_txn's own docstring). Every caller must
    check "status" - see group_issue()'s _merge_or_defer helper.

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


def _confirm_link_suggestion(sugg: dict, suggestion_id: int, link_type: str = "related") -> dict:
    """Confirming a 'link' suggestion creates a project_links row between
    whichever two projects the two issues actually belong to - a link is
    inherently a project-to-project relationship, never an issue-to-issue
    one (see project_links' own schema comment on why). Creates a
    standalone project for either side that doesn't have one yet, rather
    than requiring both issues to already be grouped before a link can
    exist. Defaults link_type to 'related' - the vaguest correct value;
    _strong_signal_match's own docstring explains why a causal type
    (enables/depends_on/etc.) can't be mechanically inferred and must stay
    a human's call, not guessed here."""
    issue_a = ws.get_issue_or_cluster(sugg["issue_id_a"])
    issue_b = ws.get_issue_or_cluster(sugg["issue_id_b"])
    project_a = issue_a.get("project_id") if issue_a else None
    project_b = issue_b.get("project_id") if issue_b else None
    if not project_a and issue_a:
        parties = ws.list_parties_for_issue(sugg["issue_id_a"])
        project_a = ws.create_project_with_new_id(
            name=_project_name_for(issue_a, issue_a.get("category"), parties), category=issue_a.get("category"))
        ws.assign_issue_to_project(sugg["issue_id_a"], project_a, reason="standalone project for a confirmed link")
    if not project_b and issue_b:
        parties = ws.list_parties_for_issue(sugg["issue_id_b"])
        project_b = ws.create_project_with_new_id(
            name=_project_name_for(issue_b, issue_b.get("category"), parties), category=issue_b.get("category"))
        ws.assign_issue_to_project(sugg["issue_id_b"], project_b, reason="standalone project for a confirmed link")
    link_id = ws.create_project_link(from_project_id=project_a, to_project_id=project_b,
                                      link_type=link_type, reason=sugg["reason"], created_by="confirmed suggestion")
    ws.resolve_project_suggestion(suggestion_id, "confirmed")
    return {"action": "linked", "from_project_id": project_a, "to_project_id": project_b,
            "link_type": link_type, "link_id": link_id}


def _confirm_merge_projects_suggestion(sugg: dict, suggestion_id: int) -> dict:
    """Executes an explicitly-human-authorized project collision (step 5,
    mandatory reconciliation) - re-fetches both issues' CURRENT project_id
    (not the suggestion-creation-time snapshot; state may have changed
    since - e.g. one side already reassigned by something else) before
    doing the actual reassign-and-archive work. issue_id_a's project always
    wins - same tie-break merge_issues_txn's own collision branch already
    used before this gate existed.

    Corrected pipeline Phase C: get_issue_or_cluster - either side of a
    'merge_projects' reconciliation can be a cluster now (merge_issues_
    txn's own collision check, would_collide_established_projects, is
    already cluster-aware)."""
    issue_a = ws.get_issue_or_cluster(sugg["issue_id_a"])
    issue_b = ws.get_issue_or_cluster(sugg["issue_id_b"])
    project_a = issue_a.get("project_id") if issue_a else None
    project_b = issue_b.get("project_id") if issue_b else None
    if not (project_a and project_b and project_a != project_b):
        # State changed since this suggestion was created - nothing left
        # to reconcile (e.g. one side was already merged/archived by
        # something else). Resolve the row so it stops showing as pending
        # rather than erroring on stale data.
        ws.resolve_project_suggestion(suggestion_id, "confirmed")
        return {"action": "no_op_state_changed"}
    project_id = ws.force_merge_projects(project_a, project_b, reason_label="confirmed reconciliation")
    ws.resolve_project_suggestion(suggestion_id, "confirmed")
    return {"action": "merged", "project_id": project_id}


def confirm_suggestion(suggestion_id: int, *, link_type: str = "related") -> dict:
    """Confirming a suggestion now actually merges the two issues (the
    pre-existing gap Marc flagged: 'Confirm' used to just mark it reviewed
    with no real effect). Called both from a human clicking Confirm in the
    cockpit and from curator's LLM judgment on weak-signal residue.

    Also records a Total Recall lesson for this outcome (free plumbing on a
    decision already made - see workgraph_lessons.record_confirmed_or_rejected).
    A rejected/invalid lesson write (e.g. no real category+company signal on
    this pair) is a normal, silent no-op here, never a failure of the merge
    itself.

    2026-07-31 (related-vs-same-project verdict): a 'link'-kind suggestion
    confirms into a project_links row instead of a merge (link_type lets
    the caller upgrade past the default 'related' if a human already knows
    the specific relationship - e.g. 'enables'). Deliberately does NOT
    record a Total Recall lesson for a link outcome - situation_key's
    precedent bucket is about "same project or not," and recording a link
    confirmation/rejection there would contaminate FUTURE merge-precedent
    decisions for the same category+company with a different question's
    answer (see create_project_suggestion's own docstring: link precedent
    isn't trusted to auto-apply anything yet, on purpose).

    2026-07-31 (step 5, mandatory reconciliation): a 'merge_projects'-kind
    suggestion executes the explicitly-authorized collision (see
    _confirm_merge_projects_suggestion). A confirmed plain 'merge' can ALSO
    now come back deferred instead of merged - a real race where the pair
    itself started colliding two established projects between suggestion-
    creation and confirm time - surfaced as its own new reconciliation
    suggestion rather than silently succeeding with a wrong id."""
    sugg = ws.get_project_suggestion(suggestion_id)
    if sugg is None:
        return {"action": "not_found"}
    kind = sugg.get("suggestion_kind")
    if kind == "link":
        return _confirm_link_suggestion(sugg, suggestion_id, link_type=link_type)
    if kind == "merge_projects":
        return _confirm_merge_projects_suggestion(sugg, suggestion_id)
    result = merge_issues(sugg["issue_id_a"], sugg["issue_id_b"], reason_label="confirmed suggestion")
    ws.resolve_project_suggestion(suggestion_id, "confirmed")
    workgraph_lessons.record_confirmed_or_rejected(issue_id_a=sugg["issue_id_a"], status="confirmed")
    if result["status"] == "deferred":
        return {"action": "deferred_reconciliation", "suggestion_id": result["suggestion_id"]}
    # Design doc Section 12.8: a real confirm event (a human click, or
    # curator's own LLM judgment on weak-signal residue - both are a
    # deliberate review of THIS specific pair, a qualitatively stronger
    # signal than the raw auto-merge threshold alone) - both sides just
    # joined the same project together, so both get marked 'confirmed',
    # never just the winner.
    ws.confirm_work_object_membership(sugg["issue_id_a"])
    ws.confirm_work_object_membership(sugg["issue_id_b"])
    return {"action": "merged", "project_id": result["project_id"]}


def reject_suggestion(suggestion_id: int) -> dict:
    sugg = ws.get_project_suggestion(suggestion_id)
    ws.resolve_project_suggestion(suggestion_id, "rejected")
    # 2026-07-31: only a 'merge' rejection feeds Total Recall precedent -
    # same reasoning as confirm_suggestion's own docstring above.
    if sugg is not None and sugg.get("suggestion_kind") == "merge":
        workgraph_lessons.record_confirmed_or_rejected(issue_id_a=sugg["issue_id_a"], status="rejected")
    # Design doc Section 12.6: a real human reject (this is the only caller,
    # server_lean.py's cockpit route) is exactly the producer for a durable
    # cannot_merge/cannot_link veto - unlike the pending-suggestion row this
    # just resolved, this memory doesn't expire, so the same pair can't
    # resurface a new suggestion once fresh evidence arrives (see
    # workgraph_store._create_project_suggestion_on, the consumer side).
    if sugg is not None and sugg.get("suggestion_kind") in ("merge", "link"):
        constraint_type = "cannot_merge" if sugg["suggestion_kind"] == "merge" else "cannot_link"
        ws.create_identity_constraint(
            constraint_type, sugg["issue_id_a"], sugg["issue_id_b"],
            reason=f"suggestion #{suggestion_id} rejected: {sugg.get('reason') or ''}",
            actor="marc",
        )
        # Section 12.7: cannot_link_ids is part of both sides' cached
        # signature - a stale cached signature from before this reject
        # would otherwise keep scoring this pair as if the constraint
        # didn't exist until something else happened to invalidate it.
        ws.invalidate_work_object_signature(sugg["issue_id_a"])
        ws.invalidate_work_object_signature(sugg["issue_id_b"])
    return {"action": "rejected"}


def resolve_work_object_relationship(relationship_id: int, status: str) -> dict:
    """Task #184 Phase F (2026-08-05): curator's real judgment call on one
    row from the persisted relationship graph (work_object_relationships),
    the eventual replacement for pending_project_suggestions as this
    review queue's source of truth. Deliberately reuses confirm_suggestion/
    reject_suggestion's ENTIRE existing safety net (deferred reconciliation
    on a contested collision, Total Recall lesson recording, the durable
    cannot_merge veto on reject, cached-signature invalidation) rather than
    re-implementing any of it - a real pending_project_suggestions row is
    created and immediately resolved as a mechanical bridge between the two
    review queues, not a second, parallel decision path that could ever
    disagree with the first about what confirming/rejecting a pair means.

    status must be 'confirmed' or 'rejected' - there is no third option
    here, same as project-suggestion resolution. A genuinely unsure
    verdict is simply never called with this function at all - the row
    stays 'candidate'/'bridge' for curator's own next pass (see
    ws.resolve_work_object_relationship's own docstring)."""
    if status not in ("confirmed", "rejected"):
        raise ValueError(f"resolve_work_object_relationship: invalid status {status!r}")
    rel = ws.get_work_object_relationship_by_id(relationship_id)
    if rel is None:
        raise ValueError(f"no such relationship: {relationship_id}")
    reason = f"relationship graph {rel['relationship_type']} (match_count={rel['match_count']})"
    suggestion_id = ws.create_project_suggestion(
        issue_id_a=rel["from_id"], issue_id_b=rel["to_id"], reason=reason, suggestion_kind="merge",
    )
    if suggestion_id is None:
        # A durable cannot_merge/cannot_link veto already exists for this
        # exact pair (create_project_suggestion's own check) - in normal
        # operation this shouldn't happen (a vetoed pair never clears
        # _matched_data_points' own veto check to get a relationship row
        # in the first place), but a constraint created by some OTHER path
        # after this row was written is a real, if rare, race. Confirming
        # against a live veto would contradict it silently - refuse rather
        # than pretend to succeed; rejecting is consistent with the veto
        # already in place, so that resolves cleanly.
        if status == "confirmed":
            raise ValueError(
                f"relationship {relationship_id} ({rel['from_id']}/{rel['to_id']}) is vetoed by an "
                "existing cannot_merge/cannot_link constraint - cannot confirm"
            )
        ws.resolve_work_object_relationship(relationship_id, "rejected")
        return {"action": "rejected", "note": "already vetoed by an existing identity_constraint"}
    if status == "confirmed":
        result = confirm_suggestion(suggestion_id)
    else:
        result = reject_suggestion(suggestion_id)
    ws.resolve_work_object_relationship(relationship_id, status)
    return result


# reject_suggestion has created a durable identity_constraint on every
# call since v2.4 (task #117) - but rejections that happened BEFORE that
# shipped never got one, so the same pair could still resurface today
# with no permanent veto. Real production data (2026-08-04) shows this
# is NOT a simple "convert every rejected row" backfill: of 870 rejected
# pending_project_suggestions rows, ~740 carry the identical auto-
# generated same_category_proximity reason text (the exact weak-signal
# flood Phase 0 (D2) later killed - see workgraph_store.
# expire_stale_project_suggestions), and the overwhelming majority
# resolve in dense same-second-or-tighter bursts of 2-40+ rows - the
# unmistakable signature of a scripted bulk pass, not one-by-one review.
# Only rows that are BOTH temporally isolated AND carry a specific,
# non-boilerplate per-pair reason plausibly went through a genuine
# reject_suggestion() review (a human click, or curator's own LLM
# judgment on weak-signal residue - see reject_suggestion's own
# docstring). This is deliberately conservative: a missed real rejection
# just means that pair could resurface and get rejected again, which
# costs nothing; a wrongly-backfilled constraint permanently blocks a
# pair that was never actually reviewed.
_WEAK_SIGNAL_PROXIMITY_REASON_RE = re.compile(
    r"^same category \('[^']*'\) within \d+d, no shared external contact found$"
)

_BACKFILL_KIND_TO_CONSTRAINT_TYPE = {"merge": "cannot_merge", "link": "cannot_link"}


def _is_explicit_review_rejection(row: dict, cluster_counts: Counter) -> bool:
    reason = (row.get("reason") or "").strip()
    if _WEAK_SIGNAL_PROXIMITY_REASON_RE.match(reason):
        return False
    resolved_ts = row.get("resolved_ts")
    if resolved_ts is None:
        return False
    if cluster_counts.get(round(resolved_ts), 0) > 1:
        return False
    return True


def backfill_identity_constraints_from_historical_rejections(*, apply: bool = False) -> dict:
    """Selective backfill (task #156) - always computes and returns the
    report; only WRITES identity_constraints when apply=True (the
    explicit migration flag; the default is a dry run). Idempotent: a
    pair that already has a durable constraint (from this backfill, or
    from reject_suggestion itself) is skipped, not duplicated - safe to
    re-run with apply=True after reviewing a dry-run report."""
    rejected = ws.list_project_suggestions(status="rejected")
    cluster_counts = Counter(round(r["resolved_ts"]) for r in rejected if r.get("resolved_ts"))

    eligible = []
    bulk_cleanup_artifact = 0
    non_constraint_kind = 0
    for row in rejected:
        if row.get("suggestion_kind") not in _BACKFILL_KIND_TO_CONSTRAINT_TYPE:
            non_constraint_kind += 1
            continue
        if not _is_explicit_review_rejection(row, cluster_counts):
            bulk_cleanup_artifact += 1
            continue
        eligible.append(row)

    already_constrained = 0
    created = []
    for row in eligible:
        constraint_type = _BACKFILL_KIND_TO_CONSTRAINT_TYPE[row["suggestion_kind"]]
        if ws.find_identity_constraint(constraint_type, row["issue_id_a"], row["issue_id_b"]) is not None:
            already_constrained += 1
            continue
        if apply:
            ws.create_identity_constraint(
                constraint_type, row["issue_id_a"], row["issue_id_b"],
                reason=f"backfilled from historical rejection of suggestion #{row['id']}: {row.get('reason') or ''}",
                actor="marc",
            )
        created.append({"suggestion_id": row["id"], "constraint_type": constraint_type,
                         "issue_id_a": row["issue_id_a"], "issue_id_b": row["issue_id_b"]})

    return {
        "rejected_rows_scanned": len(rejected),
        "eligible_explicit_rejects": len(eligible),
        "bulk_cleanup_artifact_rejects": bulk_cleanup_artifact,
        "non_constraint_kind_rejects": non_constraint_kind,
        "already_constrained": already_constrained,
        "new_constraints": len(created),
        "applied": apply,
        "detail": created,
    }


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


def group_issue(issue_id: str, *, lookback_days: Optional[int] = None) -> dict:
    """Runs the grouping logic for ONE issue. Safe to re-run -
    assign_issue_to_project and create_project_suggestion are both
    idempotent (the former no-ops if already assigned to the target project,
    the latter reuses an existing pending row for the same pair).

    Part A2 of the grouping/NBA redesign (2026-07-30): scored_grouping_
    decision() is ALWAYS computed, regardless of config('grouping',
    'scored_model_enabled') - attached to every return as "shadow_scored"
    for comparison against whichever model actually acted. Only when the
    flag is on does the scored model's verdict replace the ordered
    first-match model below (see backtest_scored_model()'s own docstring
    for the required review before ever enabling that flag).

    2026-07-31 follow-up: every decision below (once shadow_scored exists)
    is also persisted to workgraph_store.shadow_grouping_log via _finish -
    previously the shadow verdict was computed but discarded the moment
    this function returned, so there was no way to build a real historical
    dataset of live-vs-scored (dis)agreement to review before ever
    reconsidering the scored model.

    lookback_days (task #177): passed straight through to scored_grouping_
    decision's own candidate search - honest about the fact that until
    config('grouping','scored_model_enabled') is on (task #180), this only
    widens what gets shadow-logged, not what actually happens; the ordered
    model below has no lookback concept of its own and isn't getting one,
    since it's the path this whole phase is retiring, not extending. Real
    caller: POST /api/workgraph/issues/{id}/regroup, the worker-via-chat
    'look back further' action Marc asked for.

    Corrected pipeline Phase C (2026-08-05): get_issue_or_cluster, not
    get_issue - issue_id is now routinely a cluster (workgraph_classify.
    cluster_and_link no longer creates real issues directly, Phase B), and
    a cluster clearing the grouping bar is exactly what this function has
    to detect and promote into a real project."""
    issue = ws.get_issue_or_cluster(issue_id)
    if issue is None:
        return {"issue_id": issue_id, "action": "not_found"}
    if issue.get("project_id"):
        return {"issue_id": issue_id, "action": "already_grouped", "project_id": issue["project_id"]}

    shadow_scored = scored_grouping_decision(issue_id, issue, lookback_days=lookback_days)
    scored_model_enabled = bool(config.get("grouping", "scored_model_enabled"))

    def _finish(action: str, *, signal: Optional[str] = None, sibling_id: Optional[str] = None, **extra) -> dict:
        ws.log_shadow_grouping_decision(
            issue_id=issue_id, live_action=action, live_signal=signal, live_sibling_id=sibling_id,
            scored_verdict=shadow_scored["verdict"],
            # Task #184 (2026-08-04): no more numeric score to log - the
            # scored_score column now just holds the plain matched-data-
            # point count, so shadow-log comparisons still have SOME
            # numeric ranking signal to sort/filter on.
            scored_score=float(shadow_scored.get("match_count") or 0),
            scored_sibling_id=shadow_scored.get("sibling_id"),
            scored_signals_json=json.dumps(shadow_scored.get("matched_signals") or []),
        )
        result = {"issue_id": issue_id, "action": action, "shadow_scored": shadow_scored, **extra}
        if signal is not None:
            result["signal"] = signal
        return result

    def _merge_or_defer(sibling_id: str, reason_label: str, signal: str) -> dict:
        """2026-07-31 (step 5, mandatory reconciliation): merge_issues() can
        now come back 'deferred' instead of actually merging, when doing so
        would collide two ALREADY-established projects (2+ members on the
        losing side) - see merge_issues_txn's own docstring. Every call
        site that used to treat merge_issues()'s return as a bare
        project_id funnels through here so none of them can forget to
        check the new tagged result.

        Corrected pipeline Phase D (2026-08-05): every actual merge reaching
        this helper is a HIGH-confidence, no-suggestion-needed auto-merge
        (an exact shared reference, or Total Recall's repeated-confirmed-
        pattern precedent) - real confirmed-grade evidence, same reasoning
        confirm_suggestion's own docstring already uses for a human/curator
        explicit confirm ("both sides just joined the same project together,
        so both get marked 'confirmed'"). Without this, an auto_merge-only
        project (the common shape for an exact-reference cluster group -
        the one thing that's standalone-sufficient without ever going
        through the suggestion/confirm queue at all) would sit at
        membership_state='provisional' forever, and Phase D's own
        extraction trigger (a confirmed grouping) would never fire for it."""
        result = merge_issues(issue_id, sibling_id, reason_label=reason_label)
        if result["status"] == "merged":
            ws.confirm_work_object_membership(issue_id)
            ws.confirm_work_object_membership(sibling_id)
        if result["status"] == "deferred":
            return _finish("deferred_reconciliation", signal=signal, sibling_id=sibling_id,
                            suggestion_id=result["suggestion_id"], winner_project_id=result["winner_project_id"],
                            loser_project_id=result["loser_project_id"])
        return _finish("auto_merged", signal=signal, sibling_id=sibling_id, project_id=result["project_id"])

    if scored_model_enabled:
        if shadow_scored["verdict"] == "bridge":
            # Added task #169/#170 (2026-08-04, Marc's direct design ask):
            # this item connects 2+ already-established, previously-
            # separate projects - real judgment territory (which one, both
            # meaning they should merge, or neither), not safe to auto-pick
            # whichever matched the most points. The real LLM judgment call
            # this deserves isn't built yet (tracked separately) - in the
            # meantime, surface a suggestion against EVERY bridged
            # project's best-matching member so a human reviewing the
            # existing suggestion queue sees the real ambiguity, rather
            # than this silently collapsing to a single guess or a dropped
            # no_match.
            created = []
            for pid, info in shadow_scored["bridged_projects"].items():
                kind = _suggestion_kind_for_matched_signals(info["matched_signals"])
                reason = (f"bridge candidate - connects to project {pid} via "
                          f"{info['match_count']} matching data points ({','.join(info['matched_signals'])})")
                ws.create_project_suggestion(
                    issue_id_a=issue_id, issue_id_b=info["sibling_id"], reason=reason, suggestion_kind=kind,
                )
                created.append({"project_id": pid, "sibling_id": info["sibling_id"], "suggestion_kind": kind})
            return _finish("bridge_suggested", signal="scored", count=len(created), bridges=created)
        if shadow_scored["verdict"] == "auto_merge":
            # signal="reference", not the generic "scored" - the only path
            # that ever produces "auto_merge" is a genuinely shared
            # reference ID (_shared_reference_id), so this is always the
            # real reason, not just "the scored model decided this."
            reason_label = f"shared reference ID ({','.join(shadow_scored['matched_signals'])})"
            return _merge_or_defer(shadow_scored["sibling_id"], reason_label, "reference")
        if shadow_scored["verdict"] == "candidate":
            precedent = workgraph_lessons.precedent_prefilter(issue)
            if precedent == "confirmed":
                return _merge_or_defer(shadow_scored["sibling_id"],
                                        "auto-resolved by precedent (repeated confirmed pattern)", "precedent")
            if precedent != "rejected":
                matched_signals = shadow_scored["matched_signals"]
                kind = _suggestion_kind_for_matched_signals(matched_signals)
                reason = f"{shadow_scored['match_count']} matching data points ({','.join(matched_signals)})"
                ws.create_project_suggestion(
                    issue_id_a=issue_id, issue_id_b=shadow_scored["sibling_id"],
                    reason=reason, suggestion_kind=kind,
                )
                return _finish("suggested", signal="scored", sibling_id=shadow_scored["sibling_id"],
                                count=1, suggestion_kind=kind)
        return _finish("no_match")

    match = _strong_signal_match(issue_id, issue)
    if match:
        kind, detail, sibling_id, verdict = match
        reason_label = {
            "reference": f"strong signal: matching reference ID '{detail}'",
            "party": "strong signal: shared external party",
            "company": f"strong signal: shared external company '{detail}'",
            "topic": f"strong signal: matching subject core '{detail}'",
        }[kind]
        if kind == "reference":
            return _merge_or_defer(sibling_id, reason_label, kind)

        if verdict == "link":
            # 2026-07-31 (related-vs-same-project verdict): a bare shared
            # company (different people), or a shared party whose topic
            # keys DON'T overlap, prove a relationship - not the same
            # transaction (marc-166/marc-063: same H1 contact, exiting one
            # contract vs. negotiating a different new one). Never auto-
            # merge, and never auto-apply from precedent either (link
            # precedent isn't trusted yet - see create_project_suggestion's
            # own docstring) - always surface for a human to pick the real
            # relationship type (default 'related', upgradable on confirm).
            ws.create_project_suggestion(
                issue_id_a=issue_id, issue_id_b=sibling_id,
                reason=f"possibly related (not necessarily same project) - {reason_label}",
                suggestion_kind="link",
            )
            return _finish("suggested", signal=kind, sibling_id=sibling_id, count=1, suggestion_kind="link")

        # verdict == "merge" (party-with-topic-overlap, or topic-kind).
        # Narrowed 2026-07-31 (see _strong_signal_match's docstring): still
        # never auto-merge on these alone. Route through the same repeated-
        # confirmed-precedent check (a genuinely higher bar - multiple past
        # EXPLICIT human confirmations, not a single heuristic match) and
        # otherwise fall back to a suggestion instead of merging outright.
        precedent = workgraph_lessons.precedent_prefilter(issue)
        if precedent == "confirmed":
            return _merge_or_defer(sibling_id, "auto-resolved by precedent (repeated confirmed pattern)", "precedent")
        if precedent != "rejected":
            ws.create_project_suggestion(issue_id_a=issue_id, issue_id_b=sibling_id, reason=reason_label)
            return _finish("suggested", signal=kind, sibling_id=sibling_id, count=1)
        return _finish("no_match")

    # Phase 0 fix (D2, 2026-08-03): same-category-proximity candidate
    # generation is OFF by default (config('grouping',
    # 'same_category_proximity_suggestions_enabled')) - this whole branch
    # used to emit one suggestion PER candidate, unbounded, which is exactly
    # how the pending queue reached 2,004 rows (~99% eventually rejected).
    if not bool(config.get("grouping", "same_category_proximity_suggestions_enabled")):
        return _finish("no_match")

    candidates = _weak_signal_candidates(issue)
    if not candidates:
        return _finish("no_match")

    # Corroboration requirement, kept as belt-and-suspenders for whenever
    # this flag IS turned on: _strong_signal_match already searched
    # exhaustively across every other issue for shared party/company/topic
    # and found none, or this issue wouldn't have reached here - so the
    # only thing left that can genuinely corroborate a bare category match
    # is a shared INTERNAL sender. Deliberately NOT routed through
    # _matched_data_points (task #184) - Marc's redesign explicitly
    # excludes sender/participant from ever being a primary matching data
    # point, and this check is sender-only by construction (that's the
    # entire point of it - category alone isn't one of his data points
    # either). A category match with no such corroboration is dropped
    # rather than suggested.
    my_sig = get_or_compute_work_object_signature(issue_id, issue)
    my_internal = {p["party_id"] for p in my_sig["participant_roles"] if p.get("affiliation") == "internal"}
    corroborated = []
    for other in candidates:
        other_sig = get_or_compute_work_object_signature(other["id"], other)
        other_internal = {p["party_id"] for p in other_sig["participant_roles"] if p.get("affiliation") == "internal"}
        if my_internal and other_internal and not my_internal.isdisjoint(other_internal):
            corroborated.append(other)
    if not corroborated:
        return _finish("no_match")
    best = corroborated[0]

    # Total Recall precedent check, before asking curator (or Marc) at all -
    # see workgraph_lessons.precedent_prefilter. A 'confirmed' verdict merges
    # immediately; a 'rejected' verdict means don't even surface a suggestion.
    precedent = workgraph_lessons.precedent_prefilter(issue)
    if precedent == "confirmed":
        return _merge_or_defer(best["id"], "auto-resolved by precedent (repeated confirmed pattern)", "precedent")
    if precedent == "rejected":
        return _finish("no_match")

    # At most ONE suggestion per call, for the single best-corroborated
    # candidate - never one-per-candidate.
    ws.create_project_suggestion(
        issue_id_a=issue_id, issue_id_b=best["id"],
        reason=f"same category ('{issue.get('category')}') within {WEAK_SIGNAL_WINDOW_DAYS}d, corroborated by shared internal sender",
    )
    return _finish("suggested", sibling_id=best["id"], count=1)


def run_suggestion_expiry_daily_if_due(now: Optional[float] = None) -> Optional[dict]:
    """Phase 0 fix (D2, 2026-08-03): same once-a-day gate as retention/
    health_check/aristotle_detection (ws.claim_daily_run) - piggybacks
    scheduled_refresh.py's 5x/day cycle without sweeping 5x. Expires
    'pending' merge suggestions older than config('grouping',
    'weak_signal_ttl_days') to status='expired' (reversible bookkeeping,
    not a delete). Returns None on every call that isn't the day's first
    claim, a real checkable 'did not run' signal, matching the sibling
    gates' own convention."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not ws.claim_daily_run("suggestion_expiry", today):
        return None
    ttl_days = config.get("grouping", "weak_signal_ttl_days") or 21
    expired = ws.expire_stale_project_suggestions(ttl_days, kinds=("merge",))
    return {"expired": expired, "ttl_days": ttl_days}


def backfill_regroup_by_reference() -> dict:
    """One-time repair pass for Part A1 (2026-07-30): _shared_reference_id
    is a NEW signal - issues linked before this shipped may still be split
    across separate issues purely because they share only a reference ID,
    which nothing checked as a positive signal until now (the real known
    case: PR854779-V4 split across 3 separate issues). Re-runs group_issue()
    - documented idempotent, see its own docstring - for every currently-
    open issue that has at least one real reference ID. Only catches
    issues group_issue() itself would act on (i.e. not already assigned to
    a project) - matches group_issue()'s own scope, not a new mechanism.

    2026-07-31 (step 5): a run against a corpus with a real existing
    multi-project collision can now produce a 'deferred_reconciliation'
    count instead of fully resolving every candidate it touches - this
    function's premise ("only catches issues group_issue() itself would
    act on") no longer means every touched issue ends up merged-or-
    suggested; some now end up pending human reconciliation instead."""
    candidates = [
        i for i in ws.list_issues(states=["active", "waiting", "blocked"], limit=10000)
        if reference_ids_for_issue(i["id"])
    ]
    results = {"checked": len(candidates), "auto_merged": 0, "already_grouped": 0, "no_match": 0,
               "suggested": 0, "deferred_reconciliation": 0}
    for issue in candidates:
        action = group_issue(issue["id"])["action"]
        results[action] = results.get(action, 0) + 1
    return results


def find_relationship_links_for_grouped_issues() -> dict:
    """One-time (or on-demand) discovery pass, same shape and same 'catches
    what the live path structurally can't' premise as
    backfill_regroup_by_reference above. Real gap found 2026-08-01
    investigating a real Workday relationship: group_issue()'s very first
    line is `if issue.get("project_id"): return already_grouped` - once an
    issue has ANY project, it is NEVER evaluated again, for anything,
    including a genuine cross-project RELATIONSHIP (the "link" verdict
    _strong_signal_match already knows how to produce). Confirmed live:
    marc-308 (Workday Early Renewal, already in a project) and marc-014
    (Workday HCM SaaS renewal, a completely different real Workday deal)
    share the same external company - a real, useful "these are the same
    counterparty, worth knowing about" signal - but group_issue() never
    even computed it for marc-308, because "already_grouped" short-circuits
    before _shared_external_party/_shared_external_company are ever called.

    This does NOT touch group_issue()'s early return (that guard is correct
    for its own job - never re-litigate a settled MERGE) and does NOT merge
    anything itself - only ever creates a 'link'-kind suggestion (the same
    human-confirm-required, never-auto-applied path _strong_signal_match's
    own "link" verdict already uses), for a pair that isn't already in the
    same project and doesn't already have a pending suggestion (create_
    project_suggestion's own dedup).

    Fixed same-day (2026-08-01), confirmed live: the first version of this
    function called _shared_external_party/_shared_external_company, which
    each return only their OWN first match - by DESIGN, for their real job
    (_strong_signal_match only ever needs one strong signal to act on). But
    an issue with several external parties can have its FIRST party's first
    sibling be one already in the SAME project (nothing new), which made
    this function give up on that issue entirely - never trying its OTHER
    parties, which might have a genuinely new cross-project sibling. Exact
    real case that exposed it: marc-325 has 3 external parties (Dan
    Hatfield, Hhubert, Kimberley Davis); Dan Hatfield's first sibling
    (marc-005) already shares marc-325's project, so the old version quit
    right there - never reaching Hhubert, who has a real, genuinely
    unlinked sibling (marc-280, the "uMSA" issue - the exact real
    relationship this function exists to catch). Now checks EVERY external
    party, then EVERY external company, collecting every candidate rather
    than stopping at the first."""
    grouped = [i for i in ws.list_issues(states=["active", "waiting", "blocked"], limit=10000) if i.get("project_id")]
    # Pre-existing 'link' suggestions (any status - a rejected one shouldn't
    # be re-created either, same as a pending one) - checked in-memory so
    # this run's own two-direction processing (issue A finds B, then issue
    # B independently finds A) and any prior run both dedupe against the
    # SAME set, rather than only catching one of those two cases.
    seen_pairs = {
        frozenset((s["issue_id_a"], s["issue_id_b"]))
        for s in ws.list_project_suggestions(status="pending") + ws.list_project_suggestions(status="confirmed")
        + ws.list_project_suggestions(status="rejected")
        if s["suggestion_kind"] == "link"
    }
    checked = 0
    suggested = 0
    for issue in grouped:
        checked += 1
        parties = ws.list_parties_for_issue(issue["id"])
        candidates = []  # (kind, detail, sibling_id), party candidates before company
        for party in parties:
            if party.get("affiliation") != "external" or workgraph_signals.is_automated_sender(party.get("primary_email") or ""):
                continue
            for sibling_id in ws.list_issues_for_party(party["id"]):
                if sibling_id != issue["id"]:
                    candidates.append(("party", party["id"], sibling_id))
        for party in parties:
            if (party.get("affiliation") != "external" or not party.get("company")
                    or workgraph_signals.is_automated_sender(party.get("primary_email") or "")):
                continue
            for sibling_id in ws.list_issues_for_company(party["company"]):
                if sibling_id != issue["id"]:
                    candidates.append(("company", party["company"], sibling_id))

        for kind, detail, sibling_id in candidates:
            sibling = ws.get_issue(sibling_id)
            if not sibling:
                continue
            if sibling.get("project_id") == issue["project_id"]:
                continue  # already the same project (including both-None, which can't reach here since `issue` is always grouped) - try the next candidate
            if _vetoed_by_reference_mismatch(issue["id"], sibling_id):
                continue
            pair = frozenset((issue["id"], sibling_id))
            if pair in seen_pairs:
                continue
            ws.create_project_suggestion(
                issue_id_a=issue["id"], issue_id_b=sibling_id,
                reason=f"possibly related (not necessarily same project) - shared external {kind} '{detail}'",
                suggestion_kind="link",
            )
            seen_pairs.add(pair)
            suggested += 1
    return {"checked": checked, "suggested": suggested}


# Corrected pipeline Phase D data-point mapping (2026-08-05): a small local
# copy of workgraph_classify._evidence_type's own {source: type} mapping,
# not an import - workgraph_classify already imports THIS module (its
# cluster_and_link() calls workgraph_projects.run()), so importing it back
# here would be circular.
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

    return {"issue_id": new_issue_id, "project_id": project_id,
            "claims_moved": len(claims), "evidence_added": evidence_added}


def run(issue_ids: list) -> dict:
    results = [group_issue(i) for i in issue_ids]
    return {
        "processed": len(results),
        "auto_merged": sum(1 for r in results if r["action"] == "auto_merged"),
        "suggested": sum(1 for r in results if r["action"] == "suggested"),
        "no_match": sum(1 for r in results if r["action"] == "no_match"),
        "already_grouped": sum(1 for r in results if r["action"] == "already_grouped"),
        # 2026-07-31 (step 5): a real merge attempt can now be deferred to a
        # 'merge_projects' reconciliation suggestion instead of completing -
        # counted separately so it's visible, not silently absent from
        # every other bucket above.
        "deferred_reconciliation": sum(1 for r in results if r["action"] == "deferred_reconciliation"),
    }


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


if __name__ == "__main__":
    import json
    ws.init_workgraph()
    all_ids = ws.list_issue_ids()
    print(json.dumps(run(all_ids), indent=2))
