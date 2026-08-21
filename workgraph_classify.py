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
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import workgraph_store as ws
import workgraph_identity
import workgraph_parties
import workgraph_projects
import workgraph_signals
import workgraph_sessionize
import workgraph_discovery
import text_extract
import link_extraction

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

# Fixed 2026-07-29: several stems below were wrapped as bare fragments inside
# \b(...)\b - the trailing \b requires a word->non-word transition RIGHT
# AFTER the stem, which never happens once a suffix like -ed/-ing/-ion
# continues the word. Confirmed dead: "escalat"/"terminat"/"dissatisf"/
# "disput" never matched "escalated"/"terminated"/"dissatisfied"/"disputed";
# "negotiat"/"onboard"/"introduc" never matched their own most common real
# forms ("negotiation", "onboarding", "introduction"); "congrat"/"appreciate"
# never matched "congratulations"/"appreciated". Adding \w* after each stem
# (keeping the leading \b, letting the match run to the word's real end
# before the trailing \b applies) fixes all of these without narrowing what
# already matched.
NEGATIVE_CUE = re.compile(
    r"\b(delay\w*|late|miss(?:ed)?|issue|problem|disput\w*|complaint|complain\w*|"
    r"escalat\w*|breach|fail(?:ed|ure)?|concern|unhappy|dissatisf\w*|terminat\w*|penalt\w*|defect|"
    r"outage|incident|overdue|non[\s-]?compliance|reject\w*)\b", re.I)
POSITIVE_CUE = re.compile(
    r"\b(thank\w*|pleased|congrat\w*|excellent|great|appreciat\w*|agreed|resolved|success\w*|"
    r"on track|ahead of schedule|delivered early|approved|accepted|positive|strong)\b", re.I)

