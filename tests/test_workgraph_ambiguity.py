"""Tests for workgraph_ambiguity (tasks #393/#394).

The component functions are pure and take plain row dicts, so they are tested
directly with no DB. measure_project's DB reads are exercised separately
against a real project id only if one exists, so the suite stays hermetic.
"""
from __future__ import annotations

import math

import pytest

import workgraph_ambiguity as amb


def _raw(id_, source, occurred_ts, pr_number=None):
    return {"id": id_, "source": source, "occurred_ts": occurred_ts, "pr_number": pr_number}


# ------------------------------------------------------------------ freshness

def test_freshness_brand_new_evidence_is_one():
    now = 1_700_000_000.0
    c = amb.compute_freshness([_raw("a", "outlook_mail", now)], now)
    assert not c.abstained
    assert c.value == pytest.approx(1.0, abs=1e-6)


def test_freshness_decays_at_the_canonical_tau():
    """At exactly tau days old the value must be exp(-1), not something tuned."""
    now = 1_700_000_000.0
    old = now - amb.FRESHNESS_TAU_DAYS * 86400.0
    c = amb.compute_freshness([_raw("a", "outlook_mail", old)], now)
    assert c.value == pytest.approx(math.exp(-1.0), abs=1e-6)


def test_freshness_abstains_with_no_usable_timestamp():
    c = amb.compute_freshness([_raw("a", "outlook_mail", None)], 1_700_000_000.0)
    assert c.abstained
    assert c.value is None
    assert "occurred_ts" in c.abstained_reason


def test_freshness_ignores_future_dated_rows():
    """Future timestamps are bad data, not fresh evidence - they must not
    inflate the score, and if they are all we have we abstain."""
    now = 1_700_000_000.0
    c = amb.compute_freshness([_raw("a", "outlook_mail", now + 86400.0 * 30)], now)
    assert c.abstained


# --------------------------------------- provenance is CARRIED, not SCORED

def test_no_source_trust_table_exists_anywhere():
    """REGRESSION GUARD. A per-source trust map is an authority model - a
    declared precedence over evidence classes - and Jasper does not arbitrate
    which source wins. An earlier draft shipped one; it was removed. If this
    test fails, someone has reintroduced a scored authority model."""
    for attr in ("DEFAULT_SOURCE_TRUST", "UNKNOWN_SOURCE_TRUST", "SOURCE_TRUST"):
        assert not hasattr(amb, attr), f"{attr} reintroduces a source-authority model"
    assert not hasattr(amb, "compute_provenance_reliability")


def test_provenance_reliability_is_excluded_by_design_not_missing_data():
    c = next(c for c in amb._abstaining_components() if c.name == "provenance_reliability")
    assert c.abstained
    assert "design" in c.abstained_reason.lower()
    assert "authority" in c.abstained_reason.lower()


def test_source_mix_is_carried_as_plain_counts():
    """Provenance travels with the signal so a human can weigh it - as counts,
    with no ranking implied between sources."""
    now = 1_700_000_000.0
    mix = amb.compute_source_mix([
        _raw("a", "outlook_mail", now), _raw("b", "outlook_mail", now),
        _raw("c", "teams_chat", now),
    ])
    assert mix == {"outlook_mail": 2, "teams_chat": 1}


def test_source_mix_never_returns_a_score():
    """Counts only. Any float here would be a weighting in disguise."""
    now = 1_700_000_000.0
    mix = amb.compute_source_mix([_raw("a", "sharepoint", now)])
    assert all(isinstance(v, int) for v in mix.values())


# ------------------------------------------------------------- referential

def test_referential_zero_when_nothing_is_referenced():
    c = amb.compute_referential_ambiguity([_raw("a", "outlook_mail", 1.0)], set())
    assert c.value == 0.0
    assert c.detail["n_referenced"] == 0


def test_referential_all_resolved_is_zero():
    items = [_raw("a", "outlook_mail", 1.0, pr_number="PR123")]
    c = amb.compute_referential_ambiguity(items, {"PR123"})
    assert c.value == 0.0
    assert c.detail["n_unresolved"] == 0


def test_referential_all_unresolved_is_one():
    items = [_raw("a", "outlook_mail", 1.0, pr_number="PR999")]
    c = amb.compute_referential_ambiguity(items, set())
    assert c.value == 1.0
    assert c.detail["unresolved"] == ["PR999"]


def test_referential_is_a_ratio_over_distinct_tokens():
    items = [
        _raw("a", "outlook_mail", 1.0, pr_number="PR1"),
        _raw("b", "outlook_mail", 1.0, pr_number="PR1"),   # duplicate, counts once
        _raw("c", "outlook_mail", 1.0, pr_number="PR2"),
    ]
    c = amb.compute_referential_ambiguity(items, {"PR1"})
    assert c.detail["n_referenced"] == 2
    assert c.value == pytest.approx(0.5)


