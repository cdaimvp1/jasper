"""Regression tests for workgraph_confidence.py (confidence spine v0),
per its acceptance criteria in docs/design/CONFIDENCE_AND_IDENTITY_
REDESIGN.md Section 2.4: identical inputs -> identical output; every value
in range; removing an anchor lowers provenance_reliability/referential_
resolution; ageing evidence lowers freshness; no embedding/NLI import."""
from __future__ import annotations

import ast
from pathlib import Path

import workgraph_confidence as conf

DAY = 86400.0


# --- module-level design invariant -----------------------------------------

def test_module_imports_no_embedding_or_nli_dependency():
    """Static check, not a runtime one - even an unused import would
    violate the "no embeddings, no NLI" design rule."""
    src = Path(conf.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = {"numpy", "sentence_transformers", "torch", "transformers", "sklearn", "spacy", "onnxruntime"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & banned)


# --- context_coverage --------------------------------------------------

def test_context_coverage_full_when_all_required_present():
    assert conf.context_coverage({"category", "evidence"}, {"category", "evidence"}) == 1.0


def test_context_coverage_partial():
    assert conf.context_coverage({"category"}, {"category", "evidence"}) == 0.5


def test_context_coverage_empty_required_is_full():
    assert conf.context_coverage(set(), set()) == 1.0


# --- freshness ---------------------------------------------------------

def test_freshness_no_evidence_is_full():
    assert conf.freshness([], now=1000.0) == 1.0


def test_freshness_fresh_evidence_near_one():
    now = 10_000.0
    assert conf.freshness([now - 1.0], now=now) > 0.999


def test_freshness_ageing_evidence_lowers_score():
    now = 1_000_000.0
    fresh = conf.freshness([now - 1 * DAY], now=now)
    stale = conf.freshness([now - 180 * DAY], now=now)
    assert stale < fresh
    assert 0.0 <= stale <= 1.0 <= 1.0000001 and 0.0 <= fresh <= 1.0


def test_freshness_deterministic_for_same_inputs():
    now = 5_000_000.0
    a = conf.freshness([now - 30 * DAY, now - 60 * DAY], now=now)
    b = conf.freshness([now - 30 * DAY, now - 60 * DAY], now=now)
    assert a == b


# --- provenance_reliability ---------------------------------------------

def test_provenance_reliability_no_kinds_is_full():
    assert conf.provenance_reliability([]) == 1.0


def test_provenance_reliability_reference_is_strongest():
    assert conf.provenance_reliability(["reference"]) == 1.0


def test_provenance_reliability_removing_an_anchor_lowers_score():
    """The acceptance criterion, literally: removing the reference anchor
    (going from ["reference","topic"] to just ["topic"]) must lower the
    score, never raise or leave it unchanged."""
    with_anchor = conf.provenance_reliability(["reference", "topic"])
    without_anchor = conf.provenance_reliability(["topic"])
    assert without_anchor < with_anchor


def test_provenance_reliability_unknown_kind_gets_default_not_error():
    assert conf.provenance_reliability(["some_future_kind"]) == conf._DEFAULT_PROVENANCE


# --- referential_resolution ---------------------------------------------

def test_referential_resolution_no_refs_is_full():
    assert conf.referential_resolution(total_refs=0, unresolved_refs=0) == 1.0


def test_referential_resolution_all_resolved_is_full():
    assert conf.referential_resolution(total_refs=1, unresolved_refs=0) == 1.0


def test_referential_resolution_removing_resolution_lowers_score():
    resolved = conf.referential_resolution(total_refs=1, unresolved_refs=0)
    unresolved = conf.referential_resolution(total_refs=1, unresolved_refs=1)
    assert unresolved < resolved
    assert unresolved == 0.0


def test_referential_resolution_clamped_within_range():
    assert conf.referential_resolution(total_refs=1, unresolved_refs=5) == 0.0


# --- context_accuracy composite -----------------------------------------

def test_context_accuracy_all_values_in_range():
    result = conf.context_accuracy(
        present_fields={"category"}, required_fields={"category", "evidence"},
        evidence_ts=[1000.0], now=100_000.0, match_kinds=["topic"],
        total_refs=1, unresolved_refs=1,
    )
    assert 0.0 <= result["context_accuracy"] <= 1.0
    for v in result["components"].values():
        assert 0.0 <= v <= 1.0


def test_context_accuracy_deterministic_for_identical_inputs():
    kwargs = dict(
        present_fields={"category", "evidence"}, required_fields={"category", "evidence"},
        evidence_ts=[500.0, 600.0], now=1_000_000.0, match_kinds=["reference"],
        total_refs=1, unresolved_refs=0,
    )
    a = conf.context_accuracy(**kwargs)
    b = conf.context_accuracy(**kwargs)
    assert a == b


def test_context_accuracy_strong_signal_scores_higher_than_weak():
    now = 1_000_000.0
    strong = conf.context_accuracy(
        present_fields={"category", "evidence"}, required_fields={"category", "evidence"},
        evidence_ts=[now - 1 * DAY], now=now, match_kinds=["reference"],
        total_refs=1, unresolved_refs=0,
    )
    weak = conf.context_accuracy(
        present_fields=set(), required_fields={"category", "evidence"},
        evidence_ts=[now - 300 * DAY], now=now, match_kinds=["category"],
        total_refs=1, unresolved_refs=1,
    )
    assert strong["context_accuracy"] > weak["context_accuracy"]


# --- effective_score ------------------------------------------------------

def test_effective_score_full_accuracy_leaves_raw_score_unchanged():
    assert conf.effective_score(0.8, 1.0) == 0.8


def test_effective_score_dampens_with_lower_accuracy():
    assert conf.effective_score(0.8, 0.5) == 0.4


def test_effective_score_zero_accuracy_zeroes_out():
    assert conf.effective_score(0.8, 0.0) == 0.0


# --- temporal signals ------------------------------------------------------

def test_uncertainty_trend_needs_two_points():
    assert conf.uncertainty_trend([0.5]) == 0.0


def test_uncertainty_trend_positive_when_improving():
    assert conf.uncertainty_trend([0.3, 0.6]) == 0.3


def test_uncertainty_trend_clamped_to_range():
    assert conf.uncertainty_trend([-1.0, 1.0]) == 1.0


def test_volatility_indicator_needs_three_points():
    assert conf.volatility_indicator([0.5, 0.6]) == 0.0


def test_volatility_indicator_zero_for_stable_scores():
    assert conf.volatility_indicator([0.5, 0.5, 0.5, 0.5]) == 0.0


def test_volatility_indicator_nonzero_for_unstable_scores():
    assert conf.volatility_indicator([0.1, 0.9, 0.2, 0.8, 0.1]) > 0.0
    assert conf.volatility_indicator([0.1, 0.9, 0.2, 0.8, 0.1]) <= 1.0


def test_volatility_indicator_only_uses_last_five():
    stable_then_recent = [0.9, 0.1, 0.9, 0.1] + [0.5, 0.5, 0.5, 0.5, 0.5]
    assert conf.volatility_indicator(stable_then_recent) == 0.0
