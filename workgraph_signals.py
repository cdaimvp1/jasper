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
REFERENCE_ID_RE = re.compile(r"\b(?:PR|PO)\d{4,}(?:-V\d+)?\b", re.I)

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


def known_signal_types() -> list[str]:
    """Every signal_type this module can produce, in rule-list order - used
    by workgraph_aristotle.py's Settings UI to populate rule dropdowns so
    Marc picks from real, confirmed values rather than typing a signal_type
    string freehand (task #51)."""
    return [rule[0] for rule in _RULES]


def classify_signal(*, subject: str, from_actor: str) -> Optional[dict]:
    """Returns {signal_type, treatment, pr_number} for a recognized automated
    signal email, or None when nothing matches (the email is NOT one of these
    known systems - falls through to classify.py's normal cues, never forced).
    `treatment` is the live override if one has been set for this signal_type,
    else the rule's own hardcoded default."""
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
        return {"signal_type": signal_type, "treatment": treatment, "pr_number": m.group(0).upper() if m else None}

    return None
