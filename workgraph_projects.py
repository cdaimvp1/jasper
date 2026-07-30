"""
workgraph_projects.py — deterministic Project auto-grouping. No LLM calls.

Per Marc's explicit call: grouping should be automatic on a STRONG signal,
and only surfaced for confirmation when the signal is weak. Correction
happens through conversation with a worker (e.g. "no, split that back out"),
not a required review-queue step - so every grouping/reassignment operation
here is just a call to workgraph_store.assign_issue_to_project, the same
function a worker would call on Marc's behalf after he corrects one in chat.

Strong signal (auto-merge, no confirmation needed): two issues share at
least one EXTERNAL party. Sharing an INTERNAL party isn't a signal at all -
the same Lilly colleagues show up across dozens of unrelated threads, so
that alone proves nothing; a shared external supplier/counterparty contact
is what actually indicates "the same negotiation/relationship."

Originally this also required matching category, excluding 'other' as too
noisy a bucket to trust alone. Backfilling against Marc's real data showed
that requirement blocking almost every real grouping opportunity: 87% of
his issues (97/112) land in category='other' because the topic-regex
taxonomy (built for an 8-category procurement vocabulary) doesn't fit most
of his actual traffic, while the SAME external contacts (pwc.com, leahai.com,
moxo.com, veeva.com, salesforce.com...) clearly recur across multiple
issues regardless of category. A shared external party is treated as
sufficient signal on its own now; category still informs the project's
display name/label, it just isn't a gate on merging.

Weak signal (suggested, never auto-applied): same non-'other' category and
opened within a proximity window, but no shared external party - written to
pending_project_suggestions for Marc (or a worker relaying his answer) to
confirm or reject via workgraph_store.resolve_project_suggestion.
"""
from __future__ import annotations

