"""
workgraph_signals.py — recognizes known AUTOMATED signal-system emails (Ariba
PR approval flow, Adobe Sign/DocuSign signature flow, ContractPodAI CLM, the
IT Procurement Intake Power App) and classifies each into a SIGNAL TYPE with a
default TREATMENT (actionable|fyi|closure|noise). Every pattern below was
checked against Jasper's own real subject lines/senders before being written
(2026-07-29 backlog profiling pass) - none of this is guessed.

A signal type's PATTERN (what regex/sender matches it) is code, and changes
only with a real code change. Its TREATMENT is data - correctable live via
workgraph_store.get_signal_treatment/set_signal_treatment, without a
deployment, when the default turns out wrong for how Marc actually wants it
handled (e.g. "mark ContractPodAI's obligation-update emails as noise" - a
worker can persist that correction on his say-so). The defaults below are
only what fires until someone corrects them.

Deliberately narrow: matches only the SPECIFIC subject/sender templates
confirmed in real data, not broad keyword guesses. In particular, no rule
matches on a bare "leah" substring - the real LEAH signal only ever appears
as the literal phrase "LEAH | CLM Update", and even that one is an ordinary
internal FYI note from a real person (the process owner), not an automated
system signal - so it deliberately gets NO rule here and falls through to
classify.py's normal cues like any other internal email.

No LLM calls, no IO beyond the one override lookup per match (fail-open to
the hardcoded default if that lookup errors, since a known-good default beats
blocking classification on a settings-table read).
"""
from __future__ import annotations

import re
from typing import Optional

import workgraph_store as ws

# Marc's own name as it appears in Ariba subjects ("MARC LANE approved...",
# "escalated to MARC LANE for approval...") - used only to tell "escalated to
# someone else" (fyi, just tracking) apart from "escalated to Marc himself"
# (actionable - it's now genuinely his move).
OWNER_NAME_UPPER = "MARC LANE"

# Widened 2026-07-30 (enhancement #1, persisting reference numbers as a real
# field): was PR-only, case-sensitive, and only ever checked when an email
# already matched a recognized automated-sender signal below - real data
# investigation found 150 raw_items with a real PR/PO reference in their
# text, but only 117 had this narrower version populated. Also matches real
# confirmed formats this session found: "PR1111865", "PR416079-V33",
# "PO4200703817" - same pattern workgraph_projects.py's grouping veto
# already uses, now the single shared source instead of two copies.
#
# Considered widening 2026-08-06 (Kinaxis grouping investigation) to also
# match "IC-17255" - retracted on Marc's own direct correction: that's a
# "Document Number" field on one specific PDF/DOCX, generic metadata a
# document-generating system (here, Kinaxis's own CLM tool) stamps on
# whatever it exports - not a transaction/deal identifier with PR/PO's
# actual guarantees (stable across revisions, scoped to one real deal).
# Generalizing a reference-ID TYPE from one observed example, without
# knowing whether it's even stable across that same document's own
# revisions, isn't a safe basis for the auto-merge-strength "reference"
# point type this regex feeds. Left as PR/PO only; a real "document
# number" signal, if built, belongs in _matched_data_points' existing
# "document" point type (shared attachment/lineage) instead - see that
# function's own docstring - not folded into this reference-ID pattern.
REFERENCE_ID_RE = re.compile(r"\b(?:PR|PO)\d{4,}(?:-V\d+)?\b", re.I)

# Task #36: an inconspicuous reference tag Jasper itself appends to outbound
# drafts/compose subjects/bodies (see outlook_actions.py's draft_reply/
# draft_forward, and the Detail Panel's stakeholder mailto: compose) - "Ref:
# JW-<issue-id>", a plain, low-key text token chosen specifically because it
# survives any mail client, signature block, or corporate mail-security
# rewrite, unlike a hidden header/HTML comment a gateway could strip. When
# this comes back on an INBOUND reply/forward, it's a much stronger signal
# than a shared PR/PO number (which only proves "same transaction") - it
# names the exact issue directly, because Jasper's own tooling put it there.
# Case-insensitive on "JW" but the captured id itself is returned exactly as
# found - real issue ids (workgraph_store.next_issue_id) are always
# lowercase ("marc-308"), so a same-case echo-back is the overwhelmingly
# common case; validation that the id still resolves to a real issue happens
# at link time (workgraph_classify.cluster_and_link), never here.
JASPER_REF_RE = re.compile(r"\bRef:\s*JW-([\w-]+)", re.I)


