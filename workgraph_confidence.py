"""
workgraph_confidence.py — the confidence/ambiguity spine (ACE-lite), v0.

Deterministic, signal-only, replay-stable: identical inputs -> identical
outputs, every value normalized to [0.0, 1.0], no LLM calls, no embeddings,
no NLI, no wall-clock/RNG read from inside this module (every timestamp is
passed in by the caller). It decides nothing on its own — callers use these
signals to damp/rank/hedge; nothing here writes to the store or raises for a
missing signal, a missing signal is just a lower score, never an error.

Scoped to Jasper's TODAY signal vocabulary per docs/design/
CONFIDENCE_AND_IDENTITY_REDESIGN.md Section 2 — this ships BEFORE the
Blueprint's identity_anchors table exists, using a compatibility shim
(`provenance_reliability`) that keys off today's real match-signal kinds
(reference/party/company/topic/sender/category). When identity_anchors
lands, the shim's caller swaps to a real anchor_strength lookup — same
function signature, better input, no change needed here (Section 2.3).

The two axes stay separate on purpose (ACE-for-Jasper's own correction):
context_accuracy is a floor/multiplier on how much to TRUST what evidence
exists; it is not an ambiguity/polysemy model (that axis — how many
DIFFERENT readings are plausible — is explicitly parked, Section 4.5, until
the arithmetic proves too coarse).
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

DAY = 86400.0
TAU_FRESHNESS_DAYS = 90.0
VOLATILITY_V_MAX = 0.25

# Compatibility shim (Section 2.2c / 2.3): maps today's real match-signal
# kinds onto the same [0,1] strength scale a future identity_anchors.
# anchor_strength lookup will replace this with. An exact/exclusive
# reference (PR/PO, Jasper Ref: tag) is the strongest signal Jasper has
# today; a bare category match is the weakest that still gets a suggestion.
PROVENANCE_BY_MATCH_KIND = {
    "reference": 1.0,
    "jasper_ref_tag": 1.0,
    "party": 0.4,
    "company": 0.4,
    "topic": 0.3,
    "sender": 0.3,
    "category": 0.15,
}
_DEFAULT_PROVENANCE = 0.2

# Real upgrade path (Section 2.3): once identity_anchors exist for an issue,
# provenance_reliability_from_anchor_strengths replaces the match_kind shim
# above with actual anchor_strength data from that table - same [0,1] scale,
# real input, no change needed to context_accuracy's callers beyond passing
# anchor_strengths instead of leaving it unset.
PROVENANCE_BY_ANCHOR_STRENGTH = {
    "exact": 1.0,
    "strong": 0.8,
    "weak": 0.4,
    "negative": 0.0,
}


def context_coverage(present_fields: Iterable[str], required_fields: Iterable[str]) -> float:
    """|present ∩ required| / |required|. Presence, not correctness - this
    only asks whether the minimum required signal exists, not whether it's
    right. 1.0 if the required set is empty (nothing to be missing)."""
    required = set(required_fields)
    if not required:
        return 1.0
    present = set(present_fields) & required
    return round(len(present) / len(required), 6)


def freshness(evidence_ts: list, now: float, tau_days: float = TAU_FRESHNESS_DAYS) -> float:
    """Mean of exp(-age_days/tau) over the given evidence timestamps - decay,
    not a hard cutoff. 1.0 when there's no evidence to judge: an empty scope
    is unmeasured, not stale, and context_coverage already penalizes a
    missing signal - freshness shouldn't double-penalize the same gap."""
    if not evidence_ts:
        return 1.0
    tau = max(tau_days, 1e-9) * DAY
    scores = [math.exp(-max(0.0, now - ts) / tau) for ts in evidence_ts]
    return round(sum(scores) / len(scores), 6)


def provenance_reliability(match_kinds: Iterable[str]) -> float:
    """Mean provenance strength across every matched signal kind (e.g.
    ["reference"] or ["company","topic"]). 1.0 when no kinds are given -
    context_coverage already penalizes the absence of any match; this axis
    only discounts the RELIABILITY of a match that does exist."""
    kinds = list(match_kinds or [])
    if not kinds:
        return 1.0
    return round(sum(PROVENANCE_BY_MATCH_KIND.get(k, _DEFAULT_PROVENANCE) for k in kinds) / len(kinds), 6)


