"""
workgraph_repeat_signals.py — Part D of the grouping/NBA redesign (2026-07-30):
surfaces raw_item_extractions' `repeat_signals` field. Curator only writes an
entry here when a new ask genuinely restates one already asked earlier on the
SAME issue (see ingest/SYNTHESIS_ROUTINE.md's own discipline on this - never a
guess, omitted entirely for a normal new ask). Zero new extraction call here,
zero LLM calls - a plain, reflect-only per-issue reader, same shape as
workgraph_key_facts.py/workgraph_asks_decisions.py's per-issue functions.

Deliberately NOT a global rollup card (unlike the earlier asks/decisions/
key-facts work) - Marc explicitly removed the top-of-page rollup cards this
same session as unhelpful; this is issue-detail-panel-only.
"""
from __future__ import annotations

import workgraph_store as ws


def list_repeat_signals_for_issue(issue_id: str) -> list[dict]:
    """Every real repeat/escalation signal curator recorded for this issue's
    own extractions. Each entry: {ask_text, days_since_first_ask, escalated,
    escalation_note}. A malformed entry (not a dict, or missing a real
    ask_text) is skipped rather than guessed at or allowed to crash."""
    extractions_by_issue = ws.list_extractions_for_issues([issue_id])
    out = []
    for extraction in extractions_by_issue.get(issue_id, []):
        signals = (extraction.get("extracted_json") or {}).get("repeat_signals") or []
        if not isinstance(signals, list):
            continue
        for entry in signals:
            if not isinstance(entry, dict):
                continue
            ask_text = entry.get("ask_text")
            if not isinstance(ask_text, str) or not ask_text.strip():
                continue
            days = entry.get("days_since_first_ask")
            out.append({
                "ask_text": ask_text.strip(),
                "days_since_first_ask": days if isinstance(days, (int, float)) else None,
                "escalated": bool(entry.get("escalated")),
                "escalation_note": entry.get("escalation_note") if isinstance(entry.get("escalation_note"), str) else None,
                "extracted_ts": extraction["extracted_ts"],
            })
    out.sort(key=lambda e: e["extracted_ts"], reverse=True)
    return out
