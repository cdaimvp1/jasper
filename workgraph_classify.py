"""
workgraph_classify.py — deterministic, regex-cue-based classification + thread
clustering. No LLM calls. Modeled directly on the reference
supplier-communication-log.service.ts (Theo platform, read in full this
session): direction/topic/sentiment/anomaly all resolve via cue regexes, with
every inferred field flagged rather than silently asserted, and a confidence
score derived from the explicit/inferred ratio.

Two passes, both pure:
  1. classify_item() — per-raw_item direction/topic/sentiment/anomaly/item_class.
  2. cluster_and_link() — deterministic Issue creation/linking via thread_key.
     Only the STABLE-KEY case is handled here (thread_key already reliable:
     Outlook conversationId, a Teams chat_id, a calendar seriesMasterId). Fuzzy
     matching when no stable thread_key match exists (subject/entity/time
     proximity) is explicitly OUT of scope here — that's curator's LLM-judgment
     layer on the residue, per the plan's deterministic/LLM split.

Curator invokes this on wake (after relay's ingest, or on-demand via the
cockpit's "Re-triage" action) via `python workgraph_classify.py`.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws
import workgraph_parties
import workgraph_projects
import workgraph_signals

# ===========================================================================
# Cue regexes — adapted from supplier-communication-log.service.ts. The
# 8-category topic table is close to verbatim: Marc's actual work (IT
# procurement) is exactly the domain that reference file's taxonomy was built
# for, so it needed no re-invention, only ownership of the fit being noted.
# ===========================================================================

OFF_CHANNEL = re.compile(
    r"\b(called|phoned|texted|whatsapp|personal email|home email|cell|mobile number|"
    r"direct(?:ly)?|side[\s-]?channel|off[\s-]?channel|backchannel)\b", re.I)

INBOUND_CUE = re.compile(
    r"\b(received|replied|responded|they sent|sent us|came back|got back|submitted|"
    r"provided|returned|acknowledged receipt)\b", re.I)
OUTBOUND_CUE = re.compile(
    r"\b(sent|notified|transmitted|issued|requested|we sent|emailed them|delivered|"
    r"forwarded|shared with|reached out|followed up)\b", re.I)
INTERNAL_CUE = re.compile(
    r"\b(meeting notes|internal|debrief|prep|huddle|stand[\s-]?up|sync)\b", re.I)

NEGATIVE_CUE = re.compile(
    r"\b(delay|delayed|late|miss(?:ed)?|issue|problem|dispute|disput|complaint|complain|"
    r"escalat|breach|fail(?:ed|ure)?|concern|unhappy|dissatisf|terminat|penalt|defect|"
    r"outage|incident|overdue|non[\s-]?compliance|reject)\b", re.I)
POSITIVE_CUE = re.compile(
    r"\b(thank|thanks|pleased|congrat|excellent|great|appreciate|agreed|resolved|success|"
    r"on track|ahead of schedule|delivered early|approved|accepted|positive|strong)\b", re.I)

TOPIC_RULES = [
    ("rfp-sourcing", re.compile(r"\b(rfp|rfq|rfi|request for proposal|q&a|addendum|award|bidder|sourcing event)\b", re.I)),
    ("negotiation", re.compile(r"\b(redline|tracked changes|markup|counter[\s-]?proposal|term sheet|bafo|position letter|negotiat)\b", re.I)),
    ("contract", re.compile(r"\b(renewal|renew|terminat|amendment|execution|assignment|force majeure|expirat|msa|sow|contract)\b", re.I)),
    ("onboarding", re.compile(r"\b(onboard|w-?9|w-?8|certificate of insurance|coi|banking|vendor setup|activation|welcome)\b", re.I)),
    ("financial", re.compile(r"\b(invoice|purchase order|\bpo\b|payment|rate change|price (?:adjust|increase)|escalator|true[\s-]?up|credit memo)\b", re.I)),
    ("performance", re.compile(r"\b(qbr|quarterly (?:business )?review|sla|kpi|corrective action|performance|scorecard)\b", re.I)),
    ("compliance", re.compile(r"\b(audit|compliance|certification|regulatory|data breach|adverse event|finding|"
                               r"\bbaa\b|business associate agreement)\b", re.I)),
    ("relationship", re.compile(r"\b(introduc|handoff|thank you|business continuity|general correspondence|check[\s-]?in)\b", re.I)),
    # Added 2026-07-29 - real recurring patterns found in the "other" bucket during
    # backlog profiling, not guessed: "IT Savings 2026", "Savings Projects- 2026".
    # A plain \bsavings\b (not e.g. "savings project", which fails to match
    # "Savings Projects" - "project"+"s" has no word boundary between them)
    # is what actually matches both real subjects.
    ("savings", re.compile(r"\b(savings|cost saving|cost reduction)\b", re.I)),
]

# --- item_class cues (new — not in the reference file, which classifies
# comms for a supplier-relationship log, not a personal inbox needing a
# do-I-need-to-act-on-this triage layer). ---

ACTIONABLE_CUE = re.compile(
    r"\b(action required|please approve|your approval|requires your|please review|"
    r"please confirm|need your|can you|could you|awaiting your|pending your|"
    r"respond by|response required|sign(?:ature)? required)\b", re.I)

CLOSURE_CUE = re.compile(
    r"\b(approved|signed by all parties|completed|executed|resolved|closed out|no further action|all set)\b", re.I)

NOISE_CUE = re.compile(
    r"\b(unsubscribe|newsletter|no action needed|this is an automated message|do not reply)\b", re.I)

# Machine-signal senders — Tier-H candidates once wired into the NBA/issue
# layer (that wiring is a later increment; here they just inform item_class).
MACHINE_SIGNAL_SENDER = re.compile(
    r"(ansmtp\.ariba\.com|@ariba\.com|adobesign|docusign|ironclad|contractpodai\.com|concursolutions\.com)", re.I)

# workgraph_signals.classify_signal's per-signal-type treatment, mapped onto
# this module's item_class vocabulary. 'closure' maps to FYI-EVIDENCE (not
# WAITING-ON-OTHERS) - a fully-approved PR or fully-executed signature is
# genuinely done, not "moving without our confirmation yet" the way the
# generic CLOSURE_CUE path's WAITING-ON-OTHERS guess is.
_SIGNAL_TREATMENT_TO_ITEM_CLASS = {
    "noise": "NOISE", "actionable": "ACTIONABLE-ASK",
    "closure": "FYI-EVIDENCE", "fyi": "FYI-EVIDENCE",
}

# Known signal families that ARE confidently topic-worthy on their own -
# these were the largest real contributors to the 57%-"other" backlog found
# in the 2026-07-29 profiling pass (Ariba PR subjects never matched the
# generic 'financial' topic cues at all - "Requisition"/"PR#" isn't in that
# regex). Signal types not listed here (e.g. intake_new_project_assigned)
# fall through to the generic TOPIC_RULES scan unchanged.
_SIGNAL_TYPE_TOPIC = {
    "ariba_pr_approval_needed": "financial", "ariba_pr_fully_approved": "financial",
    "ariba_pr_watcher": "financial", "ariba_pr_approver_added": "financial",
    "ariba_pr_escalated": "financial", "ariba_pr_partial_approval": "financial",
    "ariba_role_changed_generic": "financial",
    "signature_requested": "contract", "signature_signed_by_me": "contract",
    "signature_fully_executed": "contract", "signature_cc_notice": "contract",
    "signature_completed_docusign": "contract", "signature_requested_docusign": "contract",
    "contractpodai_obligations_update": "contract", "contractpodai_contract_request_submitted": "contract",
    "concur_expense_reminder": "expense",
}

_RE_PREFIX = re.compile(r"^\s*(re|fwd?|fw)\s*:\s*", re.I)


def strip_subject_prefix(subject: str) -> str:
    """Strip a leading Re:/Fwd: (repeatedly, for 'Re: Fwd: Re:' chains)."""
    s = subject or ""
    prev = None
    while prev != s:
        prev = s
        s = _RE_PREFIX.sub("", s)
    return s.strip()


def classify_item(*, subject: str, body_preview: str, from_actor: str) -> dict:
    """Pure (well - one settings-table read inside workgraph_signals, fail-
    open to its hardcoded default on any error). Returns
    direction/topic/sentiment/anomaly/item_class/signal_type/pr_number, each
    inferred field flagged, plus an overall confidence tier (H/M/L).

    A recognized automated signal (Ariba PR approval, Adobe Sign/DocuSign,
    ContractPodAI - see workgraph_signals.py) is checked FIRST and, when
    matched, its treatment drives item_class (and, for the Ariba/signature
    families, topic too - see _SIGNAL_TYPE_TOPIC) with full confidence -
    these are known, real-data-confirmed templates, not a guess. Everything
    else still falls through to the generic cue-based classification below,
    unchanged."""
    text = f"{subject or ''} {body_preview or ''}"

    signal = workgraph_signals.classify_signal(subject=subject or "", from_actor=from_actor or "")

    direction_inferred = True
    if INBOUND_CUE.search(text):
        direction = "inbound"
    elif OUTBOUND_CUE.search(text):
        direction = "outbound"
    elif INTERNAL_CUE.search(text):
        direction = "internal"
    else:
        direction = "inbound"  # default: most personal-inbox mail is inbound by construction

    if signal and signal["signal_type"] in _SIGNAL_TYPE_TOPIC:
        topic = _SIGNAL_TYPE_TOPIC[signal["signal_type"]]
        topic_inferred = False  # a confirmed signal template, not a keyword guess
    else:
        topic_inferred = True
        topic = "other"
        for name, rule in TOPIC_RULES:
            if rule.search(text):
                topic = name
                break

    sentiment_inferred = True
    neg = bool(NEGATIVE_CUE.search(text))
    pos = bool(POSITIVE_CUE.search(text))
    if neg and not pos:
        sentiment = "negative"
    elif pos and not neg:
        sentiment = "positive"
    else:
        sentiment = "neutral"

    anomaly_flag = bool(OFF_CHANNEL.search(text))

    is_machine_signal = bool(from_actor and MACHINE_SIGNAL_SENDER.search(from_actor))
    is_noise = bool(NOISE_CUE.search(text))
    is_actionable = bool(ACTIONABLE_CUE.search(text))
    is_closure = bool(CLOSURE_CUE.search(text))

    # item_class precedence: a recognized signal template > noise cue >
    # machine-signal closure > actionable > closure > default. A signal match
    # is a confirmed, real-data-checked template (see workgraph_signals.py),
    # so it wins over the generic keyword cues below rather than being just
    # one more heuristic in the mix.
    if signal:
        item_class = _SIGNAL_TREATMENT_TO_ITEM_CLASS[signal["treatment"]]
        class_confident = True
    elif is_noise:
        item_class = "NOISE"
        class_confident = True
    elif is_machine_signal and is_actionable:
        item_class = "ACTIONABLE-ASK"
        class_confident = True
    elif is_machine_signal and is_closure:
        item_class = "FYI-EVIDENCE"  # e.g. an Ariba "approved" notice - informational, no action needed
        class_confident = True
    elif is_actionable:
        item_class = "ACTIONABLE-ASK"
        class_confident = True
    elif is_closure:
        item_class = "WAITING-ON-OTHERS"  # a closure phrase on a thread implies it was moving without us yet confirming
        class_confident = False
    else:
        # Genuinely ambiguous - default to the lowest-commitment classification
        # (FYI-EVIDENCE) rather than guessing ACTIONABLE, and flag low confidence
        # so curator's LLM pass picks it up rather than trusting a weak default.
        item_class = "FYI-EVIDENCE"
        class_confident = False

    # Confidence: same shape as the reference deriveConfidence - ratio of
    # explicit-vs-inferred fields, plus the item_class confidence flag.
    inferred_count = sum([direction_inferred, topic_inferred, sentiment_inferred, not class_confident])
    if inferred_count <= 1:
        confidence = "H"
    elif inferred_count <= 2:
        confidence = "M"
    else:
        confidence = "L"

    return {
        "item_class": item_class,
        "direction": direction, "direction_inferred": direction_inferred,
        "topic": topic, "topic_inferred": topic_inferred,
        "sentiment": sentiment, "sentiment_inferred": sentiment_inferred,
        "anomaly_flag": anomaly_flag,
        "confidence": confidence,
        "signal_type": signal["signal_type"] if signal else None,
        "pr_number": signal["pr_number"] if signal else None,
    }


def run_classification(limit: int = 500) -> dict:
    items = ws.get_unclassified_raw_items(limit=limit)
    counts = {"NOISE": 0, "ACTIONABLE-ASK": 0, "WAITING-ON-OTHERS": 0, "FYI-EVIDENCE": 0}
    for item in items:
        result = classify_item(
            subject=item.get("subject") or "",
            body_preview=item.get("body_preview") or "",
            from_actor=item.get("from_actor") or "",
        )
        ws.classify_raw_item(
            item["id"],
            item_class=result["item_class"],
            direction=result["direction"], direction_inferred=result["direction_inferred"],
            topic=result["topic"], topic_inferred=result["topic_inferred"],
            sentiment=result["sentiment"], sentiment_inferred=result["sentiment_inferred"],
            anomaly_flag=result["anomaly_flag"],
            signal_type=result["signal_type"], pr_number=result["pr_number"],
        )
        counts[result["item_class"]] = counts.get(result["item_class"], 0) + 1
    return {"classified": len(items), "by_class": counts}


# ===========================================================================
# Deterministic clustering (stable-key only - thread_key already reliable).
# ===========================================================================

_SYSTEM_SENDER = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|automated|system|admin)@", re.I)


def compute_deterministic_title(issue_id: str) -> Optional[str]:
    """'Requestor - Supplier - short topic', built entirely from data already
    on hand (no LLM call) - a real name/company beats a raw email subject
    line for telling two threads apart at a glance, per Marc's ask. Requires
    at least 2 of the 3 parts to be real (not just a lone topic) - a title
    that's no better than the raw one isn't worth overriding it."""
    issue = ws.get_issue(issue_id)
    if not issue:
        return None
    parties = ws.list_parties_for_issue(issue_id)
    internal = [p for p in parties if p.get("affiliation") == "internal" and p.get("display_name")]
    # A no-reply/system sender's domain-derived "company" (e.g. 'Ansmtp' from
    # no-reply@ansmtp.ariba.com) isn't a real supplier name - skip those.
    external = [p for p in parties if p.get("affiliation") == "external"
                and not _SYSTEM_SENDER.match(p.get("primary_email") or "")]
    requestor = internal[0]["display_name"] if internal else None
    supplier = None
    if external:
        supplier = external[0].get("company") or external[0].get("display_name")
    topic = strip_subject_prefix(issue.get("title") or "")
    if len(topic) > 60:
        topic = topic[:57].rstrip() + "..."
    parts = [p for p in (requestor, supplier, topic) if p]
    if len([p for p in (requestor, supplier) if p]) == 0:
        return None  # no real name/company on hand - the raw title is all there is, don't fake improve it
    return " - ".join(parts)


def backfill_derived_titles() -> dict:
    """One-time (or on-demand) repair pass: compute a derived_title for every
    existing issue that doesn't have one yet - fixes the 74 real issues
    curator already synthesized before this generator existed."""
    issue_ids = ws.list_issue_ids()
    updated = 0
    for issue_id in issue_ids:
        title = compute_deterministic_title(issue_id)
        if title:
            ws.set_derived_title("issue", issue_id, title)
            updated += 1
    return {"checked": len(issue_ids), "updated": updated}


def recompute_issue_state(issue_id: str) -> Optional[str]:
    """Derive an issue's state from its WHOLE evidence thread, not whichever
    item happened to arrive first (the bug this fixes: an issue opened by an
    FYI note froze in 'waiting' forever, even once a real actionable ask
    landed on the same thread - or vice versa). Priority rule, deterministic:
    any ACTIONABLE-ASK anywhere in the thread wins (something needs your move,
    regardless of what arrived after it) > any WAITING-ON-OTHERS > otherwise
    the whole thread has only ever been FYI-EVIDENCE/NOISE, so there was never
    anything to act on or wait for.

    Does not touch 'done'/'noise-archived' unless a genuinely new
    ACTIONABLE-ASK item justifies reopening - a human's manual close/archive
    is otherwise left alone, not silently reverted by routine re-classification."""
    items = ws.get_raw_items_for_issue(issue_id)
    classes = {i["item_class"] for i in items if i.get("classified")}
    if "ACTIONABLE-ASK" in classes:
        target = "active"
    elif "WAITING-ON-OTHERS" in classes:
        target = "waiting"
    else:
        target = "done"  # thread has only ever been FYI-EVIDENCE/NOISE - nothing to track

    issue = ws.get_issue(issue_id)
    if issue is None:
        return None
    current = issue["state"]
    if current in ("done", "noise-archived") and target != "active":
        return current  # respect a manual close/archive unless truly reopened by a new ask
    if current != target:
        ws.update_issue(issue_id, state=target)
    return target


def cluster_and_link(limit: int = 500) -> dict:
    """For every classified-but-not-yet-linked item (issue_id IS NULL),
    resolve via thread_map (an existing Issue's thread_key) or create a new
    Issue. NOISE items are never promoted to an Issue - they're classified and
    left ungrouped (auditable, per the reference file's precision-favoring
    default), not silently dropped. FYI-EVIDENCE items get the same
    not-promoted treatment when there's no existing thread to attach to -
    a standalone informational note (a meeting recap, a group-chat aside)
    isn't something Marc needs to track as an open Issue on its own."""
    with_pending = ws.get_items_pending_link(limit)
    created = 0
    linked = 0
    skipped_noise = 0
    skipped_fyi_standalone = 0
    touched_issues = set()

    for item in with_pending:
        if item["item_class"] == "NOISE":
            skipped_noise += 1
            continue

        thread_key = item["thread_key"]
        issue_id = ws.thread_map_lookup(thread_key)
        is_new_issue = False
        if issue_id is None:
            if item["item_class"] == "FYI-EVIDENCE":
                skipped_fyi_standalone += 1
                continue
            issue_id = ws.next_issue_id()
            title = strip_subject_prefix(item.get("subject") or "(no subject)")
            state = "active" if item["item_class"] == "ACTIONABLE-ASK" else "waiting"
            ws.create_issue(
                id=issue_id, title=title, category=item.get("topic"),
                state=state, priority="med", confidence_tier=item.get("confidence") or "M",
            )
            ws.thread_map_set(thread_key, issue_id)
            created += 1
            is_new_issue = True

        ws.link_raw_item_to_issue(item["id"], issue_id)
        ws.add_evidence(
            issue_id=issue_id,
            type=_evidence_type(item["source"]),
            summary=item.get("subject") or item.get("body_preview") or "(no summary)",
            raw_item_id=item["id"],
        )
        linked += 1
        touched_issues.add(issue_id)

        # A new issue opened by a genuine ask gets one starter task, with an
        # owner resolved from any matching ownership_rules - left unknown
        # (None) rather than defaulted to 'marc' when nothing matches, since
        # guessing who owns something is exactly what Marc asked NOT to do.
        if is_new_issue and item["item_class"] == "ACTIONABLE-ASK":
            owner = ws.find_owner_for(category=item.get("topic"), topic=item.get("topic"))
            ws.create_task(issue_id=issue_id, label=strip_subject_prefix(item.get("subject") or "(no subject)"), owner=owner)

    for issue_id in touched_issues:
        recompute_issue_state(issue_id)

    party_result = workgraph_parties.run(list(touched_issues))
    project_result = workgraph_projects.run(list(touched_issues))

    # Title generation runs AFTER parties/projects resolve for this batch -
    # it reads party affiliation/company, which a just-created issue doesn't
    # have until workgraph_parties.run() above has linked them.
    for issue_id in touched_issues:
        title = compute_deterministic_title(issue_id)
        if title:
            ws.set_derived_title("issue", issue_id, title)

    return {"issues_created": created, "items_linked": linked, "noise_skipped": skipped_noise,
            "fyi_standalone_skipped": skipped_fyi_standalone,
            "parties": party_result, "projects": project_result}


def backfill_reclassify() -> dict:
    """One-time (or on-demand) repair pass: re-run the FULL classify_item()
    (signal templates AND the generic TOPIC_RULES scan - not just signals)
    against every ALREADY-classified raw_item, and update item_class/topic/
    signal_type/pr_number wherever the result now differs from what's stored.

    This is safe because classify_item is pure and deterministic: given the
    same subject/body/sender, it returns the SAME result every time unless
    the ruleset itself (TOPIC_RULES or workgraph_signals._RULES) changed
    since the item was last classified. So any difference found here is a
    genuine ruleset improvement being applied retroactively, never a random
    re-guess - e.g. the 2026-07-29 additions (Ariba/Adobe Sign/DocuSign/
    ContractPodAI signals, the BAA keyword, the new 'savings'/'expense'
    categories) all only take effect for NEW mail otherwise; this is what
    closes that gap for the existing backlog too, without Marc reclassifying
    anything by hand. First run (signals only) moved 54 issues out of
    'other'; the categories_updated/updated counts below are THIS run's own.

    The issue's own `category` (set once at creation - see cluster_and_link -
    never auto-recomputed since) is only ever corrected when it's still
    sitting at 'other', the classifier's own lowest-confidence default -
    never a category a human or something else already assigned on purpose."""
    rows = ws.get_all_classified_raw_items()
    updated = 0
    touched_issues = set()
    issue_new_topic: dict[str, str] = {}

    for item in rows:
        result = classify_item(
            subject=item.get("subject") or "", body_preview=item.get("body_preview") or "",
            from_actor=item.get("from_actor") or "",
        )
        # Fixed 2026-07-29: this used to only COMPARE topic/item_class/
        # signal_type, then WRITE direction/sentiment/anomaly_flag straight
        # from the old stored row regardless - contradicting this function's
        # own docstring claim to refresh the full classify_item() result. A
        # future NEGATIVE_CUE/POSITIVE_CUE/OFF_CHANNEL/direction-cue change
        # would have silently never reached the existing backlog. Now every
        # field classify_item can produce is both compared and written.
        if (result["topic"] == item.get("topic") and result["item_class"] == item.get("item_class")
                and result["signal_type"] == item.get("signal_type")
                and result["direction"] == item.get("direction")
                and result["sentiment"] == item.get("sentiment")
                and result["anomaly_flag"] == bool(item.get("anomaly_flag"))):
            continue  # already correct - nothing to update

        if item.get("issue_id") and result["topic"] and item["issue_id"] not in issue_new_topic:
            issue_new_topic[item["issue_id"]] = result["topic"]

        ws.classify_raw_item(
            item["id"], item_class=result["item_class"],
            direction=result["direction"], direction_inferred=result["direction_inferred"],
            topic=result["topic"], topic_inferred=result["topic_inferred"],
            sentiment=result["sentiment"], sentiment_inferred=result["sentiment_inferred"],
            anomaly_flag=result["anomaly_flag"],
            signal_type=result["signal_type"], pr_number=result["pr_number"],
        )
        updated += 1
        if item.get("issue_id"):
            touched_issues.add(item["issue_id"])

    categories_updated = 0
    for issue_id, new_category in issue_new_topic.items():
        issue = ws.get_issue(issue_id)
        if issue and issue.get("category") == "other" and new_category != "other":
            ws.update_issue(issue_id, category=new_category)
            touched_issues.add(issue_id)
            categories_updated += 1

    for issue_id in touched_issues:
        recompute_issue_state(issue_id)
        title = compute_deterministic_title(issue_id)
        if title:
            ws.set_derived_title("issue", issue_id, title)

    return {"checked": len(rows), "updated": updated, "issues_touched": len(touched_issues),
            "issue_categories_updated": categories_updated}


def backfill_recompute_all_states() -> dict:
    """One-time (or on-demand) repair pass: re-derive state for EVERY existing
    issue from its full evidence thread, fixing issues created before this
    logic existed (the ~109 issues frozen in 'waiting' from a first-contact
    FYI note that classify_item never revisited)."""
    issue_ids = ws.list_issue_ids()
    changed = {}
    for issue_id in issue_ids:
        before = ws.get_issue(issue_id)["state"]
        after = recompute_issue_state(issue_id)
        if after is not None and after != before:
            changed[issue_id] = f"{before} -> {after}"
    return {"checked": len(issue_ids), "changed": changed}


def _evidence_type(source: str) -> str:
    return {"outlook_mail": "email", "teams_chat": "teams", "calendar": "calendar",
            "sharepoint": "sharepoint"}.get(source, "email")


def run() -> dict:
    ws.init_workgraph()
    classify_result = run_classification()
    cluster_result = cluster_and_link()
    return {"classify": classify_result, "cluster": cluster_result}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
