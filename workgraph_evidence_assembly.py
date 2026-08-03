"""
workgraph_evidence_assembly.py — Evidence Assembly (2026-08-03).

docs/design/CONFIDENCE_AND_IDENTITY_REDESIGN.md Section 8.1: a deterministic,
provenance-ranked evidence selector under a token budget - the confidence
spine (workgraph_confidence.py) reused as a RANKING function, not a second
signal system. Same module's signals, second consumer.

The real gap this closes: curator's synthesis routine reads every NEW
evidence row since the last marker (delta-based, already correct - see
SYNTHESIS_ROUTINE.md), but within one delta there's no bound, no relevance
ranking, and no flag for evidence that disagrees. An issue that receives an
unusually large batch (a big attachment thread, a reopened negotiation)
gets all of it, unranked. This selects the most load-bearing subset under a
budget instead.

No LLM calls, deterministic (identical issue + identical evidence state +
identical budget -> identical selection), read-only (no writes).

Known, stated gap (not built here): detecting when two included evidence
rows actually CONTRADICT each other needs semantic comparison this
codebase has no deterministic signal for yet - flagging that is Blueprint
territory (Section 8.2's `contradicts` edge, Phase 3), not something to
fake with a heuristic here. `conflicts` below is always empty in v0 -
present in the return shape so a future upgrade doesn't change callers,
never silently claiming coverage it doesn't have.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws
import workgraph_confidence as confidence

# Rough token estimate - no real tokenizer in this codebase; ~4 chars/token
# is the standard back-of-envelope ratio for English prose. Good enough for
# a BUDGET (relative sizing), not claimed to be exact.
_CHARS_PER_TOKEN = 4

RECENCY_WEIGHT = 0.4
PROVENANCE_WEIGHT = 0.3
RELEVANCE_WEIGHT = 0.3


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // _CHARS_PER_TOKEN)


def assemble_evidence(issue_id: str, *, token_budget: int, now: float | None = None) -> dict:
    """Selects the most load-bearing evidence for `issue_id` under
    `token_budget` (approximate tokens, see module docstring). Ranks every
    evidence row by:
        score = RECENCY_WEIGHT   * freshness(row's own ts)
               + PROVENANCE_WEIGHT * provenance (from the issue's real
                 identity_anchors if backfilled, else the row's own
                 reference presence as a fallback signal)
               + RELEVANCE_WEIGHT  * referential_resolution (1.0 if the
                 row's raw_item carries a real reference or the row is a
                 direct system record with no raw_item at all - e.g. a
                 merge note - 0.0 otherwise)
    then greedily includes rows highest-score-first until the budget is
    spent. Ties break on recency (ts descending), matching list_evidence's
    own existing order.

    Returns {"selected": [...], "excluded_count": N, "conflicts": [],
    "tokens_used": N}. `selected` rows are the real evidence dicts (list_
    evidence's own shape) with one added key, "assembly_score"."""
    now = now if now is not None else time.time()
    rows = ws.list_evidence(issue_id)
    if not rows:
        return {"selected": [], "excluded_count": 0, "conflicts": [], "tokens_used": 0}

    anchors = ws.list_identity_anchors(issue_id=issue_id)
    anchor_strengths = [a["anchor_strength"] for a in anchors] if anchors else None
    issue_has_reference = any(a["anchor_type"] == "reference" for a in anchors)

    scored = []
    for row in rows:
        freshness = confidence.freshness([row["ts"]], now)
        if anchor_strengths:
            provenance = confidence.provenance_reliability_from_anchor_strengths(anchor_strengths)
        else:
            provenance = confidence.provenance_reliability(["reference"] if issue_has_reference else ["category"])
        # A row with no raw_item at all (raw_item_id is NULL) is a direct
        # system record (e.g. a merge note) - not floating/ambiguous, so it
        # gets full referential_resolution. Otherwise this is issue-level
        # (does THIS ISSUE have a real reference anchor at all) - evidence
        # rows don't carry their own per-row reference field to check
        # individually without a second join this module doesn't need yet.
        referential = 1.0 if (row.get("raw_item_id") is None or issue_has_reference) else 0.0
        score = (RECENCY_WEIGHT * freshness + PROVENANCE_WEIGHT * provenance + RELEVANCE_WEIGHT * referential)
        scored.append((round(score, 6), row))

    scored.sort(key=lambda pair: (pair[0], pair[1]["ts"]), reverse=True)

    selected = []
    tokens_used = 0
    for score, row in scored:
        cost = _estimate_tokens(row.get("summary"))
        if selected and tokens_used + cost > token_budget:
            continue  # keep checking lower-ranked-but-cheaper rows rather than stopping outright
        tokens_used += cost
        selected.append({**row, "assembly_score": score})

    excluded_count = len(rows) - len(selected)
    return {"selected": selected, "excluded_count": excluded_count, "conflicts": [], "tokens_used": tokens_used}
