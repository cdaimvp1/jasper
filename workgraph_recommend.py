"""
workgraph_recommend.py — pure, deterministic per-evidence recommendation
generator. No LLM calls, no clock reads beyond an explicit `now` passed in.
Modeled directly on workgraph_nba.py's own house style (arithmetic/regex over
model calls) — see that file for the project's standing preference here.

Why this exists: the Jasper redesign's Detail pane pairs each Progress event
with "what the system can do about it" (evidence[].recommendation), but the
real evidence schema (type, summary, raw_item_id, ts — see workgraph_store.py)
never carried that field. The UI already renders it correctly when present
and leaves the cell empty otherwise ("if any", per the locked design) — this
module is what actually populates it, added 2026-07-29.

v1 heuristic, in priority order (first match wins, else no recommendation):
  1. email with an attachment AND invoice-audit language (only when
     audit_invoice is actually registered) -> audit the invoice
  2. email with an attachment AND SOW/scope language (only when
     scope_review is actually registered) -> review the SOW
  3. email with an attachment (any other case) -> contract/document review
  4. upcoming calendar event (<=14 days out, same window workgraph_nba.py
     uses for due-urgency)      -> draft a pre-read
  5. email/teams whose summary mentions benchmark language specifically
     (only when market_rate_benchmark is actually registered) -> benchmark
  6. email/teams whose summary mentions approval/benchmark/sign-off language
                                 -> summarize the thread

2026-08-01: checks 1/2/5 are real skills-registry wiring (task #14) - each
ONLY fires when its action_kind is actually registered (skills_registry.
get_skill_for_action), so an install with nothing registered for them falls
straight through to the same generic behavior as before this change. Never
a new guess: the trigger phrases are lifted directly from each skill's own
SKILL.md "Triggers on" list, not invented keywords.

Known limitation, named rather than hidden: the locked design's own mockup
examples (e.g. "Build a 1-page deal summary" keyed off Priya specifically
asking about DoA benchmarking) are far more content-specific than any
keyword/regex pass can reliably reproduce — that reads real semantic intent
in the message. This module matches the PROJECT's existing choice everywhere
else (workgraph_nba.py, the value-extraction regex) to prefer a cheap,
explainable, zero-LLM signal over a fancier one, at the cost of the
recommendation sometimes being generic ("Summarize the thread") rather than
as specific as a human — or an LLM — would write. Silence (an empty list)
is the deliberate fallback when no rule fires, not a bug: the locked design
already treats an empty Progress-column cell as valid ("if any").

2026-08-01 (task #15): returns a LIST, not a single dict — Marc's correction
("you're also still focusing on only one skill at a time being suggested.
that's not going to be the case most of the time") means a row can genuinely
warrant more than one real recommendation at once (e.g. an attachment with
BOTH invoice-audit and SOW-review language). Within each branch, every
specific skill check runs independently and any/all that match are
collected; the branch's generic fallback (contract_review / summarize) is
only added when NONE of that branch's specific checks matched, so the
generic case still degrades to exactly the old single-item behavior.
"""
from __future__ import annotations

import re

import skills_registry

DAY = 86400.0
CALENDAR_LOOKAHEAD_DAYS = 14.0  # matches workgraph_nba.py's own due_urgency window

_APPROVAL_RE = re.compile(
    r"\b(approv\w*|sign[- ]?off|benchmark\w*|DoA\b|redline\w*|review\w* (the|this) (contract|order|agreement))\b",
    re.IGNORECASE,
)
# Trigger phrases lifted verbatim from each skill's own SKILL.md "Triggers on"
# list (config/skills_registry.json), not invented - see this module's own
# docstring (task #14).
_INVOICE_AUDIT_RE = re.compile(
    r"\b(audit (?:this|the) invoice|invoice (?:vs\.?|versus|against) (?:the )?contract|"
    r"invoice discrepanc\w*|over-?billing|billing error|questioned amount|"
    r"duplicate charge|escalation cap|po vs\.? invoice|invoice.{0,20}mismatch|"
    r"timesheet reconciliation)\b",
    re.IGNORECASE,
)
_SCOPE_SOW_RE = re.compile(
    r"\b(review this sow|is this scope well defined|build a statement of work|fix this sow|"
    r"scope diagnostic|draft a sow|rate card for this sow|acceptance criteria for this sow|"
    r"change control for this sow|statement of work)\b",
    re.IGNORECASE,
)
_MARKET_BENCHMARK_RE = re.compile(
    r"\b(benchmark rates?|market rate for|compare our contracts|internal benchmarking|"
    r"portfolio comparison|rate comparison|portfolio benchmarking)\b",
    re.IGNORECASE,
)