def jasper_ref_issue_id(text: Optional[str]) -> Optional[str]:
    """Extracts the raw candidate issue id from a Jasper reference tag, or
    None if the text has none. Does NOT check the id actually exists -
    that's a link-time concern (a stale tag quoted from an old, since-
    deleted issue should fall through to the normal matching, not error)."""
    if not text:
        return None
    m = JASPER_REF_RE.search(text)
    return m.group(1) if m else None

# 2026-07-31 (meeting-grouping/related-project identity pass): the version
# suffix above (-V33 etc.) is real and worth keeping for display, but every
# matching function in workgraph_projects.py used to compare the FULL
# versioned string - so "PR416079-V32" and "PR416079-V33" were treated as
# two entirely unrelated identities, and worse, as ACTIVELY CONTRADICTING
# evidence by the disjoint-reference veto (real production pair confirmed:
# PR1140347-V2/V3). This strips the version suffix for MATCHING only - the
# full string (raw_items.pr_number) is untouched and still shown as the
# reference-ID chip.
_VERSION_SUFFIX_RE = re.compile(r"-V\d+$", re.I)


def reference_base(full: Optional[str]) -> Optional[str]:
    """Version-stripped identity for matching (e.g. "PR416079-V33" ->
    "PR416079"). None/"" in, None out - never fabricates an identity."""
    if not full:
        return full
    return _VERSION_SUFFIX_RE.sub("", full.upper())


# 2026-07-31 (meeting-grouping/related-project identity pass): confirmed
# against real captured Graph calendar payloads - a personal/solo block
# (HOLD, Focus Time, School Drop off, School Pick up, a self-scheduled
# "Lane - OOO") always has the organizer as the ONLY real participant (or
# none at all - never a meeting with anyone else). An out-of-office
# announcement can instead be broadcast to a large distribution list (real
# example: "Dima OOO Paternity Leave" sent to 34 recipients including
# Marc) - attendee-count alone would never catch that one, so it needs its
# own title match. Marc's own call: for this system's purposes, neither
# shape is a project - they should never become a trackable Issue.
OOO_SUBJECT_RE = re.compile(r"\b(?:ooo|out[\s-]of[\s-]office|paternity leave|maternity leave|vacation|pto)\b", re.I)


def is_personal_calendar_block(*, organizer: Optional[str], participants: Optional[list]) -> bool:
    """True when the organizer is the ONLY real participant (or there are
    none) - a solo calendar hold, never a meeting with anyone else. False
    (never a guess) when there's no organizer to compare against."""
    if not organizer:
        return False
    others = {p.strip().lower() for p in (participants or []) if p and p.strip().lower() != organizer.strip().lower()}
    return not others


def is_ooo_subject(subject: Optional[str]) -> bool:
    """True for a real out-of-office/leave announcement, regardless of
    attendee count - see OOO_SUBJECT_RE's own comment for the real example
    (a large-distribution-list OOO notice) is_personal_calendar_block alone
    would never catch."""
    return bool(subject) and bool(OOO_SUBJECT_RE.search(subject))


