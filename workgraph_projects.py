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
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import workgraph_store as ws
import workgraph_confidence as confidence
import workgraph_lessons
import workgraph_signals

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
    at all."""
    my_refs = reference_base_ids_for_issue(issue_id)
    for ref in sorted(my_refs):
        for sibling_id in ws.list_open_issue_ids_for_reference(ref):
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


# --- Part A2 of the grouping/NBA redesign (2026-07-30): weighted multi- ---
# --- signal confidence model, computed alongside the ordered model above --
#
# Real production evidence this session: the ordered model above trusts
# ANY ONE of shared-company/matching-topic alone to auto-merge - the same
# shape that already caused a real bug (task #81: 71 issues wrongly merged
# because a subject-topic match alone fired). Marc's own instinct, checked
# against the real pipeline: a shared internal sender or a bare category
# match are each real but individually weak signals that should COMBINE
# for confidence rather than trust any one alone. Reference-ID match and
# shared-external-party stay standalone-sufficient (real, structurally
# unambiguous signals, unlike the other three which are all heuristics).
#
# No single one of the 4 combinable signals reaches AUTO_MERGE_THRESHOLD
# alone (max weight 0.40) - this directly closes the task #81 shape.
# Ships shadow-logged only (config('grouping','scored_model_enabled')
# defaults off) - group_issue() always computes this for comparison, but
# only ACTS on it once the flag is on, and the flag should only be
# switched on after backtest_scored_model()'s output has been reviewed
# (see that function's own docstring) - a hard gate, not a formality.
SCORE_WEIGHTS = {"company": 0.40, "topic": 0.40, "sender": 0.30, "category": 0.15, "party": 0.40}
AUTO_MERGE_THRESHOLD = 0.65
WEAK_SUGGESTION_FLOOR = 0.15  # preserves today's "one weak signal -> suggestion" behavior


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


def _pairwise_score(a: dict, b: dict):
    """Combined confidence score for two issues' precomputed signal
    snapshots (see _issue_signal_snapshot). Company/topic/sender/category/
    party each contribute a partial weight; none reaches AUTO_MERGE_THRESHOLD
    alone - including party (Phase 0 fix, 2026-08-03): a shared external
    party used to auto_merge on its own in scored_grouping_decision (the
    exact D4 shape - an account manager/consultant spans many concurrent,
    unrelated deals, same reasoning that already narrowed the LIVE grouping
    path off party-alone on 2026-07-31). Now it's one contributing signal
    like the others, requiring a second corroborating signal to cross
    AUTO_MERGE_THRESHOLD. A disjoint reference-ID pair is vetoed to 0
    regardless - the same absolute override the ordered model already
    applies. Returns (score, matched_signal_names)."""
    if a["references"] and b["references"] and a["references"].isdisjoint(b["references"]):
        return 0.0, []
    signals = []
    score = 0.0
    if a["party_ids"] and b["party_ids"] and not a["party_ids"].isdisjoint(b["party_ids"]):
        score += SCORE_WEIGHTS["party"]
        signals.append("party")
    if a["companies"] and b["companies"] and not a["companies"].isdisjoint(b["companies"]):
        score += SCORE_WEIGHTS["company"]
        signals.append("company")
    if a["topic_key"] and b["topic_key"]:
        m = SequenceMatcher(None, a["topic_key"], b["topic_key"]).find_longest_match(
            0, len(a["topic_key"]), 0, len(b["topic_key"]))
        if m.size >= MIN_TOPIC_KEY_LEN:
            score += SCORE_WEIGHTS["topic"]
            signals.append("topic")
    if a["internal"] and b["internal"] and not a["internal"].isdisjoint(b["internal"]):
        score += SCORE_WEIGHTS["sender"]
        signals.append("sender")
    if a["category"] and a["category"] != "other" and a["category"] == b["category"]:
        score += SCORE_WEIGHTS["category"]
        signals.append("category")
    return score, signals


def scored_grouping_decision(issue_id: str, issue: dict) -> dict:
    """The scored model's verdict for ONE issue - always computed by
    group_issue() regardless of whether config('grouping',
    'scored_model_enabled') is on, so it can be shadow-logged for
    comparison against the real (ordered-model) decision before ever
    being trusted to act. Returns {verdict: auto_merge|suggest|no_match,
    score, sibling_id, matched_signals}."""
    m = _shared_reference_id(issue_id)
    if m:
        ref, sibling_id = m
        return {"verdict": "auto_merge", "score": 1.0, "sibling_id": sibling_id, "matched_signals": ["reference"]}
    # Phase 0 fix (2026-08-03, D4): a standalone _shared_external_party ->
    # auto_merge(1.0) branch used to live here - the exact hazard the module
    # docstring's 2026-07-31 narrowing already removed from the LIVE grouping
    # path (_strong_signal_match), but this shadow-only scored model still
    # carried it verbatim. A shared party now flows into _pairwise_score's
    # "party" signal below instead, where it contributes but can't alone
    # cross AUTO_MERGE_THRESHOLD - same treatment as company/topic/sender.

    my_snapshot = _issue_signal_snapshot(issue_id, issue)
    best_score, best_sibling, best_signals = 0.0, None, []
    # Same limit fix as _shared_topic_key/_weak_signal_candidates above.
    for other in ws.list_issues(states=None, limit=10000):
        if other["id"] == issue_id:
            continue
        if my_snapshot["project_id"] and my_snapshot["project_id"] == other.get("project_id"):
            continue
        if other.get("project_id") and other.get("project_id") != my_snapshot["project_id"]:
            continue  # never target an issue already in a DIFFERENT project - same rule as _weak_signal_candidates
        other_snapshot = _issue_signal_snapshot(other["id"], other)
        score, signals = _pairwise_score(my_snapshot, other_snapshot)
        if score > best_score:
            best_score, best_sibling, best_signals = score, other["id"], signals

    if best_score >= AUTO_MERGE_THRESHOLD:
        verdict = "auto_merge"
    elif best_score >= WEAK_SUGGESTION_FLOOR:
        verdict = "suggest"
    else:
        verdict = "no_match"

    # Confidence spine v0 (2026-08-03) - OBSERVE-ONLY for this shadow-only
    # model: computed and attached for backtest_scored_model/shadow-log
    # review, but the verdict above is still decided on the raw ordered
    # score alone. Damping the actual threshold decision needs the same
    # backtest-before-trust discipline this module already requires before
    # scored_model_enabled ever flips on (see backtest_scored_model's own
    # docstring) - not something to wire in unreviewed.
    present = set()
    if my_snapshot["category"] and my_snapshot["category"] != "other":
        present.add("category")
    if my_snapshot["references"] or my_snapshot["party_ids"] or my_snapshot["companies"] or my_snapshot["topic_key"]:
        present.add("anchor_or_relationship")
    evidence_ts = [e["ts"] for e in ws.list_evidence(issue_id) if e.get("ts")]
    # Confidence spine v1: real identity_anchors when the backfill has
    # already covered this issue; falls back to the match_kind shim
    # (best_signals) when it hasn't, via context_accuracy's own None check.
    real_anchors = ws.list_identity_anchors(issue_id=issue_id)
    ctx = confidence.context_accuracy(
        present_fields=present, required_fields={"category", "anchor_or_relationship"},
        evidence_ts=evidence_ts, now=time.time(), match_kinds=best_signals,
        total_refs=1, unresolved_refs=0 if my_snapshot["references"] else 1,
        anchor_strengths=([a["anchor_strength"] for a in real_anchors] if real_anchors else None),
    )
    return {"verdict": verdict, "score": round(best_score, 2),
            "sibling_id": best_sibling if verdict != "no_match" else None, "matched_signals": best_signals,
            "context_accuracy": ctx["context_accuracy"],
            "effective_score": confidence.effective_score(best_score, ctx["context_accuracy"])}


def backtest_scored_model() -> dict:
    """The required gate before config('grouping','scored_model_enabled')
    is ever set to true - READ-ONLY, changes nothing. Runs the scored
    model against every real pair in the current corpus and reports the
    two things that matter:
    (a) same-project pairs the new model would now score BELOW threshold
        - informational (a real merge the new model wouldn't make on its
        own; not necessarily wrong, just worth knowing).
    (b) different-project (or one/both ungrouped) pairs the new model
        would now score AT/OR-ABOVE threshold - the actual false-positive
        class that matters (the exact task #81 shape) and needs human
        review before the flag is ever enabled.
    Every issue's signal snapshot is computed ONCE up front (real queries,
    O(n)), then compared pairwise purely in memory (O(n^2) cheap set ops)
    - fast even at real corpus scale."""
    issues = ws.list_issues(states=None, limit=10000)
    snapshots = {i["id"]: _issue_signal_snapshot(i["id"], i) for i in issues}
    ids = list(snapshots.keys())

    same_project_below_threshold = []
    different_project_at_or_above = []
    for idx, a_id in enumerate(ids):
        a = snapshots[a_id]
        for b_id in ids[idx + 1:]:
            b = snapshots[b_id]
            score, signals = _pairwise_score(a, b)
            same_project = bool(a["project_id"] and a["project_id"] == b["project_id"])
            if same_project and score < AUTO_MERGE_THRESHOLD:
                same_project_below_threshold.append(
                    {"a": a_id, "b": b_id, "score": round(score, 2), "project_id": a["project_id"]})
            elif not same_project and score >= AUTO_MERGE_THRESHOLD:
                different_project_at_or_above.append({
                    "a": a_id, "b": b_id, "score": round(score, 2), "signals": signals,
                    "a_project": a["project_id"], "b_project": b["project_id"],
                })
    return {
        "issues_checked": len(ids),
        "same_project_pairs_below_threshold": same_project_below_threshold,
        "different_project_pairs_at_or_above_threshold": different_project_at_or_above,
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
    other ws.* helpers)."""
    issue_a = ws.get_issue(issue_id_a)
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
    issue_a = ws.get_issue(sugg["issue_id_a"])
    issue_b = ws.get_issue(sugg["issue_id_b"])
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
    used before this gate existed."""
    issue_a = ws.get_issue(sugg["issue_id_a"])
    issue_b = ws.get_issue(sugg["issue_id_b"])
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
    return {"action": "merged", "project_id": result["project_id"]}


def reject_suggestion(suggestion_id: int) -> dict:
    sugg = ws.get_project_suggestion(suggestion_id)
    ws.resolve_project_suggestion(suggestion_id, "rejected")
    # 2026-07-31: only a 'merge' rejection feeds Total Recall precedent -
    # same reasoning as confirm_suggestion's own docstring above.
    if sugg is not None and sugg.get("suggestion_kind") == "merge":
        workgraph_lessons.record_confirmed_or_rejected(issue_id_a=sugg["issue_id_a"], status="rejected")
    return {"action": "rejected"}


def group_issue(issue_id: str) -> dict:
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
    reconsidering the scored model."""
    issue = ws.get_issue(issue_id)
    if issue is None:
        return {"issue_id": issue_id, "action": "not_found"}
    if issue.get("project_id"):
        return {"issue_id": issue_id, "action": "already_grouped", "project_id": issue["project_id"]}

    shadow_scored = scored_grouping_decision(issue_id, issue)
    scored_model_enabled = bool(config.get("grouping", "scored_model_enabled"))

    def _finish(action: str, *, signal: Optional[str] = None, sibling_id: Optional[str] = None, **extra) -> dict:
        ws.log_shadow_grouping_decision(
            issue_id=issue_id, live_action=action, live_signal=signal, live_sibling_id=sibling_id,
            scored_verdict=shadow_scored["verdict"], scored_score=shadow_scored["score"],
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
        check the new tagged result."""
        result = merge_issues(issue_id, sibling_id, reason_label=reason_label)
        if result["status"] == "deferred":
            return _finish("deferred_reconciliation", signal=signal, sibling_id=sibling_id,
                            suggestion_id=result["suggestion_id"], winner_project_id=result["winner_project_id"],
                            loser_project_id=result["loser_project_id"])
        return _finish("auto_merged", signal=signal, sibling_id=sibling_id, project_id=result["project_id"])

    if scored_model_enabled:
        if shadow_scored["verdict"] == "auto_merge":
            reason_label = f"scored signal ({','.join(shadow_scored['matched_signals'])}, score={shadow_scored['score']})"
            return _merge_or_defer(shadow_scored["sibling_id"], reason_label, "scored")
        if shadow_scored["verdict"] == "suggest":
            precedent = workgraph_lessons.precedent_prefilter(issue)
            if precedent == "confirmed":
                return _merge_or_defer(shadow_scored["sibling_id"],
                                        "auto-resolved by precedent (repeated confirmed pattern)", "precedent")
            if precedent != "rejected":
                ws.create_project_suggestion(
                    issue_id_a=issue_id, issue_id_b=shadow_scored["sibling_id"],
                    reason=f"scored signal ({','.join(shadow_scored['matched_signals'])}, score={shadow_scored['score']})",
                )
                return _finish("suggested", signal="scored", sibling_id=shadow_scored["sibling_id"], count=1)
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
    # only _pairwise_score signal left that can genuinely corroborate a bare
    # category match is a shared INTERNAL sender ("sender", which
    # _strong_signal_match never checks). A category match with no such
    # corroboration is dropped rather than suggested.
    my_snapshot = _issue_signal_snapshot(issue_id, issue)
    corroborated = []
    for other in candidates:
        score, signals = _pairwise_score(my_snapshot, _issue_signal_snapshot(other["id"], other))
        if any(s != "category" for s in signals):
            corroborated.append((score, other))
    if not corroborated:
        return _finish("no_match")
    corroborated.sort(key=lambda pair: pair[0], reverse=True)
    _, best = corroborated[0]

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
