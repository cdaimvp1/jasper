"""Regression tests for workgraph_nba.py:
- dollar-range/billion-suffix extraction (task #24)
- per-raw_item value-extraction cache (task #30 enhancement)
- DEFAULT_WEIGHTS immutability (task #30 enhancement)
- 14-day constant behavior preserved after extraction (task #30 enhancement)
- due-date timezone handling (task #24)
"""
import time

import pytest

import workgraph_lessons
import workgraph_nba as nba


def _isolate_config(monkeypatch, tmp_path):
    """Same isolation pattern as test_workgraph_projects.py's own helper -
    config.SETTINGS_PATH is bound at import time, not per-test."""
    import config
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_cache", {})
    monkeypatch.setattr(config, "_cache_mtime", 0.0)
    return config


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


def test_extract_value_amount_reads_attachment_extracted_text(ws_db, isolated_paths):
    """The other half of task #29: a real dollar figure sitting only in an
    attachment (a PDF order form, an XLSX pricing sheet) - never in the
    email subject or body at all - was structurally invisible before
    today. Real attachments row, not a mock, via the real store function."""
    item = {"id": 200, "subject": "Order form attached", "body_preview": "see attached", "raw_ref": None}
    nba._value_cache.clear()
    assert nba._extract_value_amount([item]) == 0.0  # nothing in the email text itself

    ws_db.create_attachment(
        entity_type="raw_item", entity_id="200", kind="reference", filename="order_form.pdf",
        stored_path="raw_items/200/order_form.pdf", content_type=None, size_bytes=1234,
        sha256_hex="deadbeef", uploaded_by="test",
        extracted_text="The total contract value is $30,500,000 for this term.",
    )
    nba._value_cache.clear()  # the item id is unchanged, but the negative result above is now stale

    assert nba._extract_value_amount([item]) == 30_500_000.0


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


def test_staleness_urgency_accepts_custom_saturation_days():
    """Enhancement idea panel #9: a category-specific saturation should
    behave exactly like STALENESS_SATURATION_DAYS did before this feature -
    same math, just a different denominator."""
    now = time.time()
    u = nba._staleness_urgency(now - 15 * nba.DAY, now, saturation_days=30.0)
    assert abs(u - 0.5) < 1e-9  # 15 of 30 days
    u_default = nba._staleness_urgency(now - 7 * nba.DAY, now)
    assert abs(u_default - 0.5) < 1e-9  # unchanged default behavior


# --- enhancement idea panel #9: category-relative staleness ---------------

def _raw_item_at(ws_db, issue_id, key, occurred_ts):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=occurred_ts, subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    return rid


def test_compute_category_staleness_baselines_needs_a_minimum_gap_count(ws_db):
    """A category with too few real gaps to draw from (below
    _MIN_GAPS_FOR_CATEGORY_BASELINE) must be absent, not given a
    single-thread-derived guess."""
    now = time.time()
    issue_id = ws_db.create_issue_with_new_id(title="A", state="active", category="thin-category")
    _raw_item_at(ws_db, issue_id, "r1", now - 5 * nba.DAY)
    _raw_item_at(ws_db, issue_id, "r2", now - 3 * nba.DAY)

    baselines = nba.compute_category_staleness_baselines()

    assert "thin-category" not in baselines


def test_compute_category_staleness_baselines_uses_real_median_gap(ws_db):
    now = time.time()
    # 8 issues, each with exactly one real 10-day gap between two raw_items -
    # meets _MIN_GAPS_FOR_CATEGORY_BASELINE with a clean, known median.
    for n in range(8):
        issue_id = ws_db.create_issue_with_new_id(title=f"C{n}", state="active", category="slow-cadence")
        _raw_item_at(ws_db, issue_id, f"c{n}-r1", now - 20 * nba.DAY)
        _raw_item_at(ws_db, issue_id, f"c{n}-r2", now - 10 * nba.DAY)

    baselines = nba.compute_category_staleness_baselines()

    assert abs(baselines["slow-cadence"] - 10.0) < 0.01