# (signal_type, sender-substring-or-None, subject regex, default treatment).
# Order matters - first match wins; within one sender family, the most
# specific/least-ambiguous pattern comes first (e.g. "fully approved" before
# the looser "X approved the Requisition").
_RULES: list[tuple[str, Optional[str], "re.Pattern[str]", str]] = [
    ("ariba_wo_expiration", "ariba.com",
     re.compile(r"^Buy@Lilly:\s*Pending expiration", re.I), "noise"),
    ("ariba_pr_approval_needed", "ariba.com",
     re.compile(r"^Action [Rr]equired:\s*Approve", re.I), "actionable"),
    ("ariba_pr_fully_approved", "ariba.com",
     re.compile(r"^Notification:\s*(?:The\s+)?Requisition has been fully approved", re.I), "closure"),
    ("ariba_pr_watcher", "ariba.com",
     re.compile(r"^Notification:\s*Watch the Requisition", re.I), "fyi"),
    ("ariba_pr_approver_added", "ariba.com",
     re.compile(r"^Notification:.*\badded\b.*\bas an approver\b", re.I), "fyi"),
    ("ariba_pr_escalated", "ariba.com",
     re.compile(r"^Notification:\s*Requisition has been escalated to (.+?) for approval", re.I), "fyi"),
    ("ariba_pr_partial_approval", "ariba.com",
     re.compile(r"^Notification:\s*.+\bapproved the Requisition\b", re.I), "fyi"),
    ("ariba_role_changed_generic", "ariba.com",
     re.compile(r"^Notification:\s*Your role in .* has been changed", re.I), "fyi"),

    ("signature_requested", "adobesign.com",
     re.compile(r"^Signature requested on\b", re.I), "actionable"),
    ("signature_signed_by_me", "adobesign.com",
     re.compile(r"^You signed:", re.I), "fyi"),
    ("signature_fully_executed", "adobesign.com",
     re.compile(r"is Signed and Filed!\s*$|^Completed:", re.I), "closure"),
    ("signature_cc_notice", "adobesign.com",
     re.compile(r"has copied you on.*Contract Management System", re.I), "fyi"),

    ("signature_completed_docusign", "docusign.net",
     re.compile(r"^Completed:|^Here is your signed document:", re.I), "closure"),
    ("signature_requested_docusign", "docusign.net",
     re.compile(r"for Signature\s*$", re.I), "actionable"),

    ("contractpodai_obligations_update", "contractpodai.com",
     re.compile(r"Important Dates/Key Obligations updates", re.I), "fyi"),
    ("contractpodai_contract_request_submitted", "contractpodai.com",
     re.compile(r"^\[EXTERNAL\]\s*Contract Request Submitted", re.I), "fyi"),
    # Task #265 (2026-08-07): two more real ContractPodAI templates found
    # unclassified (signal_type NULL) while tracing the discovery
    # mechanism's proposals back to their real source system. Both carry a
    # real Request ID (extract_contractpodai_request_fields above) and
    # genuinely need Marc's action, unlike the fyi-only rules above.
    ("contractpodai_review_requested", "contractpodai.com",
     re.compile(r"Lilly Contracting Requests Your Review", re.I), "actionable"),
    ("contractpodai_reassignment_requested", "contractpodai.com",
     re.compile(r"^\[EXTERNAL\]\s*Request Reassignment of Assignee", re.I), "actionable"),

    ("intake_new_project_assigned", None,
     re.compile(r"Intake PowerApp - New Project Submitted", re.I), "fyi"),

    ("concur_expense_reminder", "concursolutions.com",
     re.compile(r"^Action Required:\s*Unapplied credit card transactions", re.I), "actionable"),
]


# Canonical definition (fixed 2026-07-29: was pasted verbatim in workgraph_classify.py,
# workgraph_lessons.py, workgraph_projects.py, AND workgraph_parties.py - 4 independent
# copies of the same literal that had to be kept in sync by hand). Every other module
# that needs this now imports it from here instead of redefining it.
_SYSTEM_SENDER = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|automated|system|admin)@", re.I)

# Known automated-system DOMAINS (Ariba, Adobe Sign, DocuSign, ContractPodAI,
# Concur) whose local part does NOT always match _SYSTEM_SENDER above -
# "adobesign@adobesign.com" and "EmailReminderService@concursolutions.com"
# both slip past the local-part-only guard. Moved here from workgraph_
# parties.py (2026-08-02, task #53 investigation) so every module that needs
# to recognize an automated sender - not just party/company naming - can use
# ONE combined check. Real live bug this fixes: workgraph_projects.py's
# grouping-relevant party-exclusion checks only ever tested _SYSTEM_SENDER,
# never this domain list, so adobesign@adobesign.com kept being treated as a
# real shared-party GROUPING signal even though the parties table itself
# already knew better - proj-012 ended up with 15 unrelated issues wrongly
# merged purely because every Adobe Sign envelope notification shares this
# one address.
_MACHINE_SIGNAL_DOMAINS = ["ansmtp.ariba.com", "ariba.com", "adobesign.com",
                           "docusign.net", "contractpodai.com", "concursolutions.com",
                           "alerts.ondemand.com"]
# alerts.ondemand.com added task #169/#170 (2026-08-04, Marc's direct report):
# SAP's own bulk alert feed ("SAP CloudSupport Alerts <sapcloudsupport@
# alerts.ondemand.com>") is a DIFFERENT domain than plain sap.com, so it was
# never covered here - a real @sap.com address belonging to an actual person
# (an account rep, a real contact) stays a genuine party/company signal;
# only this specific bulk-alert domain is excluded.