def recommend_for_evidence(ev: dict, has_attachment: bool, now: float) -> list[dict]:
    """Returns a list of {"kind","label","rationale"} dicts, possibly empty.
    `ev` is one row shape from workgraph_store.list_evidence()
    (type/summary/ts/raw_item_id/issue_id). `has_attachment` — caller
    resolves this from list_attachments_for_issue() matched against
    ev["raw_item_id"] (attachments are joined at raw_item level, not stored
    on the evidence row itself — see that function's own docstring on why).

    A row can genuinely warrant more than one recommendation (task #15) — each
    branch below runs its specific checks independently and collects every
    one that matches, falling back to that branch's generic recommendation
    only when none of its specific checks matched."""
    ev_type = ev.get("type")
    summary = ev.get("summary") or ""
    recs: list[dict] = []

    if ev_type == "email" and has_attachment:
        # 2026-08-01 (task #14): more specific skills, checked BEFORE the
        # generic attachment fallback, so a real invoice-audit or SOW ask
        # names the actual skill that does that instead of the generic
        # "review the attached document." Each ONLY fires when actually
        # registered - an unregistered kind falls through, same "no
        # registered skill -> today's generic behavior" rule as
        # contract_review already followed. Both can fire on the same row
        # (task #15) — an attachment can have both invoice-audit AND SOW
        # language.
        if _INVOICE_AUDIT_RE.search(summary):
            skill = skills_registry.get_skill_for_action("audit_invoice")
            if skill:
                recs.append({
                    "kind": "audit_invoice",
                    "label": skill["label"],
                    "rationale": f"This message has an attachment and invoice-audit language — "
                                 f"{skill['display_name']} runs the real audit and returns {skill['produces']}.",
                })
        if _SCOPE_SOW_RE.search(summary):
            skill = skills_registry.get_skill_for_action("scope_review")
            if skill:
                recs.append({
                    "kind": "scope_review",
                    "label": skill["label"],
                    "rationale": f"This message has an attachment and SOW/scope language — "
                                 f"{skill['display_name']} runs the real diagnostic and returns {skill['produces']}.",
                })
        if not recs:
            # 2026-07-31: if a real skill is registered for this action_kind
            # (skills_registry.py - swappable, no domain name hardcoded here),
            # name it explicitly so Marc sees what will actually run, not a
            # generic placeholder. No registered skill -> today's generic
            # behavior, unchanged. Only reached when neither specific check
            # above matched (task #15) — a real invoice/SOW match is always
            # more useful than the generic "review the document."
            skill = skills_registry.get_skill_for_action("contract_review")
            if skill:
                recs.append({
                    "kind": "contract_review",
                    "label": skill["label"],
                    "rationale": f"This message has an attachment — {skill['display_name']} runs "
                                 f"the real review and returns {skill['produces']}.",
                })
            else:
                recs.append({
                    "kind": "contract_review",
                    "label": "Review the attached document",
                    "rationale": "This message has an attachment — contract review compares it "
                                 "against the MSA and standard positions and returns a redlined copy.",
                })
        return recs

    if ev_type == "calendar":
        ts = ev.get("ts")
        if isinstance(ts, (int, float)) and ts > now:
            days_out = (ts - now) / DAY
            if days_out <= CALENDAR_LOOKAHEAD_DAYS:
                days_label = "today" if days_out < 1 else f"{int(round(days_out))}d"
                # 2026-08-01 (task #14): name meeting-prep-brief when it's
                # actually registered - same "exact skill, real output" upgrade
                # already applied to contract_review, just on the existing
                # calendar-window trigger instead of a new one.
                skill = skills_registry.get_skill_for_action("meeting_prep")
                if skill:
                    recs.append({
                        "kind": "meeting_prep",
                        "label": skill["label"],
                        "rationale": f"This meeting is {days_label} out — {skill['display_name']} "
                                     f"reads the invite, the recent thread, and related documents, and returns {skill['produces']}.",
                    })
                else:
                    recs.append({
                        "kind": "prep",
                        "label": "Draft a pre-read",
                        "rationale": f"This meeting is {days_label} out — a pre-read circulated "
                                     "beforehand gives attendees time to review before it happens.",
                    })
        return recs

    if ev_type in ("email", "teams"):
        # 2026-08-01 (task #14): benchmark language specifically, checked
        # before the generic approval/sign-off bucket below, so a real rate
        # question names market-rate-benchmarking instead of just
        # "summarize." Falls through to the generic check (which still
        # matches "benchmark\w*" on its own) when the skill isn't registered.
        market_matched = False
        if _MARKET_BENCHMARK_RE.search(summary):
            skill = skills_registry.get_skill_for_action("market_rate_benchmark")
            if skill:
                recs.append({
                    "kind": "market_rate_benchmark",
                    "label": skill["label"],
                    "rationale": f"This message asks about market rates — {skill['display_name']} "
                                 f"returns {skill['produces']}.",
                })
                market_matched = True
        # 2026-08-01 (task #15): the generic summarize cue is suppressed only
        # when market_rate_benchmark already covered this row — an approval/
        # sign-off match for a DIFFERENT reason (e.g. "please sign off",
        # unrelated to rates) still adds its own, independent recommendation.
        if _APPROVAL_RE.search(summary) and not market_matched:
            recs.append({
                "kind": "summarize",
                "label": "Summarize the thread",
                "rationale": "This message touches on approval, sign-off, or benchmarking — a "
                             "short summary makes the ask reviewable at a glance.",
            })

    return recs


def attach_recommendations(evidence: list[dict], attachments: list[dict], now: float) -> list[dict]:
    """Mutates and returns `evidence`: adds a "recommendations" key (list,
    possibly empty — task #15) and an "attachment" key (bool) to each row,
    computed from the already-fetched attachments list (avoids a query per
    evidence row)."""
    raw_item_ids_with_attachments = {
        str(a.get("entity_id")) for a in attachments if a.get("entity_type") == "raw_item"
    }
    for ev in evidence:
        has_attachment = str(ev.get("raw_item_id")) in raw_item_ids_with_attachments
        ev["attachment"] = has_attachment
        ev["recommendations"] = recommend_for_evidence(ev, has_attachment, now)
    return evidence