def test_compute_category_staleness_baselines_floors_unusually_short_gaps(ws_db):
    now = time.time()
    for n in range(8):
        issue_id = ws_db.create_issue_with_new_id(title=f"F{n}", state="active", category="fast-cadence")
        _raw_item_at(ws_db, issue_id, f"f{n}-r1", now - 2 * nba.DAY)
        _raw_item_at(ws_db, issue_id, f"f{n}-r2", now - 2 * nba.DAY + 3600)  # 1h gap

    baselines = nba.compute_category_staleness_baselines()

    assert baselines["fast-cadence"] == nba._CATEGORY_BASELINE_FLOOR_DAYS


def test_score_issue_uses_category_baseline_when_given(ws_db):
    now = time.time()
    issue_id = ws_db.create_issue_with_new_id(title="A", state="active", category="slow-cadence")
    ws_db.update_issue(issue_id, touch_updated_at=False, priority_score=0.0)
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (now - 15 * nba.DAY, issue_id))
    conn.commit()
    conn.close()
    issue = ws_db.get_issue(issue_id)

    score_flat, _, _ = nba.score_issue(issue, now)
    score_relative, _, _ = nba.score_issue(
        issue, now, category_staleness_baselines={"slow-cadence": 30.0})

    # 15 days stale against a flat 14d saturation is already maxed (1.0);
    # against a real 30d category baseline it's only half-stale - the whole
    # point of this feature, confirmed by the scores actually differing.
    assert score_relative < score_flat


def test_score_issue_falls_back_to_flat_default_for_unlisted_category(ws_db):
    now = time.time()
    issue_id = ws_db.create_issue_with_new_id(title="A", state="active", category="never-seen-before")
    issue = ws_db.get_issue(issue_id)

    with_empty_baselines, _, _ = nba.score_issue(issue, now, category_staleness_baselines={})
    with_none, _, _ = nba.score_issue(issue, now, category_staleness_baselines=None)

    assert with_empty_baselines == with_none


# --- enhancement idea panel #10: snooze history surfacing -----------------

def test_snooze_history_from_state_history_keeps_only_actored_waiting_transitions():
    history = [
        {"to_state": "waiting", "actor": "marc", "changed_ts": 100.0},   # real snooze
        {"to_state": "waiting", "actor": None, "changed_ts": 200.0},     # organic wait - excluded
        {"to_state": "done", "actor": "marc", "changed_ts": 300.0},      # not a waiting transition
        {"to_state": "waiting", "actor": "marc", "changed_ts": 400.0},   # real snooze
    ]
    snoozes = nba.snooze_history_from_state_history(history)
    assert [s["changed_ts"] for s in snoozes] == [100.0, 400.0]


def test_snooze_history_from_state_history_empty_for_no_history():
    assert nba.snooze_history_from_state_history([]) == []


def test_apply_snooze_avoidance_boost_scales_with_count_and_caps():
    assert nba._apply_snooze_avoidance_boost(0.5, 0) == 0.5
    assert abs(nba._apply_snooze_avoidance_boost(0.5, 2) - 0.6) < 1e-9
    # caps at _SNOOZE_BOOST_MAX_COUNT (5) - a 10th snooze adds no more than a 5th
    five = nba._apply_snooze_avoidance_boost(0.5, 5)
    ten = nba._apply_snooze_avoidance_boost(0.5, 10)
    assert five == ten
    # never exceeds 1.0
    assert nba._apply_snooze_avoidance_boost(0.95, 5) == 1.0


def test_score_issue_snoozed_issue_scores_higher_and_names_it_in_reason(ws_db):
    now = time.time()
    issue_id = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    issue = ws_db.get_issue(issue_id)
    snoozed_history = [
        {"to_state": "waiting", "actor": "marc", "changed_ts": now - 5 * nba.DAY},
        {"to_state": "waiting", "actor": "marc", "changed_ts": now - 2 * nba.DAY},
    ]

    plain_score, plain_reason, _ = nba.score_issue(issue, now)
    snoozed_score, snoozed_reason, _ = nba.score_issue(issue, now, state_history=snoozed_history)

    assert snoozed_score > plain_score
    assert "snoozed 2x" in snoozed_reason