def _is_machine_signal_domain(email: str) -> bool:
    if "ironclad" in (email or "").lower():
        return True
    return any(domain_matches(email, d) for d in _MACHINE_SIGNAL_DOMAINS)


def is_automated_sender(email: str) -> bool:
    """The one combined "is this a no-reply/automated system sender, not a
    real person" check - _SYSTEM_SENDER (local-part pattern) OR a known
    machine-signal domain (whose local part varies). Use this everywhere a
    sender needs to be excluded from party/company/grouping signals - never
    _SYSTEM_SENDER alone, which misses adobesign@/concursolutions@-shaped
    addresses entirely."""
    email = email or ""
    return bool(_SYSTEM_SENDER.match(email)) or _is_machine_signal_domain(email)


_ARIBA_REQUISITION_FIELDS_RE = re.compile(
    r"approve the requisition that\s+(?P<requester>.+?)\s+submitted\s*[-–—]+\s*"
    r"(?:(?P<pr_number>PR\d+(?:-V\d+)?)\s*[-–—]+\s*)?"
    r"(?P<descriptor>.+?)\s*\(\$\s*(?P<amount>[\d,]+(?:\.\d+)?)\s*(?:usd)?\)",
    re.I,
)


def extract_ariba_requisition_fields(subject: str) -> Optional[dict]:
    """Real fields out of an Ariba requisition-approval subject - task
    #169/#170 (2026-08-04, Marc's direct design ask): the scored grouping
    model's existing 'company'/'party' signals can't discriminate between
    two DIFFERENT Ariba requisitions from the same automated sender at all
    (that address is excluded from party/company matching entirely, by
    design - see is_automated_sender's own callers), so two genuinely
    unrelated PRs and two PRs that are really the same underlying deal
    (a version bump, a re-submission) looked identical to the grouping
    signature. Confirmed real format from live subjects: 'Action required:
    Approve the Requisition that THOMAS TURNER submitted  - PR1193376 -
    Workday HCM SaaS ($53,702,143.00 USD)' - requester, PR#, the descriptor
    naming what's being purchased (often the supplier/product name), and
    the dollar amount are all real, matchable content, not just boilerplate.
    Returns None (never guesses) when the subject doesn't match this exact
    shape - most subjects legitimately won't."""
    m = _ARIBA_REQUISITION_FIELDS_RE.search(subject or "")
    if not m:
        return None
    try:
        amount = float(m.group("amount").replace(",", ""))
    except ValueError:
        amount = None
    return {
        "requester": m.group("requester").strip(),
        "pr_number": (m.group("pr_number") or "").upper() or None,
        "descriptor": m.group("descriptor").strip(" -–—"),
        "amount": amount,
    }


# --- ContractPodAI structured-field extraction (task #265, 2026-08-07) -----
# Same rationale as extract_ariba_requisition_fields above: the discovery
# mechanism's catch-up sweep surfaced 5 real labeled fields (Request ID,
# Sourcing Lead, Functional Area, Supplier Name, "what do you want the S2P
# team to do"), and checking their real backing raw_items found every one
# traces to the same sender (no-reply@contractpodai.com). Marc's own direct
# correction: a field discovered from one specific system's own notification
# template belongs in a table SCOPED to that system, keyed by that system's
# own reference id - not folded into generic personal vocabulary next to an
# unrelated system's field that happens to share a label like "Request ID".
# Confirmed against THREE real, distinct ContractPodAI templates while
# investigating this (not just the one the discovery sweep happened to
# sample): "Contract Request Submitted" (the full intake form - Sourcing
# Lead/Functional Area/Supplier Name/Priority/S2P action + Request ID),
# "Lilly Contracting Requests Your Review" (a short review-request note -
# reviewer/requester/agreement title + Request ID), and "Request
# Reassignment of Assignee - <Supplier> - Master Agreement - <RequestID>"
# (Request ID literally in the subject line + Primary/Additional Assignee).
# Every field below is independently optional (re.search per field, never
# one all-or-nothing template match) since a real body might legitimately
# carry only a subset - the one thing every real template shares is a real
# Request ID with a stable cloud22.contractpod.com permalink.

