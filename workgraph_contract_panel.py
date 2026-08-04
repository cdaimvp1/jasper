"""
workgraph_contract_panel.py - the deterministic half of the contract_review
Pass 3 SME panel (task #50, docs/design/CONTRACT_REVIEW_SME_PANEL.md).

The design's actual panel members (13+ domain-scoped sub-agents that read a
contract clause and reason about it) are inherently LLM-executed work - a
worker running the contract_review skill spawns them, per
ingest/CONTRACT_REVIEW_PANEL_ROUTINE.md. This module is the mechanical
scaffolding around that: which SMEs even need a sub-agent for a given
document (so a simple NDA doesn't cost a flat 13x overhead), and how their
independent findings get merged back into one PASS_3_ANALYSIS list without
double-counting a shared Hard Stop or silently dropping one side of a
same-clause disagreement. Zero LLM calls in this file - same "grounded,
never guessed" discipline as workgraph_signals.py's own keyword rules.

Fully data-driven off the live sme-matrix.md, not a hardcoded SME list -
that file is reconciled against a real, separately-owned SharePoint list
that grows over time (19 named SMEs as of 2026-08-04, already more than
the 12 the original design doc counted from an older read) - this module
must keep working unchanged as that list grows or a name changes.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import skills_registry

_SECTION_RE = re.compile(
    r"^### (?P<header>.+?)\n(?P<body>.*?)(?=\n### |\n## |\Z)", re.S | re.M,
)
_EMAIL_RE = re.compile(r"^\s*-\s*\*\*(?:Email|Co-contact.*?)\s*:\*\*\s*(.+)$", re.M)
_TRIGGERS_RE = re.compile(r"^\s*-\s*\*\*Triggers:\*\*\s*(.+)$", re.M)
_THRESHOLD_RE = re.compile(r"^\s*-\s*\*\*Escalation threshold:\*\*\s*(.+)$", re.M)
_EMAIL_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def _sme_matrix_path() -> Optional[Path]:
    """The skill's real, vendored sme-matrix.md - resolved through
    skills_registry rather than a hardcoded path, so this keeps working
    across a version bump (get_skill_for_action always resolves to
    whichever version is currently active) and returns None (an honest
    miss, never a guess) if the skill isn't vendored on this install."""
    entry = skills_registry.get_skill_for_action("contract_review")
    if entry is None:
        return None
    path = entry["skill_dir"] / "references" / "sme-matrix.md"
    return path if path.exists() else None


def load_sme_directory(matrix_text: Optional[str] = None) -> list[dict]:
    """Parses every named SME section out of sme-matrix.md into
    {sme_name, email, triggers (list of lowercased keyword strings),
    escalation_threshold}. An entry with no real Triggers line (e.g. the
    live matrix's own "UNASSIGNED" EEO entry) is skipped entirely - there
    is nothing to pre-filter on, and inventing a keyword would defeat the
    whole point of this being a mechanical, grounded match. matrix_text is
    injectable for tests; production callers omit it and this reads the
    real vendored file."""
    if matrix_text is None:
        path = _sme_matrix_path()
        if path is None:
            return []
        matrix_text = path.read_text(encoding="utf-8")

    smes = []
    for m in _SECTION_RE.finditer(matrix_text):
        header, body = m.group("header").strip(), m.group("body")
        triggers_match = _TRIGGERS_RE.search(body)
        if not triggers_match:
            continue  # honest skip - no real trigger list to match on (e.g. UNASSIGNED)
        triggers = [t.strip().lower() for t in triggers_match.group(1).split(",") if t.strip()]
        email_match = _EMAIL_RE.search(body)
        email = None
        if email_match:
            addr_match = _EMAIL_ADDR_RE.search(email_match.group(1))
            email = addr_match.group(0) if addr_match else email_match.group(1).strip()
        threshold_match = _THRESHOLD_RE.search(body)
        smes.append({
            "sme_name": header,
            "email": email,
            "triggers": triggers,
            "escalation_threshold": threshold_match.group(1).strip() if threshold_match else None,
        })
    return smes


def identify_triggered_smes(document_text: str, sme_directory: Optional[list[dict]] = None) -> list[dict]:
    """Cheap, deterministic pre-filter (design doc's own "two-stage
    scoping," stage 1): which SMEs' trigger keywords actually appear
    anywhere in `document_text`. Whole-word matching (not a bare substring
    search) so a trigger like "audit" doesn't match "auditorium," and case-
    insensitive since contract text and the trigger list are cased
    differently for no meaningful reason. Returns one entry per matched
    SME, carrying which specific keyword(s) hit - a panel member's prompt
    needs this to distinguish a real domain hit from a keyword appearing
    in an unrelated context (design doc's own edge case), even though
    telling the two apart is the sub-agent's job, not this function's."""
    directory = sme_directory if sme_directory is not None else load_sme_directory()
    text_lower = document_text.lower()
    triggered = []
    for sme in directory:
        matched = [t for t in sme["triggers"] if re.search(r"\b" + re.escape(t) + r"\b", text_lower)]
        if matched:
            triggered.append({**sme, "triggers_matched": matched})
    return triggered


def reconcile_panel_findings(findings: list[dict]) -> list[dict]:
    """Mechanical merge of independent panel members' findings into one
    PASS_3_ANALYSIS list (design doc's "Synthesis" section) - never a
    13-way vote, never an attempt to arbitrate a real disagreement between
    two SMEs (that's the existing, human-facing "Multiple SME Escalation
    Handling" protocol's job, sme-matrix.md's own text, not this
    function's).

    Two mechanical rules only:
    - Same `hard_stop_id` (e.g. "HS-1") across findings from different
      members -> keep the first, drop the rest as duplicates of the same
      real Hard Stop (risk-scoring.md: a Hard Stop deduction must never be
      applied more than once for the same real Hard Stop).
    - Same `clause_reference` across findings from different members with
      DIFFERENT `owning_sme` -> keep BOTH (never silently drop one side of
      a real disagreement/overlap), but link them via `related_findings`
      so a consumer can see they're about the same clause.

    Findings with neither a shared hard_stop_id nor a shared
    clause_reference pass through completely unchanged - the common case,
    no reconciliation needed."""
    by_hard_stop: dict[str, dict] = {}
    kept: list[dict] = []
    for f in findings:
        hs_id = f.get("hard_stop_id")
        if hs_id and hs_id in by_hard_stop:
            continue  # duplicate of an already-kept Hard Stop finding
        f = dict(f)
        f.setdefault("related_findings", [])
        if hs_id:
            by_hard_stop[hs_id] = f
        kept.append(f)

    by_clause: dict[str, list[dict]] = {}
    for f in kept:
        clause = f.get("clause_reference")
        if clause:
            by_clause.setdefault(clause, []).append(f)
    for clause, group in by_clause.items():
        if len(group) < 2:
            continue
        for f in group:
            others = [g for g in group if g is not f]
            f["related_findings"] = sorted(set(f["related_findings"]) | {g.get("owning_sme") for g in others if g.get("owning_sme")})

    return kept