TOPIC_RULES = [
    ("rfp-sourcing", re.compile(r"\b(rfp|rfq|rfi|request for proposal|q&a|addendum|award|bidder|sourcing event)\b", re.I)),
    ("negotiation", re.compile(r"\b(redline|tracked changes|markup|counter[\s-]?proposal|term sheet|bafo|position letter|negotiat\w*)\b", re.I)),
    ("contract", re.compile(r"\b(renewal|renew\w*|terminat\w*|amendment|execution|assignment|force majeure|expirat\w*|msa|sow|contract)\b", re.I)),
    ("onboarding", re.compile(r"\b(onboard\w*|w-?9|w-?8|certificate of insurance|coi|banking|vendor setup|activation|welcome)\b", re.I)),
    ("financial", re.compile(r"\b(invoice|purchase order|\bpo\b|payment|rate change|price (?:adjust\w*|increas\w*)|escalator|true[\s-]?up|credit memo)\b", re.I)),
    ("performance", re.compile(r"\b(qbr|quarterly (?:business )?review|sla|kpi|corrective action|performance|scorecard)\b", re.I)),
    ("compliance", re.compile(r"\b(audit|compliance|certification|regulatory|data breach|adverse event|finding|"
                               r"\bbaa\b|business associate agreement)\b", re.I)),
    ("relationship", re.compile(r"\b(introduc\w*|handoff|thank you|business continuity|general correspondence|check[\s-]?in)\b", re.I)),
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
# Real domain-boundary matches (workgraph_signals.domain_matches), NOT a
# regex-substring check - confirmed exploitable 2026-07-29: the old
# alternation-regex form matched "notariba.com" and
# "ariba.com.evil-tracker.net" identically to the real domain, so a
# lookalike/spoofed sender got the same automated-system trust as the real
# one. "ironclad" has no confirmed real domain from this session's research
# (unlike the other five) - kept as a bare substring pending that, since
# demoting an untested entry isn't this fix's job.
MACHINE_SIGNAL_DOMAINS = ["ansmtp.ariba.com", "ariba.com", "adobesign.com",
                           "docusign.net", "contractpodai.com", "concursolutions.com"]
_IRONCLAD_SENDER = re.compile(r"ironclad", re.I)


def is_machine_signal_sender(from_actor: str) -> bool:
    if not from_actor:
        return False
    if _IRONCLAD_SENDER.search(from_actor):
        return True
    return any(workgraph_signals.domain_matches(from_actor, d) for d in MACHINE_SIGNAL_DOMAINS)

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
_EXTERNAL_PREFIX = re.compile(r"^\s*\[external\]\s*", re.I)
_AUTO_REPLY_PREFIX = re.compile(r"^\s*automatic reply:\s*", re.I)
# Real, observed pattern (2026-08-01 real-incident follow-up): Outlook's own
# calendar-reminder notification wraps the actual meeting title in this
# fixed template - "[EXTERNAL] Your meeting "Lilly and Workday - Early
# Renewal Weekly Meeting" is starting soon...". Extracts the title out of
# the wrapper rather than trying to strip the wrapper as another prefix,
# since it's not a prefix - the real subject is INSIDE quotes, not after them.
_MEETING_STARTING_SOON = re.compile(r'^\s*(?:\[external\]\s*)?your meeting\s+"(.+?)"\s+is starting soon', re.I)
# Task #52 (2026-08-04): Ariba's own requisition-approval notification wraps
# the actual descriptive content (PR number, project name, dollar figure -
# the genuinely useful part) inside a fixed boilerplate carrier. Confirmed
# against 15+ real distinct subjects live: the wrapper text and the double
# space before " - " are both exactly consistent, never varying - a safe,
# purely mechanical strip (removing a known fixed wrapper), not a judgment
# call about what the email means, so it doesn't cross the "curator's job,
# not a keyword guess" line this codebase holds elsewhere (Ariba expiration
# dates, deadline_type/resolved_date).
_ARIBA_REQUISITION_BOILERPLATE_RE = re.compile(
    r"^\s*action required:\s*approve the requisition that\s+(?P<submitter>.+?)\s+submitted\s*[-–—]?\s*", re.I,
)


def _titlecase_name(raw: str) -> str:
    """Ariba's ALL-CAPS submitter name ('CORRINA MCCORKLE') needs casing to
    read as a name rather than shouted text - plain .title() would flatten
    'Mc' surnames (confirmed real in this corpus, e.g. 'McCorkle') to
    'Mccorkle', so those get a second capitalization pass. Deliberately
    narrow (only the unambiguous 'Mc' prefix) - a broader 'Ma'-style rule
    would wrongly re-split real names like 'Mary' or 'Mason'."""
    words = []
    for w in raw.strip().split():
        tw = w.capitalize()
        if len(tw) > 3 and tw[:2] == "Mc":
            tw = "Mc" + tw[2:].capitalize()
        words.append(tw)
    return " ".join(words)


def strip_subject_prefix(subject: str) -> str:
    """Strip a leading Re:/Fwd: (repeatedly, for 'Re: Fwd: Re:' chains)."""
    s = subject or ""
    prev = None
    while prev != s:
        prev = s
        s = _RE_PREFIX.sub("", s)
    return s.strip()


def normalize_subject_for_matching(subject: str) -> str:
    """A broader normalization than strip_subject_prefix, built for one
    specific purpose (2026-08-01 real-incident follow-up): deciding whether
    two DIFFERENT raw_items - no shared thread_key, no shared PR/PO
    reference - are actually the same real-world conversation Outlook just
    fragmented into separate ConversationIDs (a meeting series' own
    reminder/auto-reply/external-tag noise, confirmed on a real thread:
    "[EXTERNAL] Re: Lilly and Workday - Early Renewal Weekly Meeting",
    "Automatic reply: Lilly and Workday - Early Renewal Weekly Meeting", and
    "[EXTERNAL] Your meeting "Lilly and Workday - Early Renewal Weekly
    Meeting" is starting soon..." all describe the exact same meeting).
    Lowercased, so this is for equality comparison, not display. Not used
    for title storage or classification - only for the subject_match
    candidate check in cluster_and_link()."""
    s = subject or ""
    m = _MEETING_STARTING_SOON.match(s)
    if m:
        s = m.group(1)
    else:
        s = _EXTERNAL_PREFIX.sub("", s)
        s = _AUTO_REPLY_PREFIX.sub("", s)
    return strip_subject_prefix(s).strip().lower()


def _parse_participants(item: dict) -> Optional[list]:
    """raw_items.participants is stored as a JSON-encoded string
    (insert_raw_item's participants_json) - classify_item wants the real
    list. Fails open to None (never a guess) on malformed/missing JSON."""
    raw = item.get("participants")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def classify_item(*, subject: str, body_preview: str, from_actor: str,
                   source: Optional[str] = None, organizer: Optional[str] = None,
                   participants: Optional[list] = None,
                   confirmed_direction: Optional[str] = None) -> dict:
    """Pure (well - one settings-table read inside workgraph_signals, fail-
    open to its hardcoded default on any error). Returns
    direction/topic/sentiment/anomaly/item_class/signal_type/pr_number/
    pr_number_base, each inferred field flagged, plus an overall confidence
    tier (H/M/L).

    source/organizer/participants (2026-07-31, meeting-grouping design
    pass): optional, calendar-specific - when source=="calendar" and the
    event is a personal/solo block or an out-of-office announcement (see
    workgraph_signals.is_personal_calendar_block/is_ooo_subject), item_class
    is confidently NOISE (class_confident=True) rather than falling through
    to the generic cue-based path below and becoming a trackable Issue -
    Marc's own call: neither shape is a project. The overall confidence
    tier can still land at M/L if direction/topic/sentiment weren't
    explicitly cued (irrelevant for a NOISE item that's never promoted to
    an Issue either way). Every existing caller passes none of these three
    and is completely unaffected.

    A recognized automated signal (Ariba PR approval, Adobe Sign/DocuSign,
    ContractPodAI - see workgraph_signals.py) is checked FIRST and, when
    matched, its treatment drives item_class (and, for the Ariba/signature
    families, topic too - see _SIGNAL_TYPE_TOPIC) with full confidence -
    these are known, real-data-confirmed templates, not a guess. Everything
    else still falls through to the generic cue-based classification below,
    unchanged."""
    text = f"{subject or ''} {body_preview or ''}"

    signal = workgraph_signals.classify_signal(subject=subject or "", from_actor=from_actor or "")

    # Fixed 2026-07-29: direction_inferred/sentiment_inferred (and topic's
    # generic-path branch) used to be hardcoded True unconditionally, never
    # flipped to False even when a real cue regex actually matched - meaning
    # inferred_count below could never drop low enough for confidence to
    # reach "H" for ANY input, including a fully signal-confirmed Ariba
    # approval. Each now genuinely reflects "was this explicitly matched by
    # a real cue, or just defaulted/guessed" - the ratio deriveConfidence
    # below is supposed to measure.
    # Task #270 Phase B (2026-08-07): a source that KNOWS an item's direction
    # structurally (e.g. the sent-items ingester - it came out of Outlook's
    # own Sent Items folder, there is no ambiguity to infer) wins over the
    # cue-regex guess below, same precedence pattern this function already
    # uses for the calendar-personal-block/signal-confirmed-topic overrides
    # above. direction_inferred=False here is honest, not borrowed from the
    # cue path - a real known fact, not a keyword match.
    direction_inferred = False
    if confirmed_direction is not None:
        direction = confirmed_direction
    elif INBOUND_CUE.search(text):
        direction = "inbound"
    elif OUTBOUND_CUE.search(text):
        direction = "outbound"
    elif INTERNAL_CUE.search(text):
        direction = "internal"
    else:
        direction = "inbound"  # default: most personal-inbox mail is inbound by construction
        direction_inferred = True

    if signal and signal["signal_type"] in _SIGNAL_TYPE_TOPIC:
        topic = _SIGNAL_TYPE_TOPIC[signal["signal_type"]]
        topic_inferred = False  # a confirmed signal template, not a keyword guess
    else:
        topic = "other"
        topic_inferred = True
        for name, rule in TOPIC_RULES:
            if rule.search(text):
                topic = name
                topic_inferred = False
                break

    neg = bool(NEGATIVE_CUE.search(text))
    pos = bool(POSITIVE_CUE.search(text))
    if neg and not pos:
        sentiment = "negative"
        sentiment_inferred = False
    elif pos and not neg:
        sentiment = "positive"
        sentiment_inferred = False
    else:
        sentiment = "neutral"  # no cue, or both fired - genuinely ambiguous, stays inferred
        sentiment_inferred = True

    anomaly_flag = bool(OFF_CHANNEL.search(text))

    is_machine_signal = is_machine_signal_sender(from_actor)
    is_noise = bool(NOISE_CUE.search(text))
    is_actionable = bool(ACTIONABLE_CUE.search(text))
    is_closure = bool(CLOSURE_CUE.search(text))
    is_calendar_personal_or_ooo = source == "calendar" and (
        workgraph_signals.is_ooo_subject(subject)
        or workgraph_signals.is_personal_calendar_block(organizer=organizer, participants=participants)
    )

    # item_class precedence: a personal/OOO calendar block > a recognized
    # signal template > noise cue > machine-signal closure > actionable >
    # closure > default. The calendar check wins over everything - a solo
    # HOLD block or an OOO broadcast is never a real ask no matter what its
    # text happens to contain. A signal match is a confirmed, real-data-
    # checked template (see workgraph_signals.py), so it wins over the
    # generic keyword cues below rather than being just one more heuristic
    # in the mix.
    if is_calendar_personal_or_ooo:
        item_class = "NOISE"
        class_confident = True
    elif signal:
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

    # Widened 2026-07-30 (enhancement #1): a recognized automated signal's
    # own pr_number (workgraph_signals.classify_signal, subject-only) wins
    # when present - a known, real-data-confirmed template, not a guess.
    # Otherwise fall back to scanning the FULL text (subject + body, not
    # just subject) with the same shared regex - a real requisition/order
    # number can appear in a normal human email that never matches a known
    # automated-sender pattern at all, and used to be invisible here.
    pr_number = signal["pr_number"] if signal else None
    if not pr_number:
        m = workgraph_signals.REFERENCE_ID_RE.search(text)
        pr_number = m.group(0).upper() if m else None
    # 2026-07-31 (meeting-grouping/related-project identity pass): version-
    # stripped identity for matching only - see workgraph_signals.
    # reference_base's own docstring for why the full pr_number above stays
    # untouched (still the real display/audit value).
    pr_number_base = signal.get("pr_number_base") if signal else None
    if not pr_number_base:
        pr_number_base = workgraph_signals.reference_base(pr_number)

    # Task #265 (2026-08-07): a ContractPodAI request id is exactly the
    # same kind of stable, deal-scoped reference PR/PO numbers already are
    # (Marc's own criteria - a real permalink, consistent across every
    # notification about the same request) - reusing the SAME pr_number/
    # pr_number_base matching machinery this codebase already has, just
    # namespaced (CPAI<id>) so it can never collide with a real Ariba
    # PR/PO. Only fires when nothing already matched above - a genuine
    # PR/PO mention (rare but possible even in a ContractPodAI thread,
    # e.g. quoted from an earlier Ariba email) still wins.
    if not pr_number and signal and (signal.get("signal_type") or "").startswith("contractpodai_"):
        cpai_fields = workgraph_signals.extract_contractpodai_request_fields(subject, body_preview)
        if cpai_fields:
            pr_number = f"CPAI{cpai_fields['request_id']}"
            pr_number_base = pr_number

    # Task #36: same full-text scan as pr_number above - a Jasper reference
    # tag most often shows up quoted back inside a reply's body (the
    # original draft's signature/subject line, echoed by the reply chain),
    # not necessarily the subject alone.
    jasper_ref_issue_id = workgraph_signals.jasper_ref_issue_id(text)

    return {
        "item_class": item_class,
        "direction": direction, "direction_inferred": direction_inferred,
        "topic": topic, "topic_inferred": topic_inferred,
        "sentiment": sentiment, "sentiment_inferred": sentiment_inferred,
        "anomaly_flag": anomaly_flag,
        "confidence": confidence,
        "signal_type": signal["signal_type"] if signal else None,
        "pr_number": pr_number,
        "pr_number_base": pr_number_base,
        "jasper_ref_issue_id": jasper_ref_issue_id,
    }


def _confirmed_direction_from_meta(item: dict) -> Optional[str]:
    """Task #270 Phase B: the sent-items ingester writes meta_json=
    '{"confirmed_direction":"outbound"}' on every row it creates - a
    structural fact of WHERE the item came from (Outlook's own Sent Items
    folder), not a keyword guess. Reused the existing meta_json field
    (already used by calendar ingestion for is_recurring/attendees_detailed,
    see workgraph_store.py) rather than a new column - no migration needed."""
    meta_json = item.get("meta_json")
    if not meta_json:
        return None
    try:
        meta = json.loads(meta_json)
    except (TypeError, ValueError):
        return None
    return meta.get("confirmed_direction")


def run_classification(limit: int = 500) -> dict:
    items = ws.get_unclassified_raw_items(limit=limit)
    counts = {"NOISE": 0, "ACTIONABLE-ASK": 0, "WAITING-ON-OTHERS": 0, "FYI-EVIDENCE": 0}
    for item in items:
        result = classify_item(
            subject=item.get("subject") or "",
            body_preview=text_extract.resolve_item_text(item),
            from_actor=item.get("from_actor") or "",
            source=item.get("source"), organizer=item.get("from_actor"),
            participants=_parse_participants(item),
            confirmed_direction=_confirmed_direction_from_meta(item),
        )
        ws.classify_raw_item(
            item["id"],
            item_class=result["item_class"],
            direction=result["direction"], direction_inferred=result["direction_inferred"],
            topic=result["topic"], topic_inferred=result["topic_inferred"],
            sentiment=result["sentiment"], sentiment_inferred=result["sentiment_inferred"],
            anomaly_flag=result["anomaly_flag"],
            signal_type=result["signal_type"], pr_number=result["pr_number"],
            pr_number_base=result["pr_number_base"],
            jasper_ref_issue_id=result["jasper_ref_issue_id"],
            confidence=result["confidence"],
        )
        counts[result["item_class"]] = counts.get(result["item_class"], 0) + 1
        # Task #303: queue any SharePoint/OneDrive document link this
        # message's body carries, regardless of signal_type - relay picks
        # these up on its own next wake and fetches the real content.
        # Never fails the classify batch over one bad link scan.
        try:
            for link in link_extraction.extract_cloud_doc_links_for_raw_item(item):
                ws.create_pending_link_fetch(item["id"], link["url"], link.get("label"))
        except Exception:
            pass
    return {"classified": len(items), "by_class": counts}


# ===========================================================================
# Deterministic clustering (stable-key only - thread_key already reliable).
# ===========================================================================

def compute_deterministic_title(issue_id: str) -> Optional[str]:
    """'Requestor - Supplier - short topic', built entirely from data already
    on hand (no LLM call) - a real name/company beats a raw email subject
    line for telling two threads apart at a glance, per Marc's ask. Returns
    None (never fake-improves the raw title) when neither a real requestor
    nor a real supplier is on hand.

    Two real quality bugs found and fixed (task #52, 2026-08-04) via a live
    backfill run against production, both confirmed against real subjects
    before fixing:
    - the Ariba requisition-approval notification's fixed boilerplate
      wrapper ("Action required: Approve the Requisition that NAME
      submitted - ...") was passing straight through into `topic`
      untouched, so the "improved" title still carried the exact same
      noise task #52 exists to remove - stripped via a narrow, exact-
      wrapper regex (a mechanical strip of known fixed text, not a
      judgment call about what the email means).
    - prepending `requestor` when their own name already appears verbatim
      in the topic (e.g. "Alex Sohn Finance Intern Presentation") produced
      a literal duplicate ("Alex Sohn - Alex Sohn Finance Intern
      Presentation") - now skipped when redundant, same treatment for
      `supplier`.

    Third bug found live (2026-08-04, checking task #57's 'Your next move'
    strip - the two highest-priority items after the first two fixes were
    STILL raw Ariba boilerplate): an Ariba PR-approval notification has no
    linked internal party for the submitter (they're not a sender/recipient
    on the email, just named in its body/subject), so `internal` comes back
    empty and the function correctly declined - but the submitter's real
    name is sitting right there in the subject. Submitting an Ariba
    requisition for internal approval is inherently an internal-Lilly
    action, so that name is used as a fallback requestor when no linked
    party already covers it - same mechanical-extraction reasoning as the
    boilerplate strip itself, not a content judgment call."""
    issue = ws.get_issue(issue_id)
    if not issue:
        return None
    parties = ws.list_parties_for_issue(issue_id)
    return _compute_deterministic_title_from_parties(parties, issue.get("title") or "")


def compute_deterministic_project_title(project_id: str) -> Optional[str]:
    """Project counterpart to compute_deterministic_title above (task #167/
    #168, 2026-08-04, Marc's direct report). The issue-side function got three
    real quality fixes (boilerplate strip, redundant-name skip, Ariba-
    submitter fallback) that never carried over to projects - projects.name
    is set once at creation by workgraph_projects._project_name_for (the
    ORIGINAL, unimproved logic) and then never revisited, so every Ariba
    requisition-approval project (an automated sender, so its own external-
    party check always misses) permanently keeps its raw, boilerplate-heavy
    subject as its name forever. That also turns out to be the real driver
    behind Marc's separate "why is everything a PR" perception (task #167) -
    the actual category split isn't PR-dominated, but every Ariba-sourced
    project is stuck showing the same repetitive "Action required: Approve
    the Requisition that ... submitted" boilerplate, which reads as "PR" at
    a glance regardless of its real category.

    Aggregates parties across every member issue (not just one) since a
    project's real external/internal contacts can be split across its
    issues, then reuses the exact same topic/party logic as the issue
    version via the shared helper. Topic comes from the earliest-created
    member issue's title - the same one workgraph_projects._project_name_for
    already treats as the project's representative subject."""
    project = ws.get_project(project_id)
    if not project:
        return None
    member_issues = ws.list_issues_for_project(project_id)
    if not member_issues:
        return None
    parties = []
    for iss in member_issues:
        parties.extend(ws.list_parties_for_issue(iss["id"]))
    seed = min(member_issues, key=lambda i: i.get("created_at") or 0)
    return _compute_deterministic_title_from_parties(parties, seed.get("title") or "")


def _compute_deterministic_title_from_parties(parties: list, raw_title: str) -> Optional[str]:
    """Shared party-resolution + topic-cleanup core of compute_deterministic_
    title/compute_deterministic_project_title (split out task #167/#168,
    2026-08-04, so the project path gets the exact same boilerplate-strip/
    redundant-name/Ariba-submitter-fallback treatment instead of a second,
    inevitably-drifting copy).

    Sorts both lists by first_seen_ts ascending before picking [0] (added
    task #167/#168, 2026-08-04, checking real live output against
    multi-issue projects) - the project path aggregates parties across
    every member issue, so an unordered pick here has real multi-company
    exposure the original single-issue version didn't (confirmed live:
    proj-046 has both a Nebius and a Databricks contact across its two
    issues - an unordered [0] could pick either, depending on unspecified
    JOIN/dict-insertion order). Same real, stable tie-break workgraph_
    projects._project_name_for already uses for the same reason."""
    internal = sorted(
        [p for p in parties if p.get("affiliation") == "internal" and p.get("display_name")],
        key=lambda p: p.get("first_seen_ts") or 0,
    )
    # A no-reply/system sender's domain-derived "company" (e.g. 'Ansmtp' from
    # no-reply@ansmtp.ariba.com) isn't a real supplier name - skip those.
    external = sorted(
        [p for p in parties if p.get("affiliation") == "external"
         and not workgraph_signals.is_automated_sender(p.get("primary_email") or "")],
        key=lambda p: p.get("first_seen_ts") or 0,
    )
    requestor = internal[0]["display_name"] if internal else None
    supplier = None
    if external:
        supplier = external[0].get("company") or external[0].get("display_name")

    topic = strip_subject_prefix(raw_title)
    ariba_match = _ARIBA_REQUISITION_BOILERPLATE_RE.match(topic)
    if ariba_match and not requestor:
        requestor = _titlecase_name(ariba_match.group("submitter"))
    topic = _ARIBA_REQUISITION_BOILERPLATE_RE.sub("", topic).strip(" -–—")
    if len(topic) > 60:
        topic = topic[:57].rstrip() + "..."

    if requestor and requestor.lower() in topic.lower():
        requestor = None
    if supplier and supplier.lower() in topic.lower():
        supplier = None

    if not requestor and not supplier:
        return None  # no real, non-redundant name/company on hand - the raw title is all there is, don't fake improve it
    parts = [p for p in (requestor, supplier, topic) if p]
    return " - ".join(parts)


def backfill_derived_titles() -> dict:
    """Scoped to issues currently MISSING a derived_title only (task #52,
    2026-08-04) - originally iterated every issue every call, redoing the
    same computation for already-titled issues and never getting wired
    into the live pipeline at all (found dead: no caller anywhere but this
    module's own definition). Now cheap enough to run every scheduled_
    refresh cycle (see run_derived_title_backfill below) rather than only
    as a one-time manual pass - real party data (a new external contact,
    a newly-resolved internal name) can arrive well after an issue's
    first classification, so an issue compute_deterministic_title
    couldn't title on cycle 1 may become titleable on a later cycle.
    Never overwrites an existing title (curator's own, or a prior
    deterministic one) - only fills a genuine gap.

    Extended task #167/#168 (2026-08-04) to also cover projects - live-DB
    check found 0 of 52 projects had ever gotten a derived_title (curator's
    synthesis wake writes real summaries for projects but wasn't reliably
    supplying derived_title on those writes), which is why every project's
    list-row name was still the raw, boilerplate-heavy subject line Marc
    kept seeing. Same never-overwrite guarantee applies."""
    issue_ids = ws.list_issue_ids_missing_derived_title()
    updated = 0
    for issue_id in issue_ids:
        title = compute_deterministic_title(issue_id)
        if title:
            ws.set_derived_title("issue", issue_id, title)
            updated += 1
    project_ids = ws.list_project_ids_missing_derived_title()
    projects_updated = 0
    for project_id in project_ids:
        title = compute_deterministic_project_title(project_id)
        if title:
            ws.set_derived_title("project", project_id, title)
            projects_updated += 1
    return {
        "checked": len(issue_ids), "updated": updated,
        "projects_checked": len(project_ids), "projects_updated": projects_updated,
    }


def derive_target_state(issue_id: str) -> str:
    """Pure, read-only: what state an issue's evidence alone implies, with no
    side effects and no regard for its CURRENT state. Extracted out of
    recompute_issue_state() (2026-08-01) so a caller that only wants to ask
    "does the evidence actually support closing this?" - e.g. server_lean.py's
    manual mark-done endpoints, checking whether to attach an advisory
    warning before a human overrides it - doesn't have to invoke the
    stateful function (which writes to the DB and respects/preserves an
    already-closed issue) just to read this one signal."""
    items = ws.get_raw_items_for_issue(issue_id)
    classified = [i for i in items if i.get("classified")]
    classes = {i["item_class"] for i in classified}
    if "ACTIONABLE-ASK" in classes:
        return "active"
    if "WAITING-ON-OTHERS" in classes:
        return "waiting"

    # 2026-08-01 (real-incident follow-up, latent-risk half): item_class alone
    # is override-able (a live signal_treatment_override can remap what
    # ariba_pr_approval_needed etc. map to), so before trusting "nothing to
    # track" for a known open-ended request, check the STABLE signal_type
    # identity too - it's a fixed regex-template match, never affected by any
    # override. An issue that ever received a real "approval needed"/
    # "signature requested" email but never received the matching real
    # closure email is never "done" via this path, no matter what any
    # override says the trigger's treatment/item_class currently is.
    signal_types_present = {i["signal_type"] for i in classified if i.get("signal_type")}
    for request_type in signal_types_present:
        closure_type = workgraph_signals.REQUEST_TO_CLOSURE_SIGNAL.get(request_type)
        if closure_type and closure_type not in signal_types_present:
            return "active"

    return "done"  # thread has only ever been FYI-EVIDENCE/NOISE - nothing to track


def recompute_issue_state(issue_id: str, *, new_item_is_actionable: bool = True) -> Optional[str]:
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
    is otherwise left alone, not silently reverted by routine re-classification.

    Fixed 2026-07-31 (real adversarial review): the docstring above already
    said this, but the code didn't check it - target was always re-derived
    from the FULL history, so ANY old, already-resolved ACTIONABLE-ASK
    (even one the human's manual close was based on) kept target == "active"
    forever, and the very next unrelated item (an ordinary FYI reply) would
    re-trigger this function and silently flip a "done" issue back to
    "active" - confirmed reproducible via the resolved-issue+later-FYI case.

    new_item_is_actionable=False is passed by cluster_and_link() when the
    SPECIFIC item that just triggered this call for THIS issue is itself
    NOT an ACTIONABLE-ASK - the exact "old ask still in history, new item
    is just an FYI" shape. Defaults to True (today's full-history-
    derivation behavior, unchanged) for callers with no specific new-item
    context: backfill_reclassify's ruleset-change re-derivation and the
    manual bulk-recompute path both legitimately want to reconsider a
    closed issue even without a brand-new item (a ruleset improvement
    retroactively revealing a real unresolved ask, or an explicit "recompute
    everything" request, are real reasons - unlike routine new mail
    arriving on an already-resolved thread)."""
    target = derive_target_state(issue_id)

    issue = ws.get_issue(issue_id)
    if issue is None:
        return None
    current = issue["state"]
    if current in ("done", "noise-archived", "dismissed") and (target != "active" or not new_item_is_actionable):
        return current  # respect a manual close/archive/dismiss unless a genuinely new ask justifies reopening
    if current != target:
        ws.update_issue(issue_id, state=target)
    return target


def _fyi_item_has_a_real_signal(item: dict, known_companies: Optional[set] = None) -> bool:
    """Corrected pipeline Phase E (2026-08-05): the gate on whether a
    standalone FYI-EVIDENCE item (no thread/container match, no reference/
    jasper-ref/subject-match attach) is real signal worth a cluster of its
    own, or genuine noise to drop exactly as before. A lightweight version
    of the same data-point vocabulary _matched_data_points checks on an
    already-linked work_object (reference/supplier/stakeholder/subject_
    entity/product_service/amount) - deliberately computed straight off the
    bare item here, since it has no issue/cluster of its own YET for the
    real signature machinery (compute_work_object_signature) to read parties/
    lineage from.

    Real gap this closes: a calendar meeting whose organizer/attendees are a
    real external company (Marc's own confirmed example - ~19 Authenticx
    meetings, no shared ConversationID/thread with anything, but every one
    carries a real supplier) used to hit this exact skip path and vanish
    permanently before ANY data-point matching ever ran on it. classify_
    affiliation is a pure domain-heuristic function (no DB read) - safe to
    call here on a not-yet-linked item.

    Fixed 2026-08-05 (same day, caught live running the Authenticx
    acceptance test end-to-end): the first version of this only checked
    item['from_actor'] - blind to the real, common shape where a Lilly-
    internal person ORGANIZES the meeting (from_actor) but the actual
    external counterparty is only a PARTICIPANT/attendee. Confirmed live:
    several real "VOC & Authenticx"/"Authenticx VOC QBR" meetings organized
    by a Lilly employee, with cameron.hilt@authenticx.com listed only in
    participants, were still being dropped by the from_actor-only check -
    exactly the case this phase exists to fix. Now checks every participant,
    not just the organizer.

    Checks, any one of which is sufficient:
      - a real captured PR/PO reference (item['pr_number_base']).
      - a real external company on the sender's OR any participant's
        domain (excludes an automated/system sender, which classify_
        affiliation already resolves to company=None).
      - an Ariba-extracted requester/descriptor/amount on the subject line
        (workgraph_signals.extract_ariba_requisition_fields) - the same
        stakeholder/product_service/amount vocabulary compute_work_object_
        signature already derives from an issue's title, here read straight
        off the raw item's own subject instead.
      - a known supplier name as a whole folder segment or filename token on
        a DOCUMENT (workgraph_signals.document_path_company_match) - added by
        task #414, and only consulted when the caller supplies
        `known_companies`; see below.

    Zero of the above is treated as real noise, same as before this phase -
    not every FYI-EVIDENCE item becomes a cluster, only ones carrying an
    actual identifiable data point.

    Task #414 (2026-08-21): the fourth check exists because the first three
    are structurally unreachable for source == "sharepoint". Measured live:
    every one of 100 unlinked SharePoint raw_items failed this gate, and all
    100 failed for want of any input at all - from_actor is NULL and
    participants is [] for that source by construction (the search payload
    carries no author field), so the domain check can never fire, and a
    document filename does not parse as an Ariba subject. A supplier contract
    was therefore indistinguishable from noise here. See
    document_path_company_match's own docstring for why the match is
    whole-token equality rather than a substring or a score.

    `known_companies` is an OPTIONAL parameter rather than a lookup inside
    this function on purpose: the paragraph above notes that classify_
    affiliation is a pure domain heuristic with no DB read, which is what
    makes this safe to call on a not-yet-linked item. Keeping the vocabulary
    an argument preserves that property (and lets the caller load it once for
    the whole loop instead of per item). Omitted or empty, this behaves
    exactly as it did before #414."""
    if item.get("pr_number_base"):
        return True
    emails = [item.get("from_actor") or ""] + (_parse_participants(item) or [])
    for email in emails:
        if not email:
            continue
        affiliation = workgraph_parties.classify_affiliation(email)
        if affiliation.get("affiliation") == "external" and affiliation.get("company"):
            return True
    ariba_fields = workgraph_signals.extract_ariba_requisition_fields(item.get("subject") or "")
    if ariba_fields and (ariba_fields.get("requester") or ariba_fields.get("descriptor")):
        return True
    if known_companies:
        meta = item.get("meta_json")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = None
        web_url = (meta or {}).get("web_url") if isinstance(meta, dict) else None
        if workgraph_signals.document_path_company_match(
                item.get("subject"), web_url, known_companies):
            return True
    return False


def _sender_domain_seen_on_issue(issue_id: str, from_actor: Optional[str]) -> bool:
    """True if `from_actor`'s domain matches (exact or real subdomain,
    workgraph_signals.domain_matches - never a spoofable substring check)
    any sender already seen on this issue's existing evidence. The second
    of the two required conditions for the subject-match fallback below -
    an exact normalized-subject match ALONE is a real false-positive risk
    (a generic recurring-meeting title reused by a completely different
    pair of companies); requiring the actual counterparty too closes it."""
    domain = workgraph_signals.sender_domain(from_actor)
    if not domain:
        return False
    for existing in ws.get_raw_items_for_issue(issue_id):
        if workgraph_signals.domain_matches(existing.get("from_actor") or "", domain):
            return True
    return False


def _build_teams_sender_email_index() -> dict[str, set[str]]:
    """email (lowercased) -> set of OPEN issue ids with at least one raw_item
    carrying that email as from_actor or a participant. Task #179 (Marc's
    direct design ask): 'the sender being the main link... you'd match the
    sender's message to any where the sender of the team's message was
    also the sender of the email or copied on the email.' Built once per
    cluster_and_link() run, same discipline as open_issues_by_subject just
    above it - a per-item DB scan for every pending Teams message would be
    needlessly expensive when the candidate set (open issues) is small and
    stable within one run."""
    index: dict[str, set[str]] = {}
    for iss in ws.list_issues(states=["active", "waiting", "blocked"], limit=5000):
        for raw in ws.get_raw_items_for_issue(iss["id"]):
            emails = set()
            frm = (raw.get("from_actor") or "").strip().lower()
            if "@" in frm:
                emails.add(frm)
            try:
                participants = json.loads(raw.get("participants") or "[]")
            except (TypeError, ValueError):
                participants = []
            for p in participants:
                p = (p or "").strip().lower()
                if "@" in p:
                    emails.add(p)
            for email in emails:
                index.setdefault(email, set()).add(iss["id"])
    return index


def _teams_sender_emails(from_actor: Optional[str], by_display_name: dict, by_local_part: dict) -> set[str]:
    """Resolves a Teams item's sender to a real email identity - direct if
    from_actor already looks like an email (Graph sometimes returns one),
    otherwise via workgraph_parties' existing bare-name resolver, which
    only ever returns an EXISTING, unambiguous party (never invents one,
    never guesses through a display-name collision). Returns an empty set
    - not a fuzzy best-guess - when neither applies, since a wrong sender
    identity here would misattach a real Teams ask to the wrong project."""
    frm = (from_actor or "").strip()
    if not frm:
        return set()
    if "@" in frm:
        return {frm.lower()}
    resolved = workgraph_parties._resolve_bare_name(frm, by_display_name, by_local_part)
    return {resolved["primary_email"].lower()} if resolved else set()


def _teams_item_has_corroborating_signal(item: dict, issue: dict) -> bool:
    """The 'along with one other data point' half of Marc's design - a
    matching sender alone is exactly the kind of single-signal match that
    caused real false-positive merges elsewhere in this codebase (task #81).
    Two independent checks, either is enough: a real (non-'other') category
    match, or a genuine topic-key overlap between the Teams item's own
    subject/title and the candidate issue's title (same longest-common-
    substring approach and length floor as workgraph_projects._shared_topic_
    key, deliberately reused rather than re-invented)."""
    category = item.get("topic")
    if category and category != "other" and category == issue.get("category"):
        return True
    item_key = ws.normalize_topic_key(item.get("subject") or "")
    issue_key = ws.normalize_topic_key(issue.get("title") or "")
    if len(item_key) >= workgraph_projects.MIN_TOPIC_KEY_LEN and len(issue_key) >= workgraph_projects.MIN_TOPIC_KEY_LEN:
        m = SequenceMatcher(None, item_key, issue_key).find_longest_match(0, len(item_key), 0, len(issue_key))
        if m.size >= workgraph_projects.MIN_TOPIC_KEY_LEN:
            return True
    return False


def _teams_sender_anchor_match(
    item: dict, sender_email_index: dict, by_display_name: dict, by_local_part: dict,
) -> Optional[str]:
    """Task #179 end-to-end: resolve the Teams item's sender to an email,
    find every OPEN issue that email appears as sender-or-participant on,
    and return the first one that also clears the corroborating-signal bar
    above. None (never a guess) if the sender doesn't resolve, resolves to
    no candidate issues, or no candidate clears the corroboration bar."""
    emails = _teams_sender_emails(item.get("from_actor"), by_display_name, by_local_part)
    if not emails:
        return None
    candidate_ids: set[str] = set()
    for email in emails:
        candidate_ids |= sender_email_index.get(email, set())
    for issue_id in candidate_ids:
        issue = ws.get_issue(issue_id)
        if issue and _teams_item_has_corroborating_signal(item, issue):
            return issue_id
    return None


def _effective_thread_key(item: dict) -> str:
    """The real grouping key for this item — the bare thread_key for every
    source except `teams_chat`, where it's session-scoped instead (Section
    3.2/8 of docs/design/CONFIDENCE_AND_IDENTITY_REDESIGN.md). Wired live
    2026-08-03, after a shadow comparison against the real corpus (4 of 17
    Teams containers are genuinely multi-session; all currently route to
    at most one issue each, so this only changes where a FUTURE message
    lands, never reassigns history) and a read-only backtest_scored_model
    run that found marc-362 — the confirmed multi-topic chat this fixes —
    generating false-positive cross-project matches via its blended,
    whole-chat signal.

    A session boundary inside the same physical Teams chat now behaves
    exactly like a different Outlook ConversationID already does: no
    match, falls through to the existing new-issue/hold-aside logic below
    (task #54/#55) rather than force-attaching to whatever issue the flat
    thread_key used to point at."""
    thread_key = item.get("thread_key")
    if item.get("source") != "teams_chat" or not thread_key:
        return thread_key
    messages = ws.list_raw_items_by_thread_key("teams_chat", thread_key)
    sessioned = workgraph_sessionize.sessionize_teams_messages(messages)
    this_id = item.get("id")
    session_seq = next((m["session_sequence"] for m in sessioned if m.get("id") == this_id), 0)
    return f"{thread_key}::s{session_seq}"


def _container_identity(item: dict, exact_key: Optional[str]) -> Optional[tuple[str, str, str, str]]:
    """Task #184 Phase B (2026-08-05, Marc's direct redesign): the real
    pass-1 clustering key is now source_containers (task #75/#76's
    identity-formalization layer), not the flat thread_map string key -
    the same typed (source, container_type, exact_key, key_quality)
    identity workgraph_identity.backfill_identity_anchors already computes
    for existing issues after the fact. Reusing that module's own
    _CONTAINER_TYPE_BY_SOURCE/_container_key_quality here (rather than a
    second, inevitably-drifting copy) means the live path and the daily
    backfill sweep can never disagree about what a container's identity
    is.

    exact_key is passed in (the caller's own _effective_thread_key(item)
    result) rather than recomputed here - for a teams_chat item, that
    call does a real DB read + sessionize pass, and cluster_and_link's
    loop needs BOTH a lookup and, on a match, a set for the same item;
    recomputing per call would double that read for no reason.

    Returns (source, container_type, exact_key, key_quality), or None
    when the item has no source/thread_key this layer recognizes (e.g. a
    source type not in workgraph_identity's map, or a null thread_key) -
    the caller falls through to the item's existing new-issue/hold-aside
    logic exactly as it already does when thread_map had no match."""
    source = item.get("source")
    container_type = workgraph_identity._CONTAINER_TYPE_BY_SOURCE.get(source)
    if not container_type or not exact_key:
        return None
    key_quality = workgraph_identity._container_key_quality(source, item.get("thread_key_source"))
    return source, container_type, exact_key, key_quality


def _container_lookup_issue(item: dict, exact_key: Optional[str]) -> Optional[str]:
    """Read half of the source_containers-based clustering key - None if
    this item has no recognized container identity, or a container exists
    for this exact key but has no issue_id yet (a container can be
    recorded with issue_id=None - upsert_source_container's own default -
    though nothing on the live path does that today)."""
    identity = _container_identity(item, exact_key)
    if identity is None:
        return None
    source, container_type, key, _ = identity
    row = ws.source_container_lookup(source=source, container_type=container_type, exact_key=key)
    return row["issue_id"] if row else None


def _container_set_issue(item: dict, exact_key: Optional[str], issue_id: str) -> None:
    """Write half - upsert_source_container is keyed on (source,
    container_type, exact_key) and idempotent (ON CONFLICT DO UPDATE
    issue_id), so re-linking a later item on the same container is a safe
    no-op/refresh, same discipline thread_map_set's INSERT OR REPLACE
    always had. No-ops (does not raise) if this item has no recognized
    container identity - callers only reach here after a successful
    _container_identity resolution already gated the call, but staying
    defensive here costs nothing and matches this module's existing
    fail-open-to-None discipline elsewhere."""
    identity = _container_identity(item, exact_key)
    if identity is None:
        return
    source, container_type, key, key_quality = identity
    ws.upsert_source_container(
        id=f"sc-{source}-{key}", source=source, container_type=container_type,
        exact_key=key, key_quality=key_quality, issue_id=issue_id,
    )


def cluster_and_link(limit: int = 500) -> dict:
    """For every classified-but-not-yet-linked item (issue_id IS NULL),
    resolve via source_containers (an existing Issue's container identity -
    see _container_lookup_issue above; thread_map, the older flat-string-key
    model this replaced, was fully retired and removed 2026-08-07) or create
    a new Issue. NOISE items are never promoted to an Issue - they're classified and
    left ungrouped (auditable, per the reference file's precision-favoring
    default), not silently dropped. FYI-EVIDENCE items get the same
    not-promoted treatment when there's no existing thread to attach to -
    a standalone informational note (a meeting recap, a group-chat aside)
    isn't something Marc needs to track as an open Issue on its own.

    Part C of the grouping/NBA redesign (2026-07-30): before creating a
    brand-new issue for an item with no thread_key match, check whether its
    real reference ID (PR/PO number) already matches an existing OPEN
    issue - Outlook's ConversationID (thread_key) only links true reply
    chains, so a freshly-sent automated reminder (not a reply) about the
    same requisition gets a NEW ConversationID and would otherwise always
    create a duplicate issue (the exact real bug Part A1 fixed
    retroactively via backfill). Gated behind config('grouping',
    'reference_id_auto_attach_enabled'), same report-only-by-default
    pattern as retention.py's enforcement_enabled: always COMPUTES and
    counts what it would do; only actually attaches when the flag is on.
    Ships with the flag OFF - this has no backtest path the way Part A2
    does, so it can only be validated by watching would_attach_via_
    reference against real new mail for a bake-in period first.

    Part D (2026-08-01, real-incident follow-up): the "curator LLM layer"
    this module's own header comment says handles standalone-FYI residue
    was never actually built - confirmed by grepping the whole repo for it.
    A real thread ("Early Renewal Weekly Meeting", 7 messages across
    Outlook's own reminder/auto-reply/[EXTERNAL] noise, no two sharing a
    ConversationID) sat permanently unlinked as a result. Before giving up
    on a standalone FYI, this now also checks whether its normalized
    subject (normalize_subject_for_matching - strips [EXTERNAL]/Automatic
    reply:/the calendar-reminder wrapper on top of the usual Re:/Fwd:)
    exact-matches an OPEN issue's own title AND the sender's domain matches
    a domain already seen on that issue's existing evidence - both required,
    since either alone is a real false-positive risk (a generic subject
    line reused by an unrelated sender; the right sender emailing about
    something unrelated with a coincidentally-matching subject). Same
    report-only-by-default pattern as the reference-id path above -
    ships OFF, always counted, no backtest path yet either."""
    reference_auto_attach = bool(config.get("grouping", "reference_id_auto_attach_enabled"))
    subject_match_auto_attach = bool(config.get("grouping", "subject_match_auto_attach_enabled"))
    # Task #36: a Jasper-authored "Ref: JW-<id>" tag echoed back on an
    # inbound reply/forward - see workgraph_signals.JASPER_REF_RE. Stronger
    # than the PR/PO reference match above (that only proves "same
    # transaction"; this names the exact issue), but still gated behind its
    # own flag rather than defaulting on, same report-only-first discipline
    # as reference_id_auto_attach_enabled/subject_match_auto_attach_enabled
    # - this is a brand-new extraction path with no bake-in history yet.
    jasper_ref_auto_attach = bool(config.get("grouping", "jasper_ref_auto_attach_enabled"))
    # Task #179: same report-only-first discipline as the three flags above -
    # ships OFF, always computed/counted (would_attach_via_teams_sender_
    # anchor) regardless, no backtest path yet either.
    teams_sender_anchor_auto_attach = bool(config.get("grouping", "teams_sender_anchor_auto_attach_enabled"))
    now = time.time()
    with_pending = ws.get_items_pending_link(limit)
    # Corrected-ordering redesign (2026-08-05): includes clusters, not just
    # real issues - a fresh, unmatched item now becomes a cluster (Phase B),
    # so the subject-match candidate pool must see clusters too, or the
    # very first item on a recurring-subject thread would be permanently
    # invisible to this fallback (the same reason the reference/jasper-ref
    # attach paths above already became type-agnostic).
    open_issues_by_subject: dict[str, str] = {}
    for iss in ws.list_issues(states=["active", "waiting", "blocked"], limit=5000) + ws.list_clusters(limit=5000):
        key = normalize_subject_for_matching(iss.get("title") or "")
        if key:
            open_issues_by_subject.setdefault(key, iss["id"])
    # Only built when there's at least one pending Teams item - both indexes
    # are a real scan over every open issue's evidence/parties, no reason to
    # pay that cost on a run with nothing to use it for.
    teams_sender_index: dict[str, set[str]] = {}
    teams_by_display_name: dict = {}
    teams_by_local_part: dict = {}
    if any(i.get("source") == "teams_chat" for i in with_pending):
        teams_sender_index = _build_teams_sender_email_index()
        teams_by_display_name, teams_by_local_part = workgraph_parties._build_party_indexes()
    # Task #414: the supplier vocabulary the standalone-FYI gate's fourth check
    # matches document folder segments/filename tokens against. Loaded once for
    # the whole run rather than per item, and - same discipline as the two Teams
    # indexes above - only when there is actually a document in this batch to
    # use it on. This is the SAME vocabulary pass-2 matching already uses for
    # its "supplier" point (workgraph_projects._matched_data_points reads
    # dp-fasttrack-supplier), not a second parallel notion of who a supplier is.
    known_companies: set[str] = set()
    if any(i.get("source") == "sharepoint" for i in with_pending):
        known_companies = {
            (row.get("value") or "").strip().lower()
            for row in ws.list_data_point_values_for_definition(
                workgraph_discovery.FASTTRACK_SUPPLIER_ID)
        }
        known_companies.discard("")
    created = 0
    linked = 0
    skipped_noise = 0
    skipped_fyi_standalone = 0
    promoted_fyi_to_cluster = 0
    skipped_teams_standalone = 0
    attached_via_reference = 0
    would_attach_via_reference = 0
    attached_via_subject_match = 0
    would_attach_via_subject_match = 0
    attached_via_jasper_ref = 0
    would_attach_via_jasper_ref = 0
    attached_via_teams_sender_anchor = 0
    would_attach_via_teams_sender_anchor = 0
    touched_issues = set()
    # 2026-07-31: tracks which touched issues had a genuinely NEW
    # ACTIONABLE-ASK item land this run (vs. just an FYI/waiting reply) -
    # see recompute_issue_state's own docstring for why this must be
    # per-issue, not "any historical item anywhere."
    newly_actionable_issues = set()
    touched_pattern_signatures = set()

    for item in with_pending:
        if item["item_class"] == "NOISE":
            skipped_noise += 1
            ws.mark_link_checked(item["id"], now)
            continue

        thread_key = _effective_thread_key(item)
        issue_id = _container_lookup_issue(item, thread_key)
        reference_match = None
        jasper_ref_match = None
        teams_sender_match = None
        if issue_id is None:
            # Task #36: checked FIRST, ahead of the PR/PO reference match
            # below - a Jasper ref tag names the exact issue directly
            # (Jasper's own tooling put it there), rather than merely
            # sharing a transaction number with it. A stale tag (quoted
            # from an old thread, or naming an issue that's since closed
            # or was deleted) is never forced - it just falls through to
            # the normal matching below, same as no tag at all.
            jasper_ref_candidate = item.get("jasper_ref_issue_id")
            if jasper_ref_candidate:
                # Corrected-ordering redesign (2026-08-05): get_issue_or_cluster,
                # not get_issue - the tag names an exact id directly, and that
                # id is equally valid whether it's already a real issue or
                # still an unpromoted cluster.
                candidate_issue = ws.get_issue_or_cluster(jasper_ref_candidate)
                if candidate_issue and candidate_issue["state"] in ("active", "waiting", "blocked"):
                    jasper_ref_match = jasper_ref_candidate
                    would_attach_via_jasper_ref += 1
            if jasper_ref_match and jasper_ref_auto_attach:
                issue_id = jasper_ref_match
                _container_set_issue(item, thread_key, issue_id)
                attached_via_jasper_ref += 1
        if issue_id is None:
            # 2026-07-31: match on pr_number_base (version-stripped), not
            # the full pr_number - see list_open_issue_ids_for_reference's
            # own docstring for why (PR416079-V32/V33 are the same real
            # requisition, not two unrelated strings).
            pr_number_base = item.get("pr_number_base")
            if pr_number_base:
                # Corrected-ordering redesign (2026-08-05): list_open_work_
                # objects_for_reference, not list_open_issue_ids_for_
                # reference - a fresh item sharing this PR/PO with an
                # existing CLUSTER (not yet promoted) needs to find it too,
                # not just already-promoted issues.
                candidates = ws.list_open_work_objects_for_reference(pr_number_base)
                if candidates:
                    reference_match = candidates[0]
                    would_attach_via_reference += 1
            if reference_match and reference_auto_attach:
                issue_id = reference_match
                _container_set_issue(item, thread_key, issue_id)
                attached_via_reference += 1
            elif issue_id is None:
                if item["item_class"] == "FYI-EVIDENCE":
                    subject_match = open_issues_by_subject.get(
                        normalize_subject_for_matching(item.get("subject") or ""))
                    if subject_match and _sender_domain_seen_on_issue(subject_match, item.get("from_actor")):
                        would_attach_via_subject_match += 1
                        if subject_match_auto_attach:
                            issue_id = subject_match
                            attached_via_subject_match += 1
                    if issue_id is None:
                        # Corrected pipeline Phase E (2026-08-05): before
                        # giving up entirely, check whether this standalone
                        # FYI actually carries a real data point of its own
                        # (see _fyi_item_has_a_real_signal's own docstring -
                        # the exact gap that dropped ~1,475 of 1,936 real
                        # unlinked raw_items, including every one of a real
                        # ~19-meeting series with a genuine external
                        # supplier, permanently before any matching ever ran
                        # on them). Zero signal types is still real noise,
                        # skipped exactly as before; one or more falls
                        # through to the normal cluster-creation path below
                        # instead of being dropped - it gets a real
                        # signature and becomes eligible for pass-2 matching
                        # on the next wake.
                        if not _fyi_item_has_a_real_signal(item, known_companies):
                            skipped_fyi_standalone += 1
                            ws.mark_link_checked(item["id"], now)
                            continue
                        promoted_fyi_to_cluster += 1
                # Task #179 (2026-08-04, Marc's direct design ask), tried
                # BEFORE the task #54/#55 hold-aside below: "the sender being
                # the main link... you'd match the sender's message to any
                # where the sender of the team's message was also the sender
                # of the email or copied on the email, along with one other
                # data point." A Teams ask that clears this bar has real
                # signal a bare "can you look at X" doesn't - it's not the
                # guess the hold-aside fix below was written to stop, it's an
                # actual identified counterparty PLUS a category/topic
                # corroboration, same two-signals-minimum discipline as the
                # rest of this grouping-v3 phase.
                teams_sender_match = None
                if item.get("source") == "teams_chat" and item["item_class"] in ("ACTIONABLE-ASK", "WAITING-ON-OTHERS"):
                    teams_sender_match = _teams_sender_anchor_match(
                        item, teams_sender_index, teams_by_display_name, teams_by_local_part)
                    if teams_sender_match:
                        would_attach_via_teams_sender_anchor += 1
                        if teams_sender_anchor_auto_attach:
                            issue_id = teams_sender_match
                            _container_set_issue(item, thread_key, issue_id)
                            attached_via_teams_sender_anchor += 1
                # Task #54/#55 (2026-08-02, Marc's direct report): "not every
                # individual message... in Teams should go into the system" -
                # confirmed live: 100% of Teams ACTIONABLE-ASK/WAITING-ON-
                # OTHERS items with no thread/reference match were forcing
                # their way into a brand-new Issue (this fallthrough always
                # created one, unconditionally, for every source). A casual
                # "can you look at X" in a Teams chat has no real signal
                # distinguishing it from a genuinely trackable ask at this
                # point - the fix isn't a better guess, it's not guessing:
                # hold it aside the same way an unmatched FYI-EVIDENCE item
                # already is, surfaced in the held-aside queue
                # (GET /api/workgraph/held-aside-teams) for a human decision
                # instead. Scoped to source == "teams_chat" only, on purpose -
                # email/calendar asks keep their existing behavior unchanged;
                # this is a Teams-specific clutter problem Marc reported, not
                # a general "distrust every new ask" policy change.
                if issue_id is None and item.get("source") == "teams_chat" and item["item_class"] in ("ACTIONABLE-ASK", "WAITING-ON-OTHERS"):
                    skipped_teams_standalone += 1
                    ws.mark_link_checked(item["id"], now)
                    continue
                if issue_id is None:
                    # Corrected-ordering redesign (2026-08-05, Marc's direct
                    # correction): a fresh, unmatched communication becomes a
                    # CLUSTER now, never a real issue directly. "Issue" is a
                    # derived output of an already-confirmed project (Phase D,
                    # curator's content-extraction step) - it is no longer
                    # something created immediately at ingest time. Real
                    # issue/checklist creation, NBA scoring, and state
                    # tracking all wait for that later promotion; see the
                    # touched_issues/touched_clusters split below for why the
                    # post-processing at the end of this loop skips clusters.
                    title = strip_subject_prefix(item.get("subject") or "(no subject)")
                    issue_id = ws.create_cluster_with_new_id(title=title, category=item.get("topic"))
                    _container_set_issue(item, thread_key, issue_id)
                    created += 1

        summary = item.get("subject") or item.get("body_preview") or "(no summary)"
        if issue_id == jasper_ref_match and jasper_ref_auto_attach:
            summary = f"{summary} [auto-attached via Jasper reference tag]"
        elif issue_id == reference_match and reference_auto_attach:
            # Real, visible breadcrumb on the issue itself (Progress
            # timeline) - never silently fold a raw_item in without a
            # trace of why it landed here instead of a new issue.
            summary = f"{summary} [auto-attached via shared reference {item.get('pr_number')}]"
        elif subject_match_auto_attach and item["item_class"] == "FYI-EVIDENCE" and issue_id == open_issues_by_subject.get(
                normalize_subject_for_matching(item.get("subject") or "")):
            summary = f"{summary} [auto-attached via matching subject + sender]"
        elif teams_sender_match and issue_id == teams_sender_match and teams_sender_anchor_auto_attach:
            summary = f"{summary} [auto-attached via Teams sender anchor + corroborating signal]"
        ws.link_raw_item_to_issue(item["id"], issue_id)
        ws.add_evidence(
            issue_id=issue_id,
            type=_evidence_type(item["source"]),
            summary=summary,
            raw_item_id=item["id"],
        )
        # Task #265 (2026-08-07): now that issue_id is finalized for this
        # item, persist any real ContractPodAI request fields into their
        # own system-scoped table (see workgraph_signals.
        # extract_contractpodai_request_fields' own docstring for why this
        # is a dedicated table, not generic personal vocabulary). Cheap
        # (regex only, no LLM) - fine to attempt on every item, but only
        # ContractPodAI-sourced ones will ever actually match.
        if (item.get("signal_type") or "").startswith("contractpodai_"):
            cpai_fields = workgraph_signals.extract_contractpodai_request_fields(
                item.get("subject") or "", text_extract.resolve_item_text(item)
            )
            if cpai_fields:
                ws.upsert_contractpodai_request(cpai_fields, raw_item_id=item["id"], issue_id=issue_id)
        # Task #267 (2026-08-07): same system-scoped treatment for Ariba's
        # requester/descriptor/amount - extract_ariba_requisition_fields
        # already ran as a significance check in _has_matchable_signal, but
        # its result was read-and-discarded there, never persisted anywhere
        # queryable. Reference-ID matching itself needs no new wiring -
        # REFERENCE_ID_RE's generic full-text scan already catches the PR#
        # in the subject the same as any other PR/PO number.
        if (item.get("signal_type") or "").startswith("ariba_"):
            ariba_fields = workgraph_signals.extract_ariba_requisition_fields(item.get("subject") or "")
            if ariba_fields and ariba_fields.get("pr_number"):
                ws.upsert_ariba_requisition(ariba_fields, raw_item_id=item["id"], issue_id=issue_id)
        linked += 1
        touched_issues.add(issue_id)
        if item["item_class"] == "ACTIONABLE-ASK":
            newly_actionable_issues.add(issue_id)

        # Personalized data-point discovery (design doc, task #213): the
        # continuous, no-LLM-cost half of the two-tier mechanism - pure
        # counting into candidate_pattern_observations, cheap enough to run
        # on every linked item without a second thought. The LLM-proposal
        # half only fires once per BATCH below (touched_pattern_signatures),
        # never per item - a real LLM call is not cheap enough to run on
        # every single classified email.
        for obs_row in workgraph_discovery.record_observations_for_item(item):
            touched_pattern_signatures.add(obs_row["pattern_signature"])

        # Starter-task creation is deliberately NOT done here anymore - a
        # freshly-created work object from this path is always a cluster
        # now (never a real issue directly), and a cluster has no checklist
        # of its own to seed. Real task/checklist-item creation happens at
        # Phase D (curator extracting issues from a confirmed project's
        # content) - this is a genuine behavior change from before tonight,
        # not an oversight.

    # Corrected-ordering redesign (2026-08-05): touched_issues can now hold
    # a mix of real issue ids (attached via an existing container/reference/
    # subject/Teams-sender match) and cluster ids (attached via the SAME
    # paths, or freshly created above) - split them, since recompute_issue_
    # state/title-generation are real-issue concepts that don't apply to a
    # cluster (no state machine, no NBA scoring of its own - those are
    # genuinely premature on unreviewed, not-yet-promoted content).
    touched_real_issues = {i for i in touched_issues if ws.get_cluster(i) is None}
    touched_clusters = touched_issues - touched_real_issues

    for issue_id in touched_real_issues:
        recompute_issue_state(issue_id, new_item_is_actionable=issue_id in newly_actionable_issues)

    # Real bug found and fixed live (2026-08-06, Marc's own direct
    # correction during the Kinaxis fragmentation follow-up): party
    # extraction was ALSO scoped to touched_real_issues only, lumped in
    # with the state-machine/NBA concepts above by the same comment - but
    # unlike those, resolving who's actually on a thread is not a
    # judgment call that could be "wrong" if the cluster later gets
    # restructured; it's a fact about the raw content, independent of
    # promotion status. workgraph_parties.run's OWN docstring already
    # says it expects "cluster_and_link's touched_issues set" (the FULL
    # set) - this call site had drifted to the narrower touched_real_
    # issues, silently starving every cluster (most of the corpus, most
    # of the time) of the exact name-resolution/company-matching signal
    # _matched_data_points already depends on. Confirmed live: 4 of 5
    # real Kinaxis-fragmentation threads were still raw clusters, so
    # their real, already-mentioned stakeholders (e.g. an internal
    # contact with an existing, resolvable party record) were never
    # being linked at all. (2026-08-07: this used to also run BEFORE
    # workgraph_projects.run's own grouping pass so fresh party data was
    # visible to that batch's matching - workgraph_projects.run and the
    # retired group_issue()/scored_grouping_decision() path it drove are
    # gone; workgraph_pipeline2.py's find_candidates/process_new_item is
    # the live replacement, called from its own separate wake, not from
    # here.)
    party_result = workgraph_parties.run(list(touched_issues))

    # Personalized data-point discovery (task #213), LLM half - only
    # signatures actually touched THIS batch get checked, bounding real
    # LLM cost to real new activity rather than a full-corpus rescan on
    # every classify run (the monthly sweep, workgraph_discovery.run_
    # monthly_sweep, is what catches anything this misses).
    if touched_pattern_signatures:
        workgraph_discovery.check_and_propose_for_signatures(
            touched_pattern_signatures, raw_items_pool=with_pending,
        )

    # Title generation runs AFTER parties/projects resolve for this batch -
    # it reads party affiliation/company, which a just-created issue doesn't
    # have until workgraph_parties.run() above has linked them.
    for issue_id in touched_real_issues:
        title = compute_deterministic_title(issue_id)
        if title:
            ws.set_derived_title("issue", issue_id, title)

    # "issues_created" is a legacy key name kept as-is (deliberately deferred
    # per the corrected-ordering plan, "cosmetic") - `created` now always
    # counts fresh CLUSTERS, since this function no longer creates real
    # issues directly at all.
    return {"issues_created": created, "clusters_touched": len(touched_clusters),
            "items_linked": linked, "noise_skipped": skipped_noise,
            "fyi_standalone_skipped": skipped_fyi_standalone,
            "fyi_promoted_to_cluster": promoted_fyi_to_cluster,
            "teams_standalone_skipped": skipped_teams_standalone,
            "attached_via_reference": attached_via_reference,
            "would_attach_via_reference": would_attach_via_reference,
            "reference_auto_attach_enabled": reference_auto_attach,
            "attached_via_subject_match": attached_via_subject_match,
            "would_attach_via_subject_match": would_attach_via_subject_match,
            "subject_match_auto_attach_enabled": subject_match_auto_attach,
            "attached_via_jasper_ref": attached_via_jasper_ref,
            "would_attach_via_jasper_ref": would_attach_via_jasper_ref,
            "jasper_ref_auto_attach_enabled": jasper_ref_auto_attach,
            "attached_via_teams_sender_anchor": attached_via_teams_sender_anchor,
            "would_attach_via_teams_sender_anchor": would_attach_via_teams_sender_anchor,
            "teams_sender_anchor_auto_attach_enabled": teams_sender_anchor_auto_attach,
            "parties": party_result}


class HeldAsideItemError(ValueError):
    """Raised by track_held_aside_item/dismiss_held_aside_item for a
    raw_item_id that doesn't exist, isn't a Teams item, or is already
    linked/reviewed - never silently a no-op, since this is always a
    direct human action on one specific row."""


def track_held_aside_item(raw_item_id: int) -> str:
    """Task #54/#55: a human's explicit "yes, actually track this" decision
    on one row from the held-aside queue (see list_held_aside_teams_items).
    Creates a real Issue for it - the exact same shape cluster_and_link's
    own new-issue fallback already builds (title/state/priority/
    confidence_tier, container-identity set, link_raw_item_to_issue, add_evidence,
    a starter task for a genuine ask, recompute_issue_state, then the same
    party-resolution pass a normal auto-created issue gets) - this is a
    human overriding the hold-aside, not a second, different way
    an issue gets created. Returns the new issue_id."""
    item = ws.get_raw_item(raw_item_id)
    if item is None:
        raise HeldAsideItemError(f"no such raw_item: {raw_item_id}")
    if item.get("source") != "teams_chat":
        raise HeldAsideItemError(f"raw_item {raw_item_id} is not a Teams item")
    if item.get("issue_id") is not None:
        raise HeldAsideItemError(f"raw_item {raw_item_id} is already linked to an issue")
    if item.get("held_aside_status") is not None:
        raise HeldAsideItemError(f"raw_item {raw_item_id} was already reviewed ({item['held_aside_status']})")

    title = strip_subject_prefix(item.get("subject") or "(no subject)")
    state = "active" if item.get("item_class") == "ACTIONABLE-ASK" else "waiting"
    issue_id = ws.create_issue_with_new_id(
        title=title, category=item.get("topic"),
        state=state, priority="med", confidence_tier=item.get("confidence") or "M",
    )
    _container_set_issue(item, _effective_thread_key(item), issue_id)
    ws.link_raw_item_to_issue(raw_item_id, issue_id)
    ws.add_evidence(
        issue_id=issue_id, type=_evidence_type(item["source"]),
        summary=(item.get("subject") or item.get("body_preview") or "(no summary)")
                + " [tracked from the held-aside Teams queue]",
        raw_item_id=raw_item_id,
    )
    if item.get("item_class") == "ACTIONABLE-ASK":
        owner = ws.find_owner_for(category=item.get("topic"), topic=item.get("topic"))
        ws.create_task(issue_id=issue_id, label=title, owner=owner)
    recompute_issue_state(issue_id, new_item_is_actionable=item.get("item_class") == "ACTIONABLE-ASK")
    workgraph_parties.run([issue_id])
    derived_title = compute_deterministic_title(issue_id)
    if derived_title:
        ws.set_derived_title("issue", issue_id, derived_title)
    ws.set_held_aside_status(raw_item_id, "tracked")
    return issue_id


def dismiss_held_aside_item(raw_item_id: int) -> None:
    """The other resolution: reviewed and confirmed NOT worth tracking -
    stays out of the Inbox permanently (list_held_aside_teams_items filters
    on held_aside_status IS NULL), same as it already silently was, just
    now a real recorded decision instead of an invisible default."""
    item = ws.get_raw_item(raw_item_id)
    if item is None:
        raise HeldAsideItemError(f"no such raw_item: {raw_item_id}")
    if item.get("source") != "teams_chat":
        raise HeldAsideItemError(f"raw_item {raw_item_id} is not a Teams item")
    if item.get("issue_id") is not None:
        raise HeldAsideItemError(f"raw_item {raw_item_id} is already linked to an issue")
    if item.get("held_aside_status") is not None:
        raise HeldAsideItemError(f"raw_item {raw_item_id} was already reviewed ({item['held_aside_status']})")
    ws.set_held_aside_status(raw_item_id, "dismissed")


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
            subject=item.get("subject") or "", body_preview=text_extract.resolve_item_text(item),
            from_actor=item.get("from_actor") or "",
            source=item.get("source"), organizer=item.get("from_actor"),
            participants=_parse_participants(item),
            confirmed_direction=_confirmed_direction_from_meta(item),
        )
        # Fixed 2026-07-29: this used to only COMPARE topic/item_class/
        # signal_type, then WRITE direction/sentiment/anomaly_flag straight
        # from the old stored row regardless - contradicting this function's
        # own docstring claim to refresh the full classify_item() result. A
        # future NEGATIVE_CUE/POSITIVE_CUE/OFF_CHANNEL/direction-cue change
        # would have silently never reached the existing backlog. Now every
        # field classify_item can produce is both compared and written.
        # 2026-07-31: pr_number_base added to the comparison - without this,
        # every existing row that already has a pr_number set (and whose
        # OTHER fields haven't changed) would be silently skipped forever,
        # leaving pr_number_base permanently NULL for the whole pre-existing
        # backlog even after this fix ships. Confirmed as the exact gotcha
        # this migration needs to avoid during the meeting-grouping/related-
        # project identity design pass.
        if (result["topic"] == item.get("topic") and result["item_class"] == item.get("item_class")
                and result["signal_type"] == item.get("signal_type")
                and result["direction"] == item.get("direction")
                and result["sentiment"] == item.get("sentiment")
                and result["anomaly_flag"] == bool(item.get("anomaly_flag"))
                and result["pr_number"] == item.get("pr_number")
                and result["pr_number_base"] == item.get("pr_number_base")
                and result["jasper_ref_issue_id"] == item.get("jasper_ref_issue_id")):
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
            pr_number_base=result["pr_number_base"],
            jasper_ref_issue_id=result["jasper_ref_issue_id"],
            # Task #54/#55: not part of the equality check above on purpose -
            # confidence was never stored before this, so every historical
            # row would register as "changed" for a field that's really just
            # newly-persisted metadata, not a genuine reclassification -
            # would inflate `updated`/touched_issues for the wrong reason.
            # Still written whenever this call fires for a real reason
            # anyway, so the backlog fills in opportunistically over time.
            confidence=result["confidence"],
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