# `[ \t]*` (never `\s*`) between a label and its value below - confirmed
# real bug (2026-08-07, caught in this build's own backfill sanity check):
# `\s*` matches a newline too, so a genuinely EMPTY field ("What is the
# Priority?: " with nothing after it, a real, observed case) let the
# match skip clean over the blank line and grab the START OF THE NEXT
# LABELED FIELD instead ("Request ID: 90996 <url>" ended up stored as a
# priority). Horizontal-whitespace-only plus `.*` (not `.+`) makes an
# empty field capture an empty string - correctly normalized to None
# below - rather than reaching past its own line for content that isn't
# this field's value at all.
_CPAI_REQUEST_ID_RE = re.compile(r"Request ID[ \t]*:?[ \t]*(\d+)", re.I)
_CPAI_URL_RE = re.compile(r"(https?://[^\s>]*contract-snapshot/(\d+)/redirect[^\s>]*)", re.I)
_CPAI_REASSIGN_SUBJECT_RE = re.compile(
    r"Request Reassignment of Assignee\s*-\s*(?P<supplier>.+?)\s*-\s*Master Agreement\s*-\s*(?P<request_id>\d+)",
    re.I,
)
_CPAI_REVIEW_REQUEST_RE = re.compile(
    r"Hi\s+(?P<reviewer>[A-Za-z][\w' -]*?)\s*-\s*(?P<requester>.+?)\s*\([^)]+@[^)]+\)\s+is requesting your "
    r"review of the following agreement:[ \t]*(?P<agreement>.+?)\.",
    re.I,
)
_CPAI_SOURCING_LEAD_RE = re.compile(r"Sourcing Lead:[ \t]*(.*)")
_CPAI_FUNCTIONAL_AREA_RE = re.compile(r"Functional Area:[ \t]*(.*)")
_CPAI_S2P_ACTION_RE = re.compile(r"What do you want the S2P team to do:[ \t]*(.*)")
_CPAI_SUPPLIER_NAME_RE = re.compile(r"Supplier Name:[ \t]*(.*)")
_CPAI_PRIORITY_RE = re.compile(r"What is the Priority\?:[ \t]*(.*)")
_CPAI_PRIMARY_ASSIGNEE_RE = re.compile(r"Primary Assignee:[ \t]*(.*)")
_CPAI_ADDITIONAL_ASSIGNEES_RE = re.compile(r"Additional Assignees:[ \t]*(.*)")


def extract_contractpodai_request_fields(subject: str, body: str) -> Optional[dict]:
    """Real fields out of ANY of ContractPodAI's own notification templates -
    see the module comment just above for how this was confirmed (real
    sender, three distinct real templates, every field independently
    optional). Returns None only when NO request id can be found anywhere
    (the labeled field, its URL, or the reassignment subject's own trailing
    number) - with no id there's no real key to store a row under."""
    text = f"{subject or ''}\n{body or ''}"

    reassign_m = _CPAI_REASSIGN_SUBJECT_RE.search(subject or "")
    request_id = None
    m = _CPAI_REQUEST_ID_RE.search(text)
    if m:
        request_id = m.group(1)
    if request_id is None:
        url_m = _CPAI_URL_RE.search(text)
        if url_m:
            request_id = url_m.group(2)
    if request_id is None and reassign_m:
        request_id = reassign_m.group("request_id")
    if request_id is None:
        return None

    url_m = _CPAI_URL_RE.search(text)
    review_m = _CPAI_REVIEW_REQUEST_RE.search(text)

    def _field(pattern):
        fm = pattern.search(text)
        if not fm:
            return None
        value = fm.group(1).strip()
        return value or None  # a real, observed case: the field is present but genuinely left blank

    return {
        "request_id": request_id,
        "contractpod_url": url_m.group(1) if url_m else None,
        "sourcing_lead": _field(_CPAI_SOURCING_LEAD_RE),
        "functional_area": _field(_CPAI_FUNCTIONAL_AREA_RE),
        "s2p_action": _field(_CPAI_S2P_ACTION_RE),
        "supplier_name": _field(_CPAI_SUPPLIER_NAME_RE)
                          or (reassign_m.group("supplier").strip() if reassign_m else None),
        "priority": _field(_CPAI_PRIORITY_RE),
        "primary_assignee": _field(_CPAI_PRIMARY_ASSIGNEE_RE),
        "additional_assignees": _field(_CPAI_ADDITIONAL_ASSIGNEES_RE),
        "reviewer": review_m.group("reviewer").strip() if review_m else None,
        "requester": review_m.group("requester").strip() if review_m else None,
        "agreement_title": review_m.group("agreement").strip() if review_m else None,
    }


