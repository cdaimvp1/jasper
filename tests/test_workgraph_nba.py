"""Regression tests for workgraph_nba.py:
- dollar-range/billion-suffix extraction (task #24)
- per-raw_item value-extraction cache (task #30 enhancement)
- DEFAULT_WEIGHTS immutability (task #30 enhancement)
- 14-day constant behavior preserved after extraction (task #30 enhancement)
- due-date timezone handling (task #24)
"""
import time

import pytest

import workgraph_nba as nba


@pytest.fixture(autouse=True)
def _clear_value_cache():
    """The value-extraction cache is intentionally process-global (that's the
    whole point of it - see workgraph_nba.py's own comment), but that makes it
    a cross-TEST leakage risk if two tests happen to reuse the same raw_item
    id. Clearing it before each test keeps the suite deterministic regardless
    of what other tests run in the same pytest process."""
    nba._value_cache.clear()
    yield
    nba._value_cache.clear()


def test_dollar_range_captures_higher_figure():
    item = {"id": 1, "subject": "Deal worth $2.5-3 million", "body_preview": ""}
    values = [v for v, _, _ in nba._extract_item_candidates(item)]
    assert max(values) == 3_000_000.0


def test_billion_suffix_recognized():
    item = {"id": 2, "subject": "This is a $1.2 billion contract", "body_preview": ""}
    values = [v for v, _, _ in nba._extract_item_candidates(item)]
    assert max(values) == 1_200_000_000.0


def test_value_cache_avoids_recomputation():
    item_v1 = {"id": 42, "subject": "Worth $2.5 million", "body_preview": ""}
    v1 = nba._extract_item_candidates(item_v1)
    assert v1 == [(2_500_000.0, False, False)]

    # same id, DIFFERENT text - cache should still return the ORIGINAL value
    item_v2 = {"id": 42, "subject": "Now says $999 billion", "body_preview": ""}
    v2 = nba._extract_item_candidates(item_v2)
    assert v2 == v1, "cache was not used - recomputed from new text for a known id"


# --- task #24 (2026-08-01): keyword-proximity cues on value extraction -----
# Real incident: marc-308 showed $834,353 as "the deal's value" when the
# text actually says "the order form includes $834,353 in accrued fees" (an
# adjustment, not the ~$53.7M headline value); marc-296 picked $10,000,000
# when the only two figures present were both explicitly labeled "credit".

def test_extract_value_amount_ignores_a_lone_accrued_fee_figure():
    """The exact marc-308 shape: the only dollar figure present is
    downweighted, so the honest answer is 0.0, not that wrong number."""
    items = [{"id": 100, "subject": "Order Form",
              "body_preview": "the order form includes $834,353 in accrued fees for this term"}]
    assert nba._extract_value_amount(items) == 0.0


def test_extract_value_amount_ignores_credit_figures_even_when_larger():
    """The exact marc-296 shape: max-of-everything would pick $10,000,000
    (the larger number) even though BOTH figures present are explicitly
    labeled credits, neither of which is a deal value."""
    items = [{"id": 101, "subject": "Sap - AI and PTO invoice/credit",
              "body_preview": "apply both the $205,500 credit (expires 12/15/26) and an appropriate "
                               "portion of the $10M credit (expires 6/15/28)"}]
    assert nba._extract_value_amount(items) == 0.0


def test_extract_value_amount_prefers_non_downweighted_over_larger_credit():
    """A real total alongside an adjustment: the smaller, un-cued real
    figure must win over the larger credit, not just get averaged out or
    lost to the old plain-max behavior."""
    items = [{"id": 102, "subject": "Renewal",
              "body_preview": "the total contract value is $5,000,000 after applying a $9,000,000 credit"}]
    assert nba._extract_value_amount(items) == 5_000_000.0


def test_extract_value_amount_prefer_cue_wins_over_larger_uncued_figure():
    """Tier 1 (explicit total/contract-value language) outranks tier 2 (the
    largest un-cued figure) even when the un-cued one is bigger - the "PO
    amount" language marks the real deal value; the other number, un-cued,
    could be anything mentioned in passing."""
    items = [{"id": 103, "subject": "PR",
              "body_preview": "the requisition amount is $2,000,000, unrelated budget note mentions $8,000,000 elsewhere"}]
    assert nba._extract_value_amount(items) == 2_000_000.0