def test_score_issue_single_snooze_not_named_in_reason(ws_db):
    """Only 2+ snoozes are worth calling out by name - one snooze is normal
    triage, not yet a pattern worth flagging."""
    now = time.time()
    issue_id = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    issue = ws_db.get_issue(issue_id)
    one_snooze = [{"to_state": "waiting", "actor": "marc", "changed_ts": now - 2 * nba.DAY}]

    _, reason, _ = nba.score_issue(issue, now, state_history=one_snooze)

    assert "snoozed" not in reason


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

def test_score_issue_dismissed_scores_as_closed(ws_db):
    """Task #44: 'dismissed' is a real, distinct terminal state, and must be
    treated the same as done/noise-archived for scoring - a dismissed issue
    should never re-surface with a nonzero priority score."""
    issue_id = ws_db.create_issue_with_new_id(title="X", state="dismissed", category="other")
    issue = ws_db.get_issue(issue_id)
    score, reason, lesson_id = nba.score_issue(issue, time.time())
    assert score == 0.0
    assert reason == "closed"


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


# --- Phase 0 fix (D11, 2026-08-03): lessons cross-engine leakage gate ------

def _issue_with_matchable_lesson(ws_db):
    """An issue whose situation_key (category + first external company) has
    a real, confirmed lesson recorded against it - the shape find_matching_
    lesson/best_lesson_for_key need to actually return something rather than
    None regardless of the flag under test."""
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="rfp-sourcing")
    ws_db.upsert_party(id="p1", primary_email="rep@acme.com", display_name="Rep",
                        affiliation="external", affiliation_confidence="H",
                        affiliation_source="domain", company="Acme")
    ws_db.link_party_to_issue(issue_id, "p1")
    workgraph_lessons.record_lesson(
        situation_key_val="category:rfp-sourcing|company:acme",
        statement="Acme RFPs of this shape usually confirm.",
        outcome="confirmed", source_issue_id=issue_id,
    )
    return ws_db.get_issue(issue_id)


def test_score_issue_real_anchors_override_the_shim(ws_db):
    """Confidence spine v1: an issue with only a category (no real
    reference on its raw_items yet) but a real, backfilled 'exact' anchor
    on file must score its provenance from that anchor, not the shim's
    weaker category-only default."""
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="rfp-sourcing")
    issue = ws_db.get_issue(issue_id)
    now = time.time()

    score_via_shim, _, _ = nba.score_issue(issue, now)

    ws_db.create_identity_anchor(anchor_type="jasper_ref", normalized_value=issue_id,
                                  anchor_strength="exact", exclusive=True, issue_id=issue_id)
    real_anchors = ws_db.list_identity_anchors(issue_id=issue_id)
    score_via_real_anchor, _, _ = nba.score_issue(issue, now, identity_anchors=real_anchors)

    assert score_via_real_anchor > score_via_shim


def test_recompute_all_batches_anchor_lookup_not_one_query_per_issue(ws_db, monkeypatch):
    a = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    b = ws_db.create_issue_with_new_id(title="B", state="active", category="other")
    calls = []
    real_batched = ws_db.list_identity_anchors_for_issues
    monkeypatch.setattr(ws_db, "list_identity_anchors_for_issues", lambda ids: (calls.append(ids), real_batched(ids))[1])

    nba.recompute_all()

    assert len(calls) == 1
    assert set(calls[0]) == {a, b}


def test_score_issue_ignores_lesson_by_default(ws_db):
    """workgraph_lessons is entirely a grouping-correction store - it must
    not move NBA urgency unless the cross-engine flag is explicitly on."""
    issue = _issue_with_matchable_lesson(ws_db)
    score, reason, lesson_id = nba.score_issue(issue, time.time())
    assert lesson_id is None
    assert "precedent:" not in reason


