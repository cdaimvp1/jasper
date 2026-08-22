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


# ------------------------------------------------- state coherence (#412)

def test_state_coherence_is_one_when_nothing_flagged():
    c = amb.compute_state_coherence([{"id": 1}, {"id": 2}], [])
    assert c.value == pytest.approx(1.0)
    assert c.detail["n_flagged"] == 0


def test_state_coherence_drops_with_flagged_claims():
    claims = [{"id": i} for i in range(1, 5)]
    inc = [{"claim_id": 1, "issue_id": "marc-1", "evidence_type": "issue_closed_with_open_claims",
            "evidence_note": "x"}]
    c = amb.compute_state_coherence(claims, inc)
    assert c.value == pytest.approx(0.75)
    assert c.detail["issues"] == ["marc-1"]


def test_state_coherence_counts_distinct_claims_not_rows():
    claims = [{"id": 1}, {"id": 2}]
    inc = [{"claim_id": 1, "issue_id": "marc-1"}, {"claim_id": 1, "issue_id": "marc-1"}]
    assert amb.compute_state_coherence(claims, inc).value == pytest.approx(0.5)


def test_state_coherence_abstains_with_no_claims():
    assert amb.compute_state_coherence([], []).abstained


def test_contradiction_still_abstains_and_says_why_it_is_not_state_coherence():
    """#412's real finding: claim_edges is empty BY DESIGN, and the 43 live
    'contradiction' suggestions are lifecycle incoherence, not semantic
    contradiction. The abstention reason must keep those distinct so nobody
    later wires the wrong signal into the wrong component."""
    c = next(x for x in amb._abstaining_components() if x.name == "contradiction")
    assert c.abstained
    assert "BY DESIGN" in c.abstained_reason
    assert "state_coherence" in c.abstained_reason


def test_closed_issue_with_open_claims_becomes_a_named_gap():
    claims = [{"id": 1}, {"id": 2}]
    inc = [{"claim_id": 1, "issue_id": "marc-4221"}]
    comps = [amb.compute_state_coherence(claims, inc)]
    gaps = amb.localize_gaps(comps, [], claims)
    g = next(g for g in gaps if g.kind == "closed_issue_with_open_claims")
    assert g.ref == "marc-4221"
    assert g.fillable_by


def test_no_coherence_gap_when_fully_coherent():
    comps = [amb.compute_state_coherence([{"id": 1}], [])]
    gaps = amb.localize_gaps(comps, [], [{"id": 1}])
    assert not any(g.kind == "closed_issue_with_open_claims" for g in gaps)


# ------------------------------------------- trend / volatility (task #395)

def _obs(score):
    return {"ambiguity_score": score}


def test_trend_unknown_with_no_history():
    """Blueprint missing-history rule: no trend from a single point."""
    t = amb.compute_uncertainty_trend([], 0.5)
    assert t.state == "unknown"
    assert t.delta is None


def test_trend_unknown_when_current_score_is_null():
    """Every component abstained - must not fabricate a delta."""
    t = amb.compute_uncertainty_trend([_obs(0.4)], None)
    assert t.state == "unknown"
    assert t.delta is None


def test_trend_improving_when_ambiguity_falls():
    t = amb.compute_uncertainty_trend([_obs(0.60)], 0.40)
    assert t.state == "improving"
    assert t.delta == pytest.approx(-0.20)


def test_trend_conflict_when_ambiguity_rises():
    """THE case a scalar conflates. Rising ambiguity in Jasper means
    contradictory context arrived - a real disagreement discovered, which must
    be surfaced, NOT treated as loop divergence the way cd\ai would."""
    t = amb.compute_uncertainty_trend([_obs(0.30)], 0.55)
    assert t.state == "conflict"
    assert t.delta == pytest.approx(0.25)


def test_trend_exhausted_when_effectively_unchanged():
    t = amb.compute_uncertainty_trend([_obs(0.500)], 0.505)
    assert t.state == "exhausted"