# -------------------------------------------------------------- abstentions

def test_the_unavailable_components_all_abstain_with_a_reason():
    names = {c.name for c in amb._abstaining_components()}
    assert names == {
        "provenance_reliability", "context_coverage", "contradiction",
        "internal_consistency", "semantic_polysemy", "embedding_dispersion",
        "relevance",
    }
    for c in amb._abstaining_components():
        assert c.abstained
        assert c.value is None
        assert c.abstained_reason


def test_abstention_never_degrades_to_zero():
    """The whole point: an abstaining component must not look like a measured
    0.0, because 0.0 for contradiction reads as 'no contradictions found'."""
    for c in amb._abstaining_components():
        assert c.value is not 0.0  # noqa: F632 - identity check is the intent
        assert c.value is None


# ------------------------------------------------------------- aggregation

def test_aggregate_inverts_goodness_components():
    """freshness is reported as goodness; ambiguity is badness."""
    now = 1_700_000_000.0
    comps = [
        amb.compute_freshness([_raw("a", "outlook_mail", now)], now),                # 1.0 good
        amb.compute_referential_ambiguity([_raw("a", "outlook_mail", now)], set()),  # 0.0 bad
    ]
    inverted = {"freshness"}
    contributions = [
        (1.0 - c.value) if c.name in inverted else c.value for c in comps if not c.abstained
    ]
    # perfectly fresh -> 0 ambiguity; no references -> 0 ambiguity
    assert contributions == pytest.approx([0.0, 0.0])


# ---------------------------------------------------------- gap localization

def test_unresolved_reference_becomes_a_named_gap():
    items = [_raw("a", "outlook_mail", 1.0, pr_number="PR777")]
    comps = [amb.compute_referential_ambiguity(items, set())]
    gaps = amb.localize_gaps(comps, items, claims=[])
    kinds = {g.kind for g in gaps}
    assert "unresolved_reference" in kinds
    g = next(g for g in gaps if g.kind == "unresolved_reference")
    assert g.ref == "PR777"
    assert g.fillable_by  # must always say where the answer could come from


def test_stale_evidence_becomes_a_gap_only_when_actually_stale():
    now = 1_700_000_000.0
    fresh = amb.compute_freshness([_raw("a", "outlook_mail", now)], now)
    assert not any(g.kind == "stale_evidence" for g in amb.localize_gaps([fresh], [], []))

    ancient = amb.compute_freshness(
        [_raw("a", "outlook_mail", now - 86400.0 * 365)], now
    )
    assert any(g.kind == "stale_evidence" for g in amb.localize_gaps([ancient], [], []))


def test_no_gap_is_ever_raised_about_source_trust():
    """REGRESSION GUARD. An unfamiliar source is not a gap in Jasper's
    understanding - it is only a gap if you believe sources must be ranked,
    which is the authority model this design rejects."""
    items = [_raw("a", "smoke_signal", 1.0)]
    gaps = amb.localize_gaps([], items, [])
    assert not any("trust" in g.kind for g in gaps)


def test_claims_with_no_retrievable_evidence_becomes_a_gap():
    gaps = amb.localize_gaps([], raw_items=[], claims=[{"id": 1}, {"id": 2}])
    g = next(g for g in gaps if g.kind == "claims_without_evidence")
    assert "2 claim" in g.what


def test_every_gap_names_where_the_answer_could_come_from():
    """A gap that can't say where the answer lives is useless downstream -
    it can neither become a targeted question (#397) nor a line in an
    escalation package (#399)."""
    now = 1_700_000_000.0
    items = [_raw("a", "pigeon", now - 86400.0 * 400, pr_number="PR1")]
    comps = [
        amb.compute_freshness(items, now),
        amb.compute_referential_ambiguity(items, set()),
    ]
    gaps = amb.localize_gaps(comps, items, claims=[])
    assert gaps
    for g in gaps:
        assert g.fillable_by and g.what and g.kind


# ------------------------------------------------------------- determinism

def test_same_inputs_give_identical_results():
    now = 1_700_000_000.0
    items = [_raw("a", "outlook_mail", now - 1000), _raw("b", "teams_chat", now - 2000)]
    a = amb.compute_freshness(items, now)
    b = amb.compute_freshness(items, now)
    assert a == b  # frozen dataclass equality


def test_signal_dict_roundtrip_shape():
    sig = amb.AmbiguitySignal(
        project_id="proj-1", ambiguity_score=0.5,
        components=(amb.Component.measured("freshness", 1.0),),
        gaps=(amb.Gap(kind="k", what="w", fillable_by="f"),),
        now_ts=1.0, n_claims=0, n_raw_items=0,
    )
    d = sig.as_dict()
    assert d["measured"] == ["freshness"]
    assert d["abstained"] == []
    assert d["gaps"][0]["fillable_by"] == "f"