# Generalized 2026-08-05 (Marc's direct correction, same day this shipped
# as extract_ariba_supplier_field - "this has to be designed to work for
# everyone... you telling me that you cannot easily identify system email
# addresses and apply the same process to all of them?"). He's right: the
# SENDER side was already fully generic (is_automated_sender, above) - it's
# the EXTRACTION side that was hand-tuned to one vendor's exact template.
# Confirmed against TWO independently-built real systems' actual bodies
# (not guessed): Ariba's own line-item table literally says "Supplier " (no
# colon, HTML table cells flattened to one space by text_extract.
# _html_to_text - PR854779-V4, Conversational AI), and ContractPodAI's own
# contract-request notification literally says "Supplier Name: Fullstory,
# Inc" (a colon-delimited paragraph, not a table at all). Different
# label wording, different body layout - same underlying concept, and both
# happen to use the word "Supplier" as part of their real label. One
# shared label vocabulary (Supplier/Vendor/Counterparty/Company Name/
# Client) plus one GENERIC terminator - stop at the next colon-delimited
# label, whatever it's actually called ("What is the Priority?:", "Request
# ID:") - covers any future colon-labeled system without a single new line
# of code. Ariba's specific table shape has no colons anywhere at all
# (labels and values are just adjacent flattened cells), so it keeps its
# own small, explicitly-named fallback terminator list - a real, confirmed
# structural exception, not vendor favoritism.
_PARTY_FIELD_LABEL_RE = r"(?:Supplier(?:\s+Name)?|Vendor(?:\s+Name)?|Counterparty|Company\s+Name|Client(?:\s+Name)?)"

_ARIBA_TABLE_NEXT_FIELD_RE = "|".join([
    "Qty", "Unit", "Price", "Amount", "Account Assignment", "Deliver To",
    "Max Amount", "Expected Amount", "Service Start Date", "Service End Date",
    "GL Account", "Cost Center", "Description",
])

# Fixed 2026-08-05 (real bug found in the very first live test against the
# ContractPodAI shape): a single "stop at the next colon-labeled field"
# terminator over-matched - text_extract._html_to_text destroys the
# original bold-label/plain-value HTML distinction, flattening
# "...Fullstory, Inc</p><p><strong>What is the Priority?: ..." down to one
# indistinguishable run of spaces, so "Inc What is the Priority?" itself
# looks exactly like "the next label" and swallowed "Inc" out of the real
# value. Two real value shapes instead, tried in this preference order:
#   1. word + a real corporate suffix (Inc/LLC/Corp/...) - "AUTHENTICX
#      INC", "Fullstory, Inc" - stops right after the suffix, never
#      greedily extends past it even when more capitalized words follow
#      with no reliable separator.
#   2. up to 4 generic capitalized words, joined by spaces/tabs only
#      (NEVER across a real newline - a genuine plain-text field boundary
#      always wins when one exists), each guarded by a negative lookahead
#      against Ariba's own known no-colon next-field words - covers a
#      value with no corporate suffix at all ("Acme Vendor Co", a bare
#      "Workday" in an Ariba table with no suffix following).
_CORP_SUFFIX_WORD = r"(?:Inc|Incorporated|LLC|L\.L\.C|Ltd|Limited|Corp|Corporation|Co)\.?,?"
_VALUE_WORD = r"[A-Z][A-Za-z0-9&.'/-]*,?"

_LABELED_PARTY_FIELD_RE = re.compile(
    r"\b" + _PARTY_FIELD_LABEL_RE + r"\b\s*:?\s*"
    r"(?P<value>"
    r"(?:" + _VALUE_WORD + r"[ \t]+(?i:" + _CORP_SUFFIX_WORD + r")\b)"
    r"|(?:" + _VALUE_WORD +
    r"(?:[ \t]+(?!(?:" + _ARIBA_TABLE_NEXT_FIELD_RE + r")\b)" + _VALUE_WORD + r"){0,3})"
    r")"
)

# Values that must never surface as "the real party" - Marc's own words:
# "a PR request that comes from Ariba for Authenticx, the supplier needs to
# be identified as authenticx and not ariba/sap." This is a defensive
# floor, not the primary guard (a real labeled field names the actual
# counterparty, never the transport system itself); it only fires if a
# malformed/atypical body ever put one of these literal names there.
# Reuses _MACHINE_SIGNAL_DOMAINS' own real systems, not a separate list -
# one place that knows "these are transports, not parties."
_NON_PARTY_NAMES = {"ariba", "sap", "sap ariba", "sap ariba buying", "adobe sign",
                    "docusign", "contractpodai", "concur"}

_COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co)\.?\s*$", re.I)


def normalize_company_name(name: Optional[str]) -> str:
    """Lowercases and strips a trailing corporate suffix (INC/LLC/CORP/...)
    so a party's tracked company name ("Authenticx") and a system's own
    formal field value ("AUTHENTICX INC") compare equal as the same real
    vendor - see workgraph_projects._matched_data_points' "supplier"
    point. ""/None in, "" out - never fabricates a name to compare
    against."""
    if not name:
        return ""
    return _COMPANY_SUFFIX_RE.sub("", name.lower().strip()).strip()


_RELATIONSHIP_KEYWORDS = (
    "subcontract", "sub-contract", "subcontractor", "flow-down", "flowdown",
    "prime contract", "teaming agreement", "change order under", "work order under",
)
_CROSS_MENTION_WINDOW_CHARS = 200


def cross_mention_match(text: str, known_companies: set) -> Optional[tuple]:
    """Task #335 (per #324's design, docs/design/CANDIDATE_DETECTION_
    BROADENING.md): the one new deterministic point type that closes the
    real, confirmed gap a bare shared-company match (the "supplier" point
    in workgraph_projects._matched_data_points) can't - a prime/
    subcontractor pair with two DIFFERENT company names only ever earns
    ONE structured point today (see that design doc's own probe: a real
    Scriptly PV1 / Scriptly-Sodalis bridge / direct Sodalis MSA case never
    cleared the 2-point gate). This corroborates a weak single-company
    signal with something that says "and this text is explicitly
    describing a relationship," rather than a bare coincidental mention.

    Deliberately narrow and inspectable, never a score: fires only when
    `text` contains, as a literal case-insensitive substring, a company
    name from `known_companies` (the OTHER side's already-normalized
    supplier vocabulary - see normalize_company_name, whose lowercase-
    plus-suffix-strip form is exactly what this searches for) within
    `_CROSS_MENTION_WINDOW_CHARS` characters of one of a short, curated
    relationship-language keyword list. Returns (company, keyword) - the
    literal matched pair, so the caller can build an auditable
    matched_signals string like "cross_mention:Scriptly (subcontract)" -
    or None. Never invents a company name; only ever checks names the
    caller already extracted through the normal party/company pipeline."""
    if not text or not known_companies:
        return None
    lowered = text.lower()
    for company in known_companies:
        if not company or len(company) < 3:
            continue  # too short/generic a normalized name to search for reliably
        idx = lowered.find(company)
        while idx != -1:
            window = lowered[max(0, idx - _CROSS_MENTION_WINDOW_CHARS):
                              idx + len(company) + _CROSS_MENTION_WINDOW_CHARS]
            for keyword in _RELATIONSHIP_KEYWORDS:
                if keyword in window:
                    return company, keyword
            idx = lowered.find(company, idx + 1)
    return None


def extract_labeled_party_field(body_text: str) -> Optional[str]:
    """The real counterparty name out of ANY automated system's body, read
    from a labeled field (Supplier/Vendor/Counterparty/Company Name/
    Client) - see this function's own comment for the two independently-
    confirmed real shapes (Ariba's flattened table, ContractPodAI's
    colon-delimited paragraph) this generalizes across. Works for any
    other system that labels its real party the same way, with no new
    code required - only a body layout with NEITHER a colon-labeled field
    NOR Ariba's specific table shape would ever return None here despite
    genuinely containing this information (an honest gap, not a silent
    guess). Returns None when the text doesn't contain a matching field,
    or when the matched value is itself one of the automated systems'
    own names (_NON_PARTY_NAMES) rather than a real counterparty."""
    m = _LABELED_PARTY_FIELD_RE.search(body_text or "")
    if not m:
        return None
    value = m.group("value").strip(" -–—")
    if not value or normalize_company_name(value) in _NON_PARTY_NAMES:
        return None
    return value


def _escalation_target_is_owner(subject: str) -> bool:
    m = re.search(r"escalated to (.+?) for approval", subject or "", re.I)
    return bool(m and m.group(1).strip().upper() == OWNER_NAME_UPPER)