def test_deadband_boundary_is_not_conflict():
    """A change smaller than the dead-band is source-exhaustion, not a
    discovered conflict - otherwise noise would look like disagreement."""
    t = amb.compute_uncertainty_trend([_obs(0.50)], 0.50 + amb.TREND_DEADBAND - 0.001)
    assert t.state == "exhausted"


def test_volatility_none_below_three_observations():
    """None, never 0.0 - 0.0 would read as 'measured, and stable'."""
    assert amb.compute_uncertainty_trend([_obs(0.4)], 0.5).volatility is None


def test_volatility_computed_from_three_or_more():
    t = amb.compute_uncertainty_trend([_obs(0.1), _obs(0.9), _obs(0.2)], 0.8)
    assert t.volatility is not None
    assert 0.0 <= t.volatility <= 1.0


def test_volatility_window_is_capped_at_five():
    hist = [_obs(v) for v in (0.5,) * 20]
    t = amb.compute_uncertainty_trend(hist, 0.5)
    assert t.volatility == pytest.approx(0.0)  # all identical -> no oscillation


def test_trend_ignores_null_scores_in_history():
    hist = [{"ambiguity_score": None}, _obs(0.6)]
    t = amb.compute_uncertainty_trend(hist, 0.3)
    assert t.state == "improving"
    assert t.n_observations == 1


def test_trend_is_deterministic():
    hist = [_obs(0.3), _obs(0.7), _obs(0.5)]
    assert amb.compute_uncertainty_trend(hist, 0.4) == amb.compute_uncertainty_trend(hist, 0.4)


# ------------------------------------------------ pass forecast (task #409)

def test_forecast_no_history_warrants_a_first_pass():
    """You cannot forecast from no data - and that must be said, not silently
    defaulted to 'spend'."""
    f = amb.forecast_next_pass([], 0.5)
    assert f.recommendation == "no_history"
    assert f.projected_passes is None


def test_forecast_null_current_score_is_no_history():
    assert amb.forecast_next_pass([_obs(0.4)], None).recommendation == "no_history"


def test_forecast_surfaces_conflict_instead_of_spending():
    """Rising ambiguity means contradictory context arrived. More passes over
    the same contradiction will not resolve it - a person will."""
    f = amb.forecast_next_pass([_obs(0.30)], 0.60)
    assert f.recommendation == "surface_conflict"
    assert "contradictory" in f.reason


def test_forecast_detects_spinning_after_two_flat_passes():
    f = amb.forecast_next_pass([_obs(0.500), _obs(0.505)], 0.500)
    assert f.recommendation == "spinning"
    assert "nothing left to add" in f.reason


def test_forecast_one_flat_pass_is_not_yet_spinning():
    """One flat pass can mean the single source consulted had nothing - not
    that nothing remains to learn."""
    f = amb.forecast_next_pass([_obs(0.50)], 0.50)
    assert f.recommendation != "spinning"


def test_forecast_spend_while_converging_and_projects_passes():
    f = amb.forecast_next_pass([_obs(0.80), _obs(0.64)], 0.51)
    assert f.recommendation == "spend"
    assert f.contraction_ratio is not None and f.contraction_ratio < 1.0
    assert f.projected_passes and f.projected_passes >= 1


def test_forecast_spinning_when_ratio_at_or_above_one():
    """A ratio >= 1 means ambiguity is not shrinking, so do not keep paying."""
    f = amb.forecast_next_pass([_obs(0.40), _obs(0.30)], 0.44)
    assert f.recommendation in ("spinning", "surface_conflict")


def test_forecast_no_projection_once_at_target():
    f = amb.forecast_next_pass([_obs(0.30), _obs(0.20)], 0.10)
    assert f.recommendation == "spend"
    assert f.projected_passes is None
    assert "at or below target" in f.reason