def test_extract_value_amount_no_cues_anywhere_behaves_as_before():
    """The vast majority of real issues have no cue words at all - plain
    max-of-everything, unchanged."""
    items = [{"id": 104, "subject": "PR416079", "body_preview": "$44,496,204.00 USD"}]
    assert nba._extract_value_amount(items) == 44_496_204.0


def test_extract_value_amount_finds_a_figure_past_the_old_500char_cutoff(isolated_paths):
    """The actual, forward-looking point of task #29: a real total contract
    value sitting past character 500 of a real email is now found at all -
    the 500-char body_preview alone could never have reached it no matter
    what else was fixed. Goes through the real text_extract.resolve_item_text
    with a real staged file, not a mock, so a real wiring bug would fail this."""
    import json

    padding = "Filler paragraph text with no dollar figures at all. " * 20  # > 500 chars
    assert len(padding) > 500
    full_body = padding + "the total contract value is $12,345,678."

    # Without a raw_ref (old mail, or absorption never happened) - only the
    # truncated preview is reachable, and the figure genuinely isn't in it.
    body_preview = full_body[:500]
    assert "$12,345,678" not in body_preview
    no_ref_item = {"id": 105, "subject": "Renewal", "body_preview": body_preview, "raw_ref": None}
    assert nba._extract_value_amount([no_ref_item]) == 0.0

    # With a real raw_ref pointing at a real staged full body (task #43's
    # actual mechanism) - the figure past character 500 is now reachable.
    rel = "raw_items/106/body.txt"
    full_path = isolated_paths.DOCUMENTS_DIR / rel
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(full_body, encoding="utf-8")
    with_ref_item = {"id": 106, "subject": "Renewal", "body_preview": body_preview,
                      "raw_ref": json.dumps({"body_text": rel})}
    assert nba._extract_value_amount([with_ref_item]) == 12_345_678.0


def test_default_weights_is_immutable():
    with pytest.raises(TypeError):
        nba.DEFAULT_WEIGHTS["value"] = 999


def test_value_amounts_for_issues_matches_single_issue_form(ws_db):
    """Hardening pass #3: batched form must agree with value_amount_for_issue
    exactly - it's a query-count optimization, not a behavior change."""
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    ra = ws_db.insert_raw_item(source="outlook_mail", stable_key="va1", thread_key="va1", dedupe_key="va1",
                                occurred_ts=100.0, subject="Worth $2.5 million", from_actor="a@example.com",
                                participants_json="[]")
    ws_db.link_raw_item_to_issue(ra, a)
    rb = ws_db.insert_raw_item(source="outlook_mail", stable_key="va2", thread_key="va2", dedupe_key="va2",
                                occurred_ts=100.0, subject="no dollar figure here", from_actor="a@example.com",
                                participants_json="[]")
    ws_db.link_raw_item_to_issue(rb, b)

    result = nba.value_amounts_for_issues([a, b])

    assert result[a] == nba.value_amount_for_issue(a) == 2_500_000.0
    assert result[b] == nba.value_amount_for_issue(b) == 0.0


def test_value_amounts_for_issues_empty_list_is_safe(ws_db):
    assert nba.value_amounts_for_issues([]) == {}


def test_value_amounts_for_issues_issue_with_no_raw_items_is_zero(ws_db):
    iid = ws_db.create_issue_with_new_id(title="No items", state="active", category="other")
    assert nba.value_amounts_for_issues([iid]) == {iid: 0.0}


def test_staleness_and_due_urgency_use_same_named_constant():
    now = time.time()
    u = nba._staleness_urgency(now - 7 * nba.DAY, now)
    assert abs(u - 0.5) < 1e-9  # 7 of 14 days

    due_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 7 * nba.DAY))
    d = nba._due_urgency(due_iso, now)
    assert abs(d - 0.5) < 0.01


