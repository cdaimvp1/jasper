"""
workgraph_aristotle.py — task #51: a taught, not-inferred prerequisite/gate
check. The idea: some automated signals (a DocuSign/Adobe Sign signature
request, say) shouldn't be treated as ready to act on until another signal
(an Ariba PO fully-approved notification) has been seen for the same
project or supplier. Named "Aristotle" by Marc, pairing with the existing
Socrates Q&A engine.

Deliberately NOT inferred from patterns in the mail. A rule only ever comes
from explicit Settings input (server_lean.py's /api/settings/prerequisite-
rules) - Marc picks trigger_signal_type/requires_signal_type/match_on from
real, confirmed workgraph_signals.py values via a dropdown, never freeform
text. There is no NLU/LLM parsing of a conversational rule statement in this
version - building a parser that guesses at intent from prose would be
exactly the kind of guess this whole codebase's signal-recognition discipline
argues against. A chat-based "just tell Jasper the rule" flow is a real,
reasonable future enhancement, not attempted here.

The check itself is fully deterministic: given an issue's raw_items, look up
any ACTIVE rule whose trigger_signal_type matches one of them, then search
the same project's (or supplier's) other issues for ANY raw_item carrying
the required signal_type. HONEST FRAMING, load-bearing: this can only ever
say "no confirmation seen yet" - never "this hasn't happened". Jasper isn't
connected to Ariba/DocuSign/etc. directly; absence of evidence in its own
ingested mail is not proof the prerequisite was never satisfied, only that
no notification about it has been seen yet.
"""
from __future__ import annotations

from typing import Optional

import workgraph_store as ws


def _issues_to_check(issue_id: str, match_on: str) -> list[str]:
    """Which issues' evidence counts toward satisfying a rule, per its
    match_on. Falls back to just the triggering issue itself if the project/
    supplier can't be resolved (no project assigned, no external party
    resolved yet) - a narrower check rather than skipping it entirely."""
    issue = ws.get_issue(issue_id)
    if not issue:
        return [issue_id]

    if match_on == "project" and issue.get("project_id"):
        found = ws.list_issues_for_project(issue["project_id"])
        ids = [i["id"] for i in found]
        if ids:
            return ids

    if match_on == "supplier":
        parties = ws.list_parties_for_issue(issue_id)
        companies = {p.get("company") for p in parties if p.get("company")}
        issue_ids: set[str] = set()
        for company in companies:
            issue_ids.update(ws.list_issues_for_company(company))
        if issue_ids:
            return list(issue_ids)

    return [issue_id]


def _prerequisite_satisfied(issue_id: str, rule: dict) -> bool:
    for check_issue_id in _issues_to_check(issue_id, rule["match_on"]):
        for item in ws.get_raw_items_for_issue(check_issue_id):
            if item.get("signal_type") == rule["requires_signal_type"]:
                return True
    return False


def _build_warning(rule: dict) -> str:
    what = rule.get("reason") or f'evidence of "{rule["requires_signal_type"]}"'
    return f"No confirmation seen yet of {what} — verify before proceeding"


def check_prerequisites(issue_id: str, raw_items: list[dict]) -> Optional[dict]:
    """Checks every distinct signal_type present in `raw_items` against
    active rules. Returns the FIRST unsatisfied rule's warning (as
    {"warning","rule_id"}), or None if nothing triggers or every triggered
    rule is already satisfied. `raw_items` is the caller's already-fetched
    list (workgraph_nba.py already fetches this for value extraction) so
    this never issues its own duplicate raw_items query."""
    checked_signal_types: set[str] = set()
    for item in raw_items:
        signal_type = item.get("signal_type")
        if not signal_type or signal_type in checked_signal_types:
            continue
        checked_signal_types.add(signal_type)
        rules = ws.get_active_prerequisite_rules_for_trigger(signal_type)
        for rule in rules:
            if not _prerequisite_satisfied(issue_id, rule):
                return {"warning": _build_warning(rule), "rule_id": rule["id"]}
    return None