def test_forecast_is_advisory_only_and_carries_a_reason():
    """PCM Theorem G.9.1: it cannot authorize or execute. Every outcome must
    explain itself so a caller can override it knowingly."""
    for hist, cur in (([], 0.5), ([_obs(0.3)], 0.6), ([_obs(0.5), _obs(0.5)], 0.5),
                      ([_obs(0.8), _obs(0.6)], 0.4)):
        f = amb.forecast_next_pass(hist, cur)
        assert f.reason
        assert f.recommendation in ("spend", "spinning", "surface_conflict", "no_history")


def test_forecast_is_deterministic_and_calls_no_model():
    hist = [_obs(0.7), _obs(0.6)]
    assert amb.forecast_next_pass(hist, 0.5) == amb.forecast_next_pass(hist, 0.5)


# ------------------------------------------ the Two Gates convention (#410)
# Adopted 2026-08-21. docs/design/GATES_FEDERATION_AND_MECHANISM_TRIAGE.md s1.
# These guard the ONE line that would turn measurement into authority:
# `if ambiguity < threshold: auto_approve()`. Before this convention was
# written down, both gates were independent BY ACCIDENT - Gate A because
# nothing consumed it, Gate B because nothing fed it. Wiring them is a
# natural-looking refactor, which is exactly why it needs a guard.

def test_gate_a_takes_no_write_path_and_grants_no_approval():
    """R1/R2: this module may block or name a gap; it may never authorize.
    An approval/authorize/dispatch entry point here is the violation."""
    import inspect
    for name in dir(amb):
        if name.startswith("_"):
            continue
        low = name.lower()
        assert not any(w in low for w in ("approve", "authorize", "authorise",
                                          "dispatch", "execute", "commit")), (
            f"workgraph_ambiguity.{name} looks like an authorization path; "
            "Gate A measures and abstains, it never authorizes (R2)")
    src = inspect.getsource(amb)
    for banned in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert banned not in src.upper().replace("UPDATED", "XXXXXXX"), (
            f"Gate A must stay signal-only; found {banned!r}")


def test_gate_b_never_consults_evidence_quality():
    """R1 from the other side. resolve_required_approval is Gate B: it decides
    Jasper's PERMISSIONS from a human-maintained table, never from how sure
    Jasper feels. If it ever grows an ambiguity/confidence parameter, the two
    gates have been fused and the authority model is back."""
    import inspect
    import workgraph_store as ws
    sig = inspect.signature(ws.resolve_required_approval)
    assert list(sig.parameters) == ["action_type"], (
        "Gate B gained a parameter; if it is an ambiguity/confidence score "
        "this is the score-to-action mapping R1 forbids")
    # Strip the docstring first: it DISCUSSES ambiguity deliberately (it names
    # Gate A and the R1 rule). What must stay clean is the executable body.
    src = inspect.getsource(ws.resolve_required_approval)
    body = src.replace(ws.resolve_required_approval.__doc__ or "", "").lower()
    for banned in ("ambiguity", "confidence", "sufficiency", "trust_score"):
        assert banned not in body, (
            f"Gate B references {banned!r} - it must not read evidence quality")


def test_gate_b_defaults_to_requiring_approval():
    """R2: gates subtract. An unknown action_type must escalate, not pass."""
    import workgraph_store as ws
    assert ws.resolve_required_approval("some_action_never_seen_before") == 1


def test_provenance_docstring_matches_the_code():
    """The header's computable/abstains table is load-bearing documentation -
    it is how the next reader learns what this module can actually do. It
    listed provenance_reliability as COMPUTABLE 'via declared trust map'
    until 2026-08-21, describing a design removed in 0c3de45 and contradicting
    both _abstaining_components() and test_no_source_trust_table_exists_
    anywhere. Keep them in agreement."""
    doc = amb.__doc__ or ""
    assert "provenance_reliability COMPUTABLE" not in doc
    abstaining = {c.name for c in amb._abstaining_components()}
    assert "provenance_reliability" in abstaining