def provenance_reliability_from_anchor_strengths(anchor_strengths: Iterable[str]) -> float:
    """Mean provenance strength across real identity_anchors.anchor_strength
    values (exact/strong/weak/negative) for an issue/pair - the upgraded
    input for provenance_reliability's role once real anchors exist. 1.0
    when no anchors are given - context_coverage already penalizes the
    absence of any anchor at all."""
    strengths = list(anchor_strengths or [])
    if not strengths:
        return 1.0
    return round(sum(PROVENANCE_BY_ANCHOR_STRENGTH.get(s, _DEFAULT_PROVENANCE) for s in strengths) / len(strengths), 6)


def referential_resolution(total_refs: int, unresolved_refs: int) -> float:
    """1.0 - clamp(unresolved/total, 0, 1) - the anchor-resolved-vs-floating
    measure (ACE's Model 4; the numeric form of the Automatic-vs-One-touch
    split). 1.0 when there are no refs at all to judge - same "missing !=
    unreliable" reasoning as freshness above."""
    if total_refs <= 0:
        return 1.0
    ratio = unresolved_refs / total_refs
    return round(1.0 - min(1.0, max(0.0, ratio)), 6)


def context_accuracy(*, present_fields: Iterable[str], required_fields: Iterable[str],
                      evidence_ts: list, now: float, match_kinds: Iterable[str],
                      total_refs: int = 0, unresolved_refs: int = 0,
                      tau_days: float = TAU_FRESHNESS_DAYS,
                      anchor_strengths: Optional[Iterable[str]] = None) -> dict:
    """The composite: context_accuracy = mean(coverage, freshness,
    provenance, referential_resolution). This is a MULTIPLIER/floor on other
    scores (effective_score below), never a parallel axis and never itself
    a decision - low context accuracy dampens everything downstream and can
    never justify unattended action, but it doesn't decide anything here.

    provenance is computed from real identity_anchors.anchor_strength data
    when the caller passes `anchor_strengths` (the Section 2.3 upgrade
    path); otherwise it falls back to the match_kind compatibility shim -
    same call shape either way, no change needed at existing call sites
    that haven't been upgraded yet. Returns {"context_accuracy": float,
    "components": {...}}."""
    components = {
        "coverage": context_coverage(present_fields, required_fields),
        "freshness": freshness(evidence_ts, now, tau_days),
        "provenance": (provenance_reliability_from_anchor_strengths(anchor_strengths)
                        if anchor_strengths is not None else provenance_reliability(match_kinds)),
        "referential_resolution": referential_resolution(total_refs, unresolved_refs),
    }
    accuracy = round(sum(components.values()) / len(components), 6)
    return {"context_accuracy": accuracy, "components": components}


def effective_score(raw_score: float, accuracy: float) -> float:
    """effective_score = raw_score x context_accuracy. The one line that
    turns the spine's signals into something a caller actually damps by."""
    return round(raw_score * accuracy, 6)


def uncertainty_trend(scores: list) -> float:
    """clamp(score_t - score_{t-1}, -1, 1). 0.0 with fewer than 2 scores -
    a trend needs at least two points; one point is not a trend."""
    if len(scores) < 2:
        return 0.0
    delta = scores[-1] - scores[-2]
    return round(max(-1.0, min(1.0, delta)), 6)


def volatility_indicator(scores: list, v_max: float = VOLATILITY_V_MAX) -> float:
    """clamp(stddev(last <=5 scores) / v_max, 0, 1). 0.0 with fewer than 3
    scores - a standard deviation of 2 points is not a meaningful signal."""
    recent = scores[-5:]
    if len(recent) < 3:
        return 0.0
    mean = sum(recent) / len(recent)
    variance = sum((s - mean) ** 2 for s in recent) / len(recent)
    stddev = variance ** 0.5
    return round(max(0.0, min(1.0, stddev / v_max)), 6)