def test_score_issue_uses_lesson_when_flag_enabled(ws_db, monkeypatch, tmp_path):
    config = _isolate_config(monkeypatch, tmp_path)
    config.set_value(True, "grouping", "legacy_lessons_cross_engine_enabled")

    issue = _issue_with_matchable_lesson(ws_db)
    score, reason, lesson_id = nba.score_issue(issue, time.time())

    assert lesson_id is not None
    assert "precedent:" in reason


# --- Phase 0 fix (D12, 2026-08-03): nba_choice_log expiry gate -------------

def test_run_choice_log_expiry_daily_if_due_expires_old_and_gates_second_call(ws_db):
    iid = ws_db.create_issue_with_new_id(title="A", state="active", category="other")
    log_id = ws_db.create_nba_choice_log(issue_id=iid, offered_json="[]", scoring_inputs_json="{}")
    conn = ws_db._connect()
    conn.execute("UPDATE nba_choice_log SET offered_ts = ? WHERE id = ?", (time.time() - 30 * 86400, log_id))
    conn.close()

    first = nba.run_choice_log_expiry_daily_if_due()
    assert first["expired"] == 1
    assert ws_db.get_most_recent_open_choice_log(iid) is None

    second = nba.run_choice_log_expiry_daily_if_due()
    assert second is None


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


def test_candidate_actions_evidence_row_candidate_carries_raw_item_id():
    # 2026-08-02, detail-panel port: lets the checklist UI scope this action
    # to the specific ask/decision sharing this same raw_item_id.
    issue = {"nba_reason": None, "state": "active", "priority_score": 0.5}
    evidence = [{"raw_item_id": 469, "recommendations": [
        {"kind": "contract_review", "label": "Review the attached document", "rationale": "has an attachment"},
    ]}]
    result = nba.candidate_actions(issue, evidence)
    match = next(c for c in result if c["kind"] == "contract_review")
    assert match["raw_item_id"] == 469


def test_candidate_actions_nba_surface_has_no_raw_item_id():
    issue = {"nba_reason": "your move", "state": "active", "priority_score": 0.5}
    result = nba.candidate_actions(issue, [])
    match = next(c for c in result if c["source_surface"] == "nba")
    assert "raw_item_id" not in match


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


def test_candidate_actions_falls_back_to_project_synthesis_when_issue_synthesis_has_no_actions():
    # Task #21: a synthesis dict with a real summary but an empty
    # suggested_actions list is still truthy - the old `synthesis or
    # project_synthesis` at the call site always picked it and never
    # looked at project_synthesis, even when THAT had real actions.
    issue = {"nba_reason": None, "state": "active", "priority_score": 0.5}
    synthesis = {"summary": "issue-level summary, no actions", "suggested_actions": []}
    project_synthesis = {"suggested_actions": [{"label": "Project-level action", "rationale": "r"}]}
    result = nba.candidate_actions(issue, [], synthesis, project_synthesis)
    assert any(c["label"] == "Project-level action" and c["source_surface"] == "synthesis" for c in result)


def test_candidate_actions_prefers_issue_synthesis_over_project_when_both_have_actions():
    issue = {"nba_reason": None, "state": "active", "priority_score": 0.5}
    synthesis = {"suggested_actions": [{"label": "Issue-level action", "rationale": "r"}]}
    project_synthesis = {"suggested_actions": [{"label": "Project-level action", "rationale": "r"}]}
    result = nba.candidate_actions(issue, [], synthesis, project_synthesis)
    labels = {c["label"] for c in result}
    assert "Issue-level action" in labels
    assert "Project-level action" not in labels


def test_candidate_actions_empty_with_no_real_signal():
    """Phase 0 fix (D15, 2026-08-03): this used to assert the OPPOSITE - an
    unconditional 'Draft a reply' fallback with source_surface 'fallback'
    and zero supporting evidence, presented like a real candidate. Changed
    because that fallback is exactly D15: no evidence should mean no
    candidate, not a manufactured one."""
    issue = {"nba_reason": None, "state": "active", "priority_score": None}
    result = nba.candidate_actions(issue, [])
    assert result == []


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
