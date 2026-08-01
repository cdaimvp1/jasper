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
  1. email with an attachment  -> contract/document review
  2. upcoming calendar event (<=14 days out, same window workgraph_nba.py
     uses for due-urgency)      -> draft a pre-read
  3. email/teams whose summary mentions approval/benchmark/sign-off language
                                 -> summarize the thread

Known limitation, named rather than hidden: the locked design's own mockup
examples (e.g. "Build a 1-page deal summary" keyed off Priya specifically
asking about DoA benchmarking) are far more content-specific than any
keyword/regex pass can reliably reproduce — that reads real semantic intent
in the message. This module matches the PROJECT's existing choice everywhere
else (workgraph_nba.py, the value-extraction regex) to prefer a cheap,
explainable, zero-LLM signal over a fancier one, at the cost of the
recommendation sometimes being generic ("Summarize the thread") rather than
as specific as a human — or an LLM — would write. Silence (no recommendation)
is the deliberate fallback when no rule fires, not a bug: the locked design
already treats an empty Progress-column cell as valid ("if any").
"""
from __future__ import annotations

import re
from typing import Optional

import skills_registry

DAY = 86400.0
CALENDAR_LOOKAHEAD_DAYS = 14.0  # matches workgraph_nba.py's own due_urgency window

_APPROVAL_RE = re.compile(
    r"\b(approv\w*|sign[- ]?off|benchmark\w*|DoA\b|redline\w*|review\w* (the|this) (contract|order|agreement))\b",
    re.IGNORECASE,
)


def recommend_for_evidence(ev: dict, has_attachment: bool, now: float) -> Optional[dict]:
    """Returns {"kind","label","rationale"} or None. `ev` is one row shape
    from workgraph_store.list_evidence() (type/summary/ts/raw_item_id/issue_id).
    `has_attachment` — caller resolves this from list_attachments_for_issue()
    matched against ev["raw_item_id"] (attachments are joined at raw_item
    level, not stored on the evidence row itself — see that function's own
    docstring on why)."""
    ev_type = ev.get("type")
    summary = ev.get("summary") or ""

    if ev_type == "email" and has_attachment:
        # 2026-07-31: if a real skill is registered for this action_kind
        # (skills_registry.py - swappable, no domain name hardcoded here),
        # name it explicitly so Marc sees what will actually run, not a
        # generic placeholder. No registered skill -> today's generic
        # behavior, unchanged.
        skill = skills_registry.get_skill_for_action("contract_review")
        if skill:
            return {
                "kind": "contract_review",
                "label": skill["label"],
                "rationale": f"This message has an attachment — {skill['display_name']} runs "
                             f"the real review and returns {skill['produces']}.",
            }
        return {
            "kind": "contract_review",
            "label": "Review the attached document",
            "rationale": "This message has an attachment — contract review compares it "
                         "against the MSA and standard positions and returns a redlined copy.",
        }

    if ev_type == "calendar":
        ts = ev.get("ts")
        if isinstance(ts, (int, float)) and ts > now:
            days_out = (ts - now) / DAY
            if days_out <= CALENDAR_LOOKAHEAD_DAYS:
                days_label = "today" if days_out < 1 else f"{int(round(days_out))}d"
                return {
                    "kind": "prep",
                    "label": "Draft a pre-read",
                    "rationale": f"This meeting is {days_label} out — a pre-read circulated "
                                 "beforehand gives attendees time to review before it happens.",
                }

    if ev_type in ("email", "teams") and _APPROVAL_RE.search(summary):
        return {
            "kind": "summarize",
            "label": "Summarize the thread",
            "rationale": "This message touches on approval, sign-off, or benchmarking — a "
                         "short summary makes the ask reviewable at a glance.",
        }

    return None


def attach_recommendations(evidence: list[dict], attachments: list[dict], now: float) -> list[dict]:
    """Mutates and returns `evidence`: adds a "recommendation" key (dict or
    None) and an "attachment" key (bool) to each row, computed from the
    already-fetched attachments list (avoids a query per evidence row)."""
    raw_item_ids_with_attachments = {
        str(a.get("entity_id")) for a in attachments if a.get("entity_type") == "raw_item"
    }
    for ev in evidence:
        has_attachment = str(ev.get("raw_item_id")) in raw_item_ids_with_attachments
        ev["attachment"] = has_attachment
        ev["recommendation"] = recommend_for_evidence(ev, has_attachment, now)
    return evidence