def domain_matches(from_actor: str, target_domain: str) -> bool:
    """Real domain-boundary match, not substring containment.

    Confirmed exploitable 2026-07-29: "ariba.com" in "notariba.com" is True
    in Python, and "ariba.com" in "ariba.com.evil-tracker.net" is ALSO True -
    a lookalike/spoofed sender domain got the exact same automated-system
    trust as the real one. Extracts the sender's actual domain (after '@')
    and requires it to equal target_domain or be a genuine SUBDOMAIN of it
    (ends with '.' + target_domain, e.g. "ansmtp.ariba.com" for target
    "ariba.com") - a suffix-of-the-string match is not the same as a
    dot-boundary subdomain match, and this only accepts the latter."""
    if "@" not in (from_actor or ""):
        return False
    sender_domain = from_actor.rsplit("@", 1)[-1].lower()
    target_domain = target_domain.lower()
    return sender_domain == target_domain or sender_domain.endswith("." + target_domain)


# Request signal_type -> the specific real closure signal_type that actually
# confirms it (2026-08-01, real-incident follow-up). signal_type is a fixed
# regex-template identity (which real email this was), never affected by a
# live signal_treatment_override - unlike `treatment`/item_class, which are.
# Latent risk this closes: recompute_issue_state's "done" branch only ever
# looked at item_class, so a future override remapping e.g.
# ariba_pr_approval_needed's treatment away from "actionable" would have
# made every one of Marc's open Ariba approvals look like "nothing to
# track" and silently auto-close - the exact real, wrong outcome already
# found by manual click on marc-014/marc-185, just reachable a second way.
# Checking the STABLE signal_type identity instead of the override-able
# mapping closes that path regardless of what any override says. Only
# request signal_types with a real, confirmed closure counterpart in _RULES
# are listed - concur_expense_reminder has no matching closure template in
# this catalog, so it's deliberately absent rather than made to require an
# email that will never arrive.
REQUEST_TO_CLOSURE_SIGNAL: dict[str, str] = {
    "ariba_pr_approval_needed": "ariba_pr_fully_approved",
    "signature_requested": "signature_fully_executed",
    "signature_requested_docusign": "signature_completed_docusign",
}


def sender_domain(from_actor: Optional[str]) -> Optional[str]:
    """The bare domain after '@', lowercased, or None for an address with no
    '@' (or none at all). Centralizes a pattern that was independently
    duplicated in workgraph_parties.py (x2) and this module's own
    domain_matches() before this - added when workgraph_classify.py's
    subject-match fallback (2026-08-01) needed a 4th copy."""
    if not from_actor or "@" not in from_actor:
        return None
    return from_actor.rsplit("@", 1)[-1].lower()


def known_signal_types() -> list[str]:
    """Every signal_type this module can produce, in rule-list order - used
    by workgraph_aristotle.py's Settings UI to populate rule dropdowns so
    Marc picks from real, confirmed values rather than typing a signal_type
    string freehand (task #51)."""
    return [rule[0] for rule in _RULES]


def classify_signal(*, subject: str, from_actor: str) -> Optional[dict]:
    """Returns {signal_type, treatment, pr_number, pr_number_base} for a
    recognized automated signal email, or None when nothing matches (the
    email is NOT one of these known systems - falls through to classify.py's
    normal cues, never forced). `treatment` is the live override if one has
    been set for this signal_type, else the rule's own hardcoded default.
    `pr_number_base` is the version-stripped identity (see reference_base) -
    used for matching only; `pr_number` stays the full string, for display."""
    subject = subject or ""

    for signal_type, domain, pattern, default_treatment in _RULES:
        if domain and not domain_matches(from_actor, domain):
            continue
        if not pattern.search(subject):
            continue

        treatment = default_treatment
        if signal_type == "ariba_pr_escalated" and _escalation_target_is_owner(subject):
            treatment = "actionable"  # escalated to Marc himself, not fyi about someone else's move
        try:
            treatment = ws.get_signal_treatment(signal_type, treatment)
        except Exception:
            pass  # fail-open to the hardcoded default rather than block classification

        m = REFERENCE_ID_RE.search(subject)
        pr_number = m.group(0).upper() if m else None
        return {"signal_type": signal_type, "treatment": treatment, "pr_number": pr_number,
                "pr_number_base": reference_base(pr_number)}

    return None
