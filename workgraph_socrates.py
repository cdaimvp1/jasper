"""
workgraph_socrates.py — Socrates for Jasper: ask a free-text question, get a
grounded, confidence-scored answer, or an honest "nothing cleared the bar"
instead of a guess.

Unifies three signals Jasper already has, cheapest-and-most-authoritative
first, stopping the instant one clears the depth's confidence bar (see
workgraph_socrates_depth.py):

  1. recall             - Total Recall precedent (workgraph_lessons.py).
  2. materialized        - the relevant issue's/project's existing synthesis.
  3. targeted-research    - a narrow keyword search of linked evidence.
  4. broad-research       - the same search widened across issues (deep only).

No LLM call anywhere in this module - every tier is a plain DB read. A tier
that turns up nothing is 'none', never fabricated; a question with no
category/supplier signal at all and no issue context simply can't be keyed,
and that is treated as an honest miss, not an error. Every tier CONSULTED is
logged to socrates_retrieval_log (workgraph_store.py), which is both the
audit trail and the raw material for learned tier-ordering (_learned_order
below) - the more it's used, the better it gets at trying the right tier
first for a given shape of question.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import workgraph_store as ws
import workgraph_lessons
import workgraph_synthesis
import workgraph_socrates_depth

BAND_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}

# Same stopword-drop-and-sort idea as workgraph_socrates_depth's vocabulary is
# borrowed from Theo's own querySignature - carries no routing/search signal.
_STOPWORDS = {
    "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "is", "are",
    "was", "were", "be", "with", "at", "by", "from", "as", "that", "this",
    "it", "what", "how", "when", "which", "who", "we", "our", "i", "do",
    "does", "can", "should", "will", "have", "has", "had", "any", "there",
}


def _tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return sorted(set(t for t in toks if len(t) >= 3 and t not in _STOPWORDS))


def situation_signature(text: str) -> str:
    """Deterministic signature of a question: same shape -> same signature,
    regardless of phrasing/word order. The learned-routing + retrieval-log
    key (mirrors Theo's querySignature)."""
    return "|".join(_tokens(text))


def _no_evidence(detail: str) -> dict:
    return {"band": "none", "needs_review": False, "detail": detail}


def _extract_candidates(text: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort (category, company) spotted in free text, checked against
    what's actually on record (never a fixed/hardcoded vocabulary, so it can't
    drift from real data). Longest-name-first so a company name that contains
    a shorter one isn't shadowed. Either or both can come back None - that is
    a normal, expected 'not enough signal' outcome, not a failure.

    Company matching is case-SENSITIVE for short names (confirmed false
    positives, 2026-07-29: case-insensitive matching made "Sap", "Reply", and
    "H1" - all real companies in the data - match ordinary English usage:
    "this will really sap morale", "please reply", "our H1 2026 priorities").
    A real company mention is virtually always written ALL-CAPS (an acronym,
    "SAP") or Title-Case (the stored form, "Sap"); a common word in ordinary
    prose is virtually always plain lowercase mid-sentence. Not foolproof - a
    sentence-initial "Reply to this..." still capitalizes the common word -
    so a small explicit stoplist covers the one term where even that doesn't
    help ("H1" conventionally stays capitalized whether it means the company
    or "first half of the year"). Longer names keep case-insensitive
    matching - a coincidental collision gets less likely as names get longer."""
    _SHORT_NAME_MAX = 6
    _AMBIGUOUS_EVEN_CAPITALIZED = {"h1"}

    lowered = (text or "").lower()
    category = None
    for cat in sorted(ws.list_known_categories(), key=len, reverse=True):
        if cat and re.search(r"\b" + re.escape(cat.lower()) + r"\b", lowered):
            category = cat
            break
    company = None
    for comp in sorted(ws.list_known_companies(), key=len, reverse=True):
        if not comp:
            continue
        if comp.lower() in _AMBIGUOUS_EVEN_CAPITALIZED:
            continue
        if len(comp) <= _SHORT_NAME_MAX:
            if (re.search(r"\b" + re.escape(comp.upper()) + r"\b", text or "")
                    or re.search(r"\b" + re.escape(comp[:1].upper() + comp[1:].lower()) + r"\b", text or "")):
                company = comp
                break
        elif re.search(r"\b" + re.escape(comp.lower()) + r"\b", lowered):
            company = comp
            break
    return category, company


# ---------------------------------------------------------------------------
# Tiers - each returns (evidence dict, provenance list).
# ---------------------------------------------------------------------------

def _recall_evidence(issue: Optional[dict], category: Optional[str], company: Optional[str]):
    if issue is not None:
        lesson = workgraph_lessons.find_matching_lesson(issue)
        if lesson is None:
            return _no_evidence("no matching precedent for this issue"), []
        band = workgraph_lessons.confidence_band(lesson["trust_score"])
        return (
            {"band": band, "needs_review": False,
             "detail": f"precedent trust {lesson['trust_score']:.2f} ({band}): {lesson['statement']}"},
            [f"recall:{lesson['id']}"],
        )

    key = workgraph_lessons.situation_key(category, company)
    if key is None:
        return _no_evidence("not enough signal to key a precedent lookup (need a category + supplier)"), []

    best = workgraph_lessons.best_lesson_for_key(key)
    if best is None:
        return _no_evidence(f"no confirmed precedent for {key}"), []
    band = workgraph_lessons.confidence_band(best["trust_score"])
    return (
        {"band": band, "needs_review": False,
         "detail": f"precedent trust {best['trust_score']:.2f} ({band}): {best['statement']}"},
        [f"recall:{best['id']}"],
    )


def _materialized_evidence(issue: Optional[dict], company: Optional[str]):
    # (entity_type, entity_id) pairs to check - NOT just issue-level. Fixed
    # 2026-07-29: once an issue is grouped into a Project, synthesis is
    # written under ("project", project_id) instead (see cluster_and_link /
    # SYNTHESIS_ROUTINE.md) - checking only the issue-level row meant this
    # tier was blind to the majority case grouping exists for. Live repro
    # before the fix: a project with fresh, current synthesis containing the
    # exact answer still produced "no current synthesis found."
    entities: list[tuple[str, str]] = []
    if issue is not None:
        entities.append(("issue", issue["id"]))
        if issue.get("project_id"):
            entities.append(("project", issue["project_id"]))
    elif company:
        entities.extend(("issue", iid) for iid in ws.list_issues_for_company(company))
    if not entities:
        return _no_evidence("no issue in scope to check for an existing synthesis"), []

    best = None
    for entity_type, eid in entities:
        synth = ws.get_synthesis(entity_type, eid)
        if synth and synth.get("summary"):
            if best is None or (synth.get("synthesized_at") or 0) > (best.get("synthesized_at") or 0):
                best = synth
    if best is None:
        return _no_evidence("no current synthesis found"), []

    try:
        current_marker = workgraph_synthesis.compute_evidence_marker(best["entity_type"], best["entity_id"])
        stale = current_marker != best.get("synthesized_from_marker")
    except Exception:
        stale = False
    band = "low" if stale else "medium"
    return (
        {"band": band, "needs_review": stale,
         "detail": f"synthesis for {best['entity_type']} {best['entity_id']}{' (stale)' if stale else ''}"},
        [f"materialized:{best['entity_type']}:{best['entity_id']}"],
    )


def _search_evidence(question_tokens: list[str], issue_ids: list[str]) -> list[dict]:
    matched = []
    for iid in issue_ids:
        for row in ws.list_evidence(iid):
            summary_l = (row.get("summary") or "").lower()
            if any(tok in summary_l for tok in question_tokens):
                matched.append(row)
    return matched


def _research_evidence(question_tokens: list[str], issue_ids: list[str], tier_label: str):
    if not issue_ids:
        return _no_evidence(f"no issues in scope for {tier_label}"), []
    if not question_tokens:
        return _no_evidence("question had no searchable terms"), []
    matched = _search_evidence(question_tokens, issue_ids)
    if not matched:
        return _no_evidence("no matching evidence found"), []
    # Keyword overlap alone never earns 'high' - it confirms a mention exists,
    # not that it settles the question. Deliberately capped, not a fabrication.
    band = "medium" if len(matched) >= 2 else "low"
    provenance = [f"{tier_label}:{m['id']}" for m in matched[:5]]
    return (
        {"band": band, "needs_review": False,
         "detail": f"{len(matched)} evidence row(s) matched via {tier_label}"},
        provenance,
    )


# ---------------------------------------------------------------------------
# Learned tier ordering - reorders WITHIN the depth's own tier set; never
# promotes a tier depth didn't already select (e.g. broad-research can't jump
# ahead for a 'lookup' question).
# ---------------------------------------------------------------------------

def _learned_order(signature: str, candidate_tiers: list[str]) -> list[str]:
    stats = {s["tier"]: s for s in ws.socrates_source_outcomes(signature)}
    learned = [t for t in candidate_tiers
               if t in stats and stats[t]["consulted"] > 0 and stats[t]["contributed"] > 0]
    if not learned:
        return candidate_tiers

    def rate(t: str) -> float:
        s = stats[t]
        return s["contributed"] / s["consulted"]

    ranked = sorted(learned, key=lambda t: (-rate(t), -stats[t]["contributed"]))
    rest = [t for t in candidate_tiers if t not in ranked]
    return ranked + rest


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def answer(*, question: str, issue_id: Optional[str] = None, asker: Optional[str] = None,
           explicit_depth: Optional[str] = None, now: Optional[float] = None) -> dict:
    """Answer a free-text question. Never raises for a missing signal - it
    degrades honestly (outcome 'degraded'/'abstained', an empty-evidence
    explanation) rather than guessing. Always logs every tier it consulted."""
    asked_ts = now if now is not None else time.time()
    issue = ws.get_issue(issue_id) if issue_id else None

    depth_plan = workgraph_socrates_depth.classify_depth(
        text=question, issue=issue, explicit_depth=explicit_depth,
    )
    signature = situation_signature(question)
    category, company = (None, None) if issue is not None else _extract_candidates(question)
    q_tokens = _tokens(question)

    tiers = _learned_order(signature, depth_plan["tiers"])

    steps: list[dict] = []
    provenance: list[str] = []
    best_band = "none"
    cleared = False
    stopped_at: Optional[str] = None

    for tier in tiers:
        if tier == "recall":
            ev, prov = _recall_evidence(issue, category, company)
        elif tier == "materialized":
            ev, prov = _materialized_evidence(issue, company)
        elif tier == "targeted-research":
            scope = [issue["id"]] if issue else (ws.list_issues_for_company(company) if company else [])
            ev, prov = _research_evidence(q_tokens, scope, "targeted-research")
        else:  # broad-research - deep depth only
            scope = [i["id"] for i in ws.list_issues(states=["active", "waiting", "blocked"], limit=200)]
            ev, prov = _research_evidence(q_tokens, scope, "broad-research")

        steps.append({"tier": tier, "band": ev["band"], "needs_review": ev["needs_review"], "detail": ev["detail"]})
        if BAND_RANK[ev["band"]] > BAND_RANK[best_band]:
            best_band = ev["band"]
        provenance.extend(prov)

        if not ev["needs_review"] and BAND_RANK[ev["band"]] >= BAND_RANK[depth_plan["stop_band"]]:
            cleared = True
            stopped_at = tier
            break

    if cleared:
        outcome = "answered"
        needs_review = False
        answer_text = f"Grounded evidence found via {stopped_at} (confidence: {best_band}). Confirm before acting."
    elif best_band == "none":
        outcome = "degraded"
        needs_review = True
        # Deliberately NOT an empty string (unlike Theo's raw API return) -
        # this lands directly in a chat bubble, and an empty bubble reads as
        # broken rather than as an honest abstention.
        answer_text = ("I don't have grounded evidence for that yet — nothing in precedent, "
                        "synthesis, or linked evidence turned up a match.")
    else:
        outcome = "abstained"
        needs_review = True
        answer_text = (f"I found some signal (confidence: {best_band}) but not enough to answer "
                        f"confidently — confirm before acting.")

    for step in steps:
        ws.append_socrates_log(
            asked_ts=asked_ts, asker=asker, question=question, signature=signature,
            tier=step["tier"], band=step["band"], contributed=(step["tier"] == stopped_at),
            outcome=outcome,
        )

    return {
        "answer": answer_text,
        "confidence": best_band,
        "needs_review": needs_review,
        "outcome": outcome,
        "depth": depth_plan["depth"],
        "rationale": depth_plan["rationale"],
        "provenance": provenance,
        "signature": signature,
        "steps": steps,
        "generated_ts": asked_ts,
    }
