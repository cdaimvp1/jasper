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

v2 (task #52): detect_candidate_rules() - a SEPARATE, deterministic
correlation scan over real history, proposing (never auto-activating)
candidate rules. For every pair of signal types actually observed, for every
project (or supplier) where BOTH appear, checks whether the "requires" one
is consistently EARLIER than the "trigger" one - with zero exceptions,
across at least MIN_SAMPLE_GROUPS independent projects/suppliers, since one
coincidental case proves nothing. This still doesn't "read English" or infer
intent - it counts timestamps, the same shape as personal_patterns.py's
co-occurrence counting. Candidates land in pending_prerequisite_suggestions
(shared with task #54's chat-taught suggestions) and only ever become a
real, active prerequisite_rules row once a person confirms it.
"""
from __future__ import annotations

import time
from typing import Optional

import workgraph_store as ws

MIN_SAMPLE_GROUPS = 2  # below this, one coincidental case could look "consistent" by pure luck


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


# Fixed, owned string - never derived from user input - so checking a
# reason string's prefix (workgraph_nba.recompute_all(), task #55, to know
# whether to persist has_unmet_prerequisite without recomputing this whole
# check a second time) is a safe, deliberate coupling point, not fragile
# string-sniffing of arbitrary text.
WARNING_PREFIX = "No confirmation seen yet of "


def _build_warning(rule: dict) -> str:
    what = rule.get("reason") or f'evidence of "{rule["requires_signal_type"]}"'
    return f"{WARNING_PREFIX}{what} — verify before proceeding"


def _group_keys_for_issue(issue_id: str, match_on: str) -> list[str]:
    """Which group key(s) (a project_id, or company name(s)) this issue
    belongs to, for correlation purposes. An issue with no project (for
    match_on='project') or no resolved external party (for 'supplier')
    contributes NO group key - it's excluded from that pass's counting, not
    treated as its own isolated one-issue group (which would let every
    unresolved issue silently satisfy MIN_SAMPLE_GROUPS with "groups" that
    share nothing in common)."""
    if match_on == "project":
        issue = ws.get_issue(issue_id)
        return [issue["project_id"]] if issue and issue.get("project_id") else []
    parties = ws.list_parties_for_issue(issue_id)
    return list({p["company"] for p in parties if p.get("company")})


def _occurrences_by_group(signal_type: str, match_on: str) -> dict[str, list[float]]:
    """group_key -> sorted occurred_ts list, for every raw_item carrying this
    signal_type that resolves to at least one group key (an item with no
    linked issue yet contributes nothing)."""
    out: dict[str, list[float]] = {}
    for item in ws.get_raw_items_by_signal_type(signal_type):
        issue_id = item.get("issue_id")
        if not issue_id:
            continue
        for key in _group_keys_for_issue(issue_id, match_on):
            out.setdefault(key, []).append(item["occurred_ts"])
    for key in out:
        out[key].sort()
    return out


def detect_candidate_rules() -> list[dict]:
    """Every (trigger, requires, match_on) triple where the "requires" signal
    was consistently seen BEFORE the "trigger" signal, with zero exceptions,
    across at least MIN_SAMPLE_GROUPS independent projects/suppliers - and
    that isn't already an active rule or an existing (pending, confirmed, OR
    previously rejected) suggestion. Pure/ungated: the daily gate and the
    actual logging happen in detect_and_log_candidates_daily_if_due()."""
    signal_types = ws.list_distinct_signal_types_in_use()
    if len(signal_types) < 2:
        return []

    existing_triples = {
        (r["trigger_signal_type"], r["requires_signal_type"], r["match_on"])
        for r in ws.list_prerequisite_rules()
    }
    existing_triples |= {
        (s["trigger_signal_type"], s["requires_signal_type"], s["match_on"])
        for s in ws.list_prerequisite_suggestions(status=None)
        if s.get("trigger_signal_type") and s.get("requires_signal_type")
    }

    candidates = []
    for match_on in ("project", "supplier"):
        occ_by_type = {st: _occurrences_by_group(st, match_on) for st in signal_types}
        for trigger in signal_types:
            trigger_groups = occ_by_type[trigger]
            if not trigger_groups:
                continue
            for requires in signal_types:
                if requires == trigger:
                    continue
                if (trigger, requires, match_on) in existing_triples:
                    continue
                requires_groups = occ_by_type[requires]
                shared_groups = [g for g in trigger_groups if g in requires_groups]
                if len(shared_groups) < MIN_SAMPLE_GROUPS:
                    continue
                consistent = sum(
                    1 for g in shared_groups if requires_groups[g][0] < trigger_groups[g][0]
                )
                if consistent == len(shared_groups):
                    candidates.append({
                        "trigger_signal_type": trigger, "requires_signal_type": requires,
                        "match_on": match_on, "observed_count": len(shared_groups),
                    })
    return candidates


def _describe_candidate(candidate: dict) -> tuple[str, str]:
    n = candidate["observed_count"]
    reason = f'evidence of "{candidate["requires_signal_type"]}" for the same {candidate["match_on"]}'
    evidence = (f'Every time "{candidate["trigger_signal_type"]}" appeared, '
                f'"{candidate["requires_signal_type"]}" was already present first — '
                f'consistent across {n} of {n} {candidate["match_on"]}(s) seen so far.')
    return reason, evidence


def detect_and_log_candidates_daily_if_due(now: Optional[float] = None) -> Optional[dict]:
    """Gate for scheduled_refresh.py - same once/day ingest_cursors pattern
    as retention/health_check/personal_learning (source='aristotle_detection').
    Returns None - a real 'did not run' result, not a silent no-op - if
    already run today."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if ws.get_cursor("aristotle_detection", "last_run_date") == today:
        return None
    candidates = detect_candidate_rules()
    logged = 0
    for candidate in candidates:
        reason, evidence = _describe_candidate(candidate)
        ws.create_prerequisite_suggestion(
            origin="detected", trigger_signal_type=candidate["trigger_signal_type"],
            requires_signal_type=candidate["requires_signal_type"], match_on=candidate["match_on"],
            reason=reason, evidence=evidence, raw_explanation=None, proposed_by="system",
        )
        logged += 1
    ws.set_cursor("aristotle_detection", "last_run_date", today)
    return {"candidates_found": len(candidates), "logged": logged}


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