import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws
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
    it - one real source, not two copies of the same read."""
    return {
        item["pr_number"].upper() for item in ws.get_raw_items_for_issue(issue_id)
        if item.get("pr_number")
    }


def _vetoed_by_reference_mismatch(issue_id: str, sibling_id: str) -> bool:
    """True when BOTH issues have at least one identified PR/PO reference
    and the sets are disjoint - a real, structured signal that overrides
    ANY of the strong-signal checks below (shared party/company/subject),
    since two different purchase requisitions are two different
    transactions no matter how similar their surrounding text looks."""
    my_refs = reference_ids_for_issue(issue_id)
    if not my_refs:
        return False
    sibling_refs = reference_ids_for_issue(sibling_id)
    return bool(sibling_refs) and my_refs.isdisjoint(sibling_refs)


def _project_name_for(issue: dict, category: str, parties: list) -> str:
    external = [p for p in parties if p.get("affiliation") == "external" and p.get("company")
                and not workgraph_signals._SYSTEM_SENDER.match(p.get("primary_email") or "")]
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
        if party.get("affiliation") != "external" or workgraph_signals._SYSTEM_SENDER.match(party.get("primary_email") or ""):
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
                or workgraph_signals._SYSTEM_SENDER.match(party.get("primary_email") or "")):
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
        and not workgraph_signals._SYSTEM_SENDER.match(p.get("primary_email") or "")
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


def _strong_signal_match(issue_id: str, issue: dict):
    """First strong deterministic signal found, checked in order of
    confidence: exact external party > shared external company > matching
    normalized subject/topic core. Any one is trusted enough to auto-merge
    without asking - UNLESS vetoed by a disjoint PR/PO reference number
    (see _vetoed_by_reference_mismatch above), which overrides all three.
    A candidate rejected on reference grounds is not retried against a
    weaker signal for the same pair; the safer failure mode is no_match,
    not risking a second wrong merge via a looser check. Returns
    (kind, detail, sibling_issue_id) or None."""
    m = _shared_external_party(issue_id)
    if m:
        party_id, sibling_id = m
        if not _vetoed_by_reference_mismatch(issue_id, sibling_id):
            return "party", party_id, sibling_id
    m = _shared_external_company(issue_id)
    if m:
        company, sibling_id = m
        if not _vetoed_by_reference_mismatch(issue_id, sibling_id):
            return "company", company, sibling_id
    sibling_id = _shared_topic_key(issue)
    if sibling_id and not _vetoed_by_reference_mismatch(issue_id, sibling_id):
        return "topic", ws.normalize_topic_key(issue.get("title") or ""), sibling_id
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


def merge_issues(issue_id_a: str, issue_id_b: str, *, reason_label: str) -> str:
    """The one place two issues actually become the same project - joins
    whichever of the two already has a project, or creates a new one.
    Shared by the deterministic strong-signal path and by a confirmed
    project-suggestion (Marc's own call, or curator's LLM judgment on the
    weak-signal residue). Returns the resulting project_id.

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
    project moves to the winning one, and the emptied loser is archived
    (workgraph_store.set_project_status - the exact mechanism task #81's
    one-off remediation used, now actually wired into the ongoing path
    instead of only ever being called by a test)."""
    issue_a = ws.get_issue(issue_id_a)
    issue_b = ws.get_issue(issue_id_b)
    project_a = issue_a.get("project_id")
    project_b = issue_b.get("project_id")

    if project_a and project_b and project_a != project_b:
        winner, loser = project_a, project_b
        for member in ws.list_issues_for_project(loser):
            if member["id"] in (issue_id_a, issue_id_b):
                continue  # reassigned explicitly below either way
            ws.assign_issue_to_project(member["id"], winner, reason=f"{reason_label}: project {loser} merged into {winner}")
        ws.set_project_status(loser, "archived")
        project_id = winner
    elif project_a:
        project_id = project_a
    elif project_b:
        project_id = project_b
    else:
        parties = ws.list_parties_for_issue(issue_id_a)
        category = issue_a.get("category")
        project_id = ws.create_project_with_new_id(name=_project_name_for(issue_a, category, parties), category=category)

    ws.assign_issue_to_project(issue_id_a, project_id, reason=f"{reason_label} with {issue_id_b}")
    ws.assign_issue_to_project(issue_id_b, project_id, reason=f"{reason_label} with {issue_id_a}")
    return project_id


def confirm_suggestion(suggestion_id: int) -> dict:
    """Confirming a suggestion now actually merges the two issues (the
    pre-existing gap Marc flagged: 'Confirm' used to just mark it reviewed
    with no real effect). Called both from a human clicking Confirm in the
    cockpit and from curator's LLM judgment on weak-signal residue.

    Also records a Total Recall lesson for this outcome (free plumbing on a
    decision already made - see workgraph_lessons.record_confirmed_or_rejected).
    A rejected/invalid lesson write (e.g. no real category+company signal on
    this pair) is a normal, silent no-op here, never a failure of the merge
    itself."""
    sugg = ws.get_project_suggestion(suggestion_id)
    if sugg is None:
        return {"action": "not_found"}
    project_id = merge_issues(sugg["issue_id_a"], sugg["issue_id_b"], reason_label="confirmed suggestion")
    ws.resolve_project_suggestion(suggestion_id, "confirmed")
    workgraph_lessons.record_confirmed_or_rejected(issue_id_a=sugg["issue_id_a"], status="confirmed")
    return {"action": "merged", "project_id": project_id}


def reject_suggestion(suggestion_id: int) -> dict:
    sugg = ws.get_project_suggestion(suggestion_id)
    ws.resolve_project_suggestion(suggestion_id, "rejected")
    if sugg is not None:
        workgraph_lessons.record_confirmed_or_rejected(issue_id_a=sugg["issue_id_a"], status="rejected")
    return {"action": "rejected"}


def group_issue(issue_id: str) -> dict:
    """Runs the strong/weak signal logic for ONE issue. Safe to re-run -
    assign_issue_to_project and create_project_suggestion are both
    idempotent (the former no-ops if already assigned to the target project,
    the latter reuses an existing pending row for the same pair)."""
    issue = ws.get_issue(issue_id)
    if issue is None:
        return {"issue_id": issue_id, "action": "not_found"}
    if issue.get("project_id"):
        return {"issue_id": issue_id, "action": "already_grouped", "project_id": issue["project_id"]}

    match = _strong_signal_match(issue_id, issue)
    if match:
        kind, detail, sibling_id = match
        reason_label = {
            "party": "strong signal: shared external party",
            "company": f"strong signal: shared external company '{detail}'",
            "topic": f"strong signal: matching subject core '{detail}'",
        }[kind]
        project_id = merge_issues(issue_id, sibling_id, reason_label=reason_label)
        return {"issue_id": issue_id, "action": "auto_merged", "project_id": project_id, "signal": kind}

    candidates = _weak_signal_candidates(issue)

    # Total Recall precedent check, before asking curator (or Marc) at all:
    # has this issue's situation (category + external company) already been
    # confirmed/rejected STRONG_PRECEDENT_HITS+ times with high trust? If so,
    # resolve deterministically now rather than adding to curator's queue -
    # see workgraph_lessons.precedent_prefilter. A 'confirmed' verdict merges
    # immediately; a 'rejected' verdict means don't even surface a
    # suggestion. None (no strong precedent yet) falls through to today's
    # behavior unchanged.
    precedent = workgraph_lessons.precedent_prefilter(issue)
    if precedent == "confirmed" and candidates:
        project_id = merge_issues(issue_id, candidates[0]["id"],
                                   reason_label="auto-resolved by precedent (repeated confirmed pattern)")
        return {"issue_id": issue_id, "action": "auto_merged", "project_id": project_id, "signal": "precedent"}

    suggested = 0
    if precedent != "rejected":
        for other in candidates:
            ws.create_project_suggestion(
                issue_id_a=issue_id, issue_id_b=other["id"],
                reason=f"same category ('{issue.get('category')}') within {WEAK_SIGNAL_WINDOW_DAYS}d, no shared external contact found",
            )
            suggested += 1
    if suggested:
        return {"issue_id": issue_id, "action": "suggested", "count": suggested}
    return {"issue_id": issue_id, "action": "no_match"}


def run(issue_ids: list) -> dict:
    results = [group_issue(i) for i in issue_ids]
    return {
        "processed": len(results),
        "auto_merged": sum(1 for r in results if r["action"] == "auto_merged"),
        "suggested": sum(1 for r in results if r["action"] == "suggested"),
        "no_match": sum(1 for r in results if r["action"] == "no_match"),
        "already_grouped": sum(1 for r in results if r["action"] == "already_grouped"),
    }


if __name__ == "__main__":
    import json
    ws.init_workgraph()
    all_ids = ws.list_issue_ids()
    print(json.dumps(run(all_ids), indent=2))