def test_due_date_naive_timestamp_uses_utc_not_local():
    """Fixed 2026-07-29: a bare date (no explicit tz) used to parse as naive
    and .timestamp() assumed LOCAL time while `now` is a UTC epoch - a
    measured 4h drift on US Eastern. Explicit UTC attachment removes it."""
    now = time.time()
    due_naive = time.strftime("%Y-%m-%dT00:00:00", time.gmtime(now + 10 * nba.DAY))
    d = nba._due_urgency(due_naive, now)
    # 10 days out -> should be firmly in the "not yet overdue, not maxed" band,
    # not skewed hours off by an ambient-timezone assumption
    assert 0.2 < d < 0.4


# score_issue() end-to-end tests (task #51) - this file previously only
# tested score_issue()'s small helper functions in isolation and never
# score_issue() itself, which let a real regression through undetected this
# session: the Aristotle wiring accidentally dropped the days_quiet
# computation, and every score_issue() call (any state) would have raised
# NameError - caught only by a live, real-data check, not by this suite.
# These close that gap.

def test_score_issue_active_no_rules_produces_a_sane_reason(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    issue = ws_db.get_issue(issue_id)
    score, reason, lesson_id = nba.score_issue(issue, time.time())
    assert isinstance(score, float)
    assert "your move" in reason


def test_score_issue_prepends_aristotle_warning_when_unsatisfied(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Sign this", state="active", category="other")
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
        occurred_ts=time.time(), subject="Signature requested", from_actor="a@example.com",
        participants_json="[]", body_preview="please sign",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", ("signature_requested_docusign", row_id))
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign", requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved PO", created_by="marc",
    )

    issue = ws_db.get_issue(issue_id)
    score, reason, lesson_id = nba.score_issue(issue, time.time())

    assert reason.startswith("No confirmation seen yet")
    assert "your move" in reason  # still appended after the warning, not replaced


def test_recompute_all_persists_has_unmet_prerequisite(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Sign this", state="active", category="other")
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k3", thread_key="k3", dedupe_key="k3",
        occurred_ts=time.time(), subject="Signature requested", from_actor="a@example.com",
        participants_json="[]", body_preview="please sign",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", ("signature_requested_docusign", row_id))
    ws_db.create_prerequisite_rule(
        trigger_signal_type="signature_requested_docusign", requires_signal_type="ariba_pr_fully_approved",
        match_on="project", reason="an approved PO", created_by="marc",
    )

    nba.recompute_all()

    issue = ws_db.get_issue(issue_id)
    assert issue["has_unmet_prerequisite"] == 1


def test_recompute_all_leaves_has_unmet_prerequisite_zero_when_no_rule_triggers(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Normal", state="active", category="other")
    nba.recompute_all()
    issue = ws_db.get_issue(issue_id)
    assert issue["has_unmet_prerequisite"] == 0


def test_recompute_all_does_not_reset_updated_at(ws_db):
    """Regression: update_issue() used to unconditionally bump updated_at on
    every write, so recompute_all()'s periodic NBA rescoring erased the very
    staleness signal it's supposed to measure - a 10-day-quiet issue looked
    freshly touched again after each recompute pass."""
    issue_id = ws_db.create_issue_with_new_id(title="Stale one", state="active", category="other")
    before = ws_db.get_issue(issue_id)["updated_at"]
    time.sleep(0.01)

    nba.recompute_all()

    after = ws_db.get_issue(issue_id)["updated_at"]
    assert after == before


def test_update_issue_touch_updated_at_false_leaves_timestamp_alone(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    before = ws_db.get_issue(issue_id)["updated_at"]
    time.sleep(0.01)

    ws_db.update_issue(issue_id, touch_updated_at=False, priority_score=0.5)

    issue = ws_db.get_issue(issue_id)
    assert issue["priority_score"] == 0.5
    assert issue["updated_at"] == before


def test_update_issue_default_still_touches_updated_at(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Y", state="active", category="other")
    before = ws_db.get_issue(issue_id)["updated_at"]
    time.sleep(0.01)

    ws_db.update_issue(issue_id, priority_score=0.5)

    issue = ws_db.get_issue(issue_id)
    assert issue["updated_at"] > before


def test_score_issue_no_warning_when_no_rule_triggers(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Normal issue", state="active", category="other")
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k2", thread_key="k2", dedupe_key="k2",
        occurred_ts=time.time(), subject="Hi", from_actor="a@example.com",
        participants_json="[]", body_preview="just checking in",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)

    issue = ws_db.get_issue(issue_id)
    score, reason, lesson_id = nba.score_issue(issue, time.time())

    assert "No confirmation seen yet" not in reason


# --- task #65: value_at_risk_rollup ------------------------------------------

def _open_issue_with_value(ws_db, title, amount_text, key):
    issue_id = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject=amount_text, from_actor="a@example.com",
        participants_json="[]", body_preview="",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)
    return issue_id


def test_value_at_risk_rollup_empty_when_no_open_issues(ws_db):
    rollup = nba.value_at_risk_rollup()
    assert rollup == {"total": 0.0, "issue_count": 0, "top": []}


def test_value_at_risk_rollup_single_issue(ws_db):
    _open_issue_with_value(ws_db, "Deal A", "Worth $2.5 million", "vr1")

    rollup = nba.value_at_risk_rollup()

    assert rollup["total"] == 2_500_000.0
    assert rollup["issue_count"] == 1
    assert rollup["top"][0]["amount"] == 2_500_000.0


def test_value_at_risk_rollup_sums_and_sorts_multiple_issues(ws_db):
    _open_issue_with_value(ws_db, "Small deal", "Worth $10,000", "vr2")
    _open_issue_with_value(ws_db, "Big deal", "Worth $5 million", "vr3")

    rollup = nba.value_at_risk_rollup()

    assert rollup["total"] == 5_010_000.0
    assert rollup["issue_count"] == 2
    assert [t["amount"] for t in rollup["top"]] == [5_000_000.0, 10_000.0]


def test_value_at_risk_rollup_excludes_amounts_below_floor(ws_db):
    _open_issue_with_value(ws_db, "Tiny mention", "Lunch was $12", "vr4")

    rollup = nba.value_at_risk_rollup()

    assert rollup == {"total": 0.0, "issue_count": 0, "top": []}


def test_value_at_risk_rollup_excludes_closed_issues(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Closed big deal", state="done", category="other")
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="vr5", thread_key="vr5", dedupe_key="vr5",
        occurred_ts=time.time(), subject="Worth $9 million", from_actor="a@example.com",
        participants_json="[]", body_preview="",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)

    rollup = nba.value_at_risk_rollup()

    assert rollup == {"total": 0.0, "issue_count": 0, "top": []}


def test_value_at_risk_rollup_top_capped_at_five_but_total_counts_all(ws_db):
    for i in range(7):
        _open_issue_with_value(ws_db, f"Deal {i}", f"Worth ${(i + 1) * 100_000}", f"vr6-{i}")

    rollup = nba.value_at_risk_rollup()

    assert rollup["issue_count"] == 7
    assert len(rollup["top"]) == 5
    assert rollup["total"] == sum((i + 1) * 100_000 for i in range(7))


# --- Part E1 (2026-07-30): unified ranked candidate-action scoring -------

def test_candidate_actions_includes_nba_surface_when_reason_present():
    issue = {"nba_reason": "your move · $200,000", "state": "active", "priority_score": 0.7}
    result = nba.candidate_actions(issue, evidence=[])
    assert len(result) == 1
    assert result[0]["kind"] == "draft_reply"
    assert result[0]["source_surface"] == "nba"
    assert result[0]["rationale"] == "your move · $200,000"


def test_candidate_actions_waiting_state_maps_to_nudge():
    issue = {"nba_reason": "waiting on vendor", "state": "waiting", "priority_score": 0.4}
    result = nba.candidate_actions(issue, evidence=[])
    assert result[0]["kind"] == "nudge"
    assert result[0]["label"] == "Nudge"


def test_candidate_actions_includes_evidence_row_recommendation():
    issue = {"nba_reason": None, "state": "active", "priority_score": 0.5}
    evidence = [{"recommendations": [{"kind": "contract_review", "label": "Review the attached document",
                                       "rationale": "has an attachment"}]}]
    result = nba.candidate_actions(issue, evidence)
    kinds = {c["kind"] for c in result}
    assert "contract_review" in kinds


def test_candidate_actions_includes_multiple_recommendations_from_one_row():
    # task #15: a single evidence row can carry more than one genuine
    # recommendation (e.g. an attachment matching both invoice-audit and SOW
    # language) - both must surface as separate candidates.
    issue = {"nba_reason": None, "state": "active", "priority_score": 0.5}
    evidence = [{"recommendations": [
        {"kind": "audit_invoice", "label": "Run Invoice Audit", "rationale": "r1"},
        {"kind": "scope_review", "label": "Run Scope Review", "rationale": "r2"},
    ]}]
    result = nba.candidate_actions(issue, evidence)
    kinds = {c["kind"] for c in result}
    assert {"audit_invoice", "scope_review"} <= kinds


def test_candidate_actions_dedupes_evidence_rows_by_kind():
    issue = {"nba_reason": None, "state": "active", "priority_score": 0.5}
    evidence = [
        {"recommendations": [{"kind": "summarize", "label": "Summarize the thread", "rationale": "r1"}]},
        {"recommendations": [{"kind": "summarize", "label": "Summarize the thread", "rationale": "r2"}]},
    ]
    result = nba.candidate_actions(issue, evidence)
    assert len([c for c in result if c["kind"] == "summarize"]) == 1


def test_candidate_actions_includes_synthesis_suggested_actions():
    issue = {"nba_reason": None, "state": "active", "priority_score": 0.5}
    synthesis = {"suggested_actions": [{"label": "Build a 1-page deal summary", "rationale": "per the DoA question"}]}
    result = nba.candidate_actions(issue, [], synthesis)
    assert any(c["label"] == "Build a 1-page deal summary" and c["source_surface"] == "synthesis" for c in result)


def test_candidate_actions_never_empty_even_with_no_real_signal():
    issue = {"nba_reason": None, "state": "active", "priority_score": None}
    result = nba.candidate_actions(issue, [])
    assert len(result) == 1
    assert result[0]["source_surface"] == "fallback"


def test_candidate_actions_ranked_by_score_descending():
    issue = {"nba_reason": "your move", "state": "active", "priority_score": 0.9}
    evidence = [{"recommendations": [{"kind": "summarize", "label": "Summarize", "rationale": "r"}]}]
    synthesis = {"suggested_actions": [{"label": "Custom task", "rationale": "r"}]}
    result = nba.candidate_actions(issue, evidence, synthesis)
    scores = [c["score"] for c in result]
    assert scores == sorted(scores, reverse=True)
    assert result[0]["source_surface"] == "synthesis"


def test_candidate_actions_synthesis_never_outranked_by_high_priority_generic():
    # Real regression (2026-07-31, Marc's direct report against marc-185):
    # a $111.7M issue's generic "Draft a reply / your move" was outranking
    # curator's own specific, content-derived "Confirm the Nintex DocGen
    # notice is legitimate" - a reasoned candidate must never lose to a
    # template just because the issue's priority_score is high.
    issue = {"nba_reason": "your move · $111.7M", "state": "active", "priority_score": 0.99}
    synthesis = {"suggested_actions": [
        {"label": "Approve/reject PR1111865 (SAP RISE Private Cloud)", "rationale": "largest pending approval"},
        {"label": "Confirm the Nintex DocGen notice is legitimate", "rationale": "external phishing banner"},
    ]}
    result = nba.candidate_actions(issue, [], synthesis)
    assert result[0]["source_surface"] == "synthesis"
    assert result[0]["label"] == "Approve/reject PR1111865 (SAP RISE Private Cloud)"
    assert result[1]["source_surface"] == "synthesis"
    nba_candidate = next(c for c in result if c["source_surface"] == "nba")
    assert nba_candidate["score"] < result[1]["score"]


def test_candidate_actions_capped_at_four():
    issue = {"nba_reason": "your move", "state": "active", "priority_score": 0.9}
    evidence = [
        {"recommendations": [{"kind": "contract_review", "label": "a", "rationale": "r"}]},
        {"recommendations": [{"kind": "prep", "label": "b", "rationale": "r"}]},
        {"recommendations": [{"kind": "summarize", "label": "c", "rationale": "r"}]},
    ]
    synthesis = {"suggested_actions": [{"label": "d", "rationale": "r"}, {"label": "e", "rationale": "r"}]}
    result = nba.candidate_actions(issue, evidence, synthesis)
    assert len(result) == 4
