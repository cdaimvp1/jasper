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


# --- enhancement idea panel #16: conflicting dollar-figure flag ------------

def test_conflicting_value_figures_single_entry_when_all_messages_agree():
    item_a = {"id": 401, "subject": "Total contract value is $50,000", "body_preview": ""}
    item_b = {"id": 402, "subject": "Confirming total contract value $50,000", "body_preview": ""}

    figures = nba.conflicting_value_figures_for_issue([item_a, item_b])

    assert len(figures) == 1  # same figure everywhere - not a conflict


def test_conflicting_value_figures_flags_two_disagreeing_preferred_amounts():
    item_a = {"id": 411, "subject": "Total contract value is $50,000", "body_preview": "", "occurred_ts": 100.0}
    item_b = {"id": 412, "subject": "Updated: total contract value is $75,000", "body_preview": "", "occurred_ts": 200.0}

    figures = nba.conflicting_value_figures_for_issue([item_a, item_b])

    assert len(figures) == 2
    assert [f["amount"] for f in figures] == [75_000.0, 50_000.0]  # highest first
    assert {f["raw_item_id"] for f in figures} == {411, 412}


def test_conflicting_value_figures_ignores_non_preferred_figures():
    """Two different NON-preferred numbers (no total/contract-value cue)
    aren't a real conflict about the deal's own value - only disagreement
    among PREFERRED-tier figures counts."""
    item_a = {"id": 421, "subject": "See attached, $1,200 for shipping", "body_preview": ""}
    item_b = {"id": 422, "subject": "Also $900 for handling", "body_preview": ""}

    figures = nba.conflicting_value_figures_for_issue([item_a, item_b])

    assert figures == []


def test_conflicting_value_figures_single_entry_for_single_raw_item():
    item = {"id": 431, "subject": "Total contract value is $50,000", "body_preview": ""}
    assert len(nba.conflicting_value_figures_for_issue([item])) == 1


def test_conflicting_value_figures_empty_for_no_raw_items():
    assert nba.conflicting_value_figures_for_issue([]) == []


def test_conflicting_value_figures_ignores_multiple_totals_within_one_message(ws_db):
    """Real bug caught during live verification: a single SOW/order-form
    message routinely has several internally legitimate preferred-cued
    figures of its own (milestone totals, a grand total) - collecting every
    preferred candidate across the whole thread flagged that as a false
    multi-way "conflict" on real production data. Only comparing each
    MESSAGE's own single best figure against other messages' should treat
    a lone multi-total message as zero conflict, not len>=2."""
    item = {
        "id": 441,
        "subject": "SOW",
        "body_preview": "Milestone 1 total: $58,800. Milestone 2 total: $10,800. Grand total: $135,000.",
    }
    figures = nba.conflicting_value_figures_for_issue([item])

    assert len(figures) == 1  # one message, one headline figure - not a conflict
    assert figures[0]["amount"] == 135_000.0  # this message's own MAX preferred figure


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


def _scoring_claim(first_seen_ts, **kw):
    c = {"id": 1, "claim_type": "ask", "text": "t", "owner": "marc",
         "first_seen_ts": first_seen_ts, "escalated": 0, "raw_item_id": 1}
    c.update(kw)
    return c


def test_staleness_uses_when_the_ask_arrived_not_when_it_was_extracted():
    """The trap: first_seen_ts is MATERIALIZATION time. Measured live
    2026-08-22, 8,635 of 9,021 open claims (96%) had first_seen_ts later than
    their own evidence, median 55 days - so a long-ignored ask scored as if it
    had just landed. asked_ts is the message's own occurred_ts."""
    now = 1_800_000_000.0
    day = 86400.0
    extracted_yesterday = _scoring_claim(now - 1 * day)

    fresh, _ = nba.score_claim(extracted_yesterday, date_urgency=0.0,
                               value_urgency_score=0.0, now=now)
    aged, _ = nba.score_claim(extracted_yesterday, date_urgency=0.0,
                              value_urgency_score=0.0, now=now,
                              asked_ts=now - 120 * day)
    assert aged > fresh, "a 120-day-old ask must outscore a same-claim 1-day read"


def test_days_open_reports_the_real_age_in_the_reason_line():
    """41 live claims showed no age at all when they should have read
    'open 7d+' - the human-visible half of the same bug."""
    now = 1_800_000_000.0
    day = 86400.0
    claim = _scoring_claim(now - 1 * day)

    _, reason_wrong = nba.score_claim(claim, date_urgency=0.0, value_urgency_score=0.0, now=now)
    assert "open" in reason_wrong and "d" not in reason_wrong.replace("open", "")

    _, reason_right = nba.score_claim(claim, date_urgency=0.0, value_urgency_score=0.0,
                                      now=now, asked_ts=now - 90 * day)
    assert "open 90d" in reason_right


def test_absent_asked_ts_falls_back_to_first_seen_unchanged():
    """Every pre-existing caller must keep its exact old behaviour - the
    parameter is additive, not a silent change of meaning."""
    now = 1_800_000_000.0
    claim = _scoring_claim(now - 30 * 86400.0)
    a = nba.score_claim(claim, date_urgency=0.0, value_urgency_score=0.0, now=now)
    b = nba.score_claim(claim, date_urgency=0.0, value_urgency_score=0.0, now=now, asked_ts=None)
    c = nba.score_claim(claim, date_urgency=0.0, value_urgency_score=0.0, now=now,
                        asked_ts=claim["first_seen_ts"])
    assert a == b == c


def test_rank_actions_supplies_the_real_ask_time_from_evidence(ws_db, monkeypatch):
    """End-to-end: the caller must actually pass it. A correct score_claim
    that nobody feeds is the same bug with extra steps."""
    now = time.time()
    day = 86400.0
    iid = ws_db.create_issue_with_new_id(title="Long-ignored ask", state="active", category="other")
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k", thread_key="k", dedupe_key="k",
        occurred_ts=now - 120 * day, subject="please approve", from_actor="rep@vendor.com",
        participants_json="[]", body_preview="please approve")
    ws_db.link_raw_item_to_issue(rid, iid)
    conn = ws_db._connect()
    conn.execute(
        """INSERT INTO claims (issue_id, raw_item_id, claim_type, text, author,
                               author_basis, owner, status, first_seen_ts, last_seen_ts)
           VALUES (?, ?, 'ask', 'please approve', 'counterparty', 'direction',
                   'marc', 'open', ?, ?)""",
        (iid, rid, now - 1 * day, now - 1 * day))
    conn.commit()
    conn.close()

    seen = {}
    real = nba.score_claim
    monkeypatch.setattr(nba, "score_claim",
                        lambda c, **kw: seen.update(asked_ts=kw.get("asked_ts")) or real(c, **kw))
    ranked = nba.rank_actions(limit=5, now=now)

    assert ranked, "the claim should rank"
    assert seen["asked_ts"] == pytest.approx(now - 120 * day), \
        "rank_actions must pass the evidence's occurred_ts, not leave it None"
    assert "open 120d" in ranked[0]["reason"]


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


# --- project_links prerequisite gating (task #319) -------------------------
# Same real "your move" gating mechanism as Aristotle's own taught rules
# above, driven by a real project_links depends_on/blocks row instead of a
# signal-type rule - see workgraph_aristotle.check_project_link_prerequisite.

def test_score_issue_prepends_project_link_warning_when_target_unresolved(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="New H1 Deal")
    proj_b = ws_db.create_project_with_new_id(name="Old H1 Exit")
    issue_id = ws_db.create_issue_with_new_id(title="Sign the new deal", state="active", category="other")
    ws_db.update_issue(issue_id, project_id=proj_a)
    ws_db.create_project_link(from_project_id=proj_a, to_project_id=proj_b,
                               link_type="depends_on", reason="needs the old contract exited first")

    issue = ws_db.get_issue(issue_id)
    score, reason, lesson_id = nba.score_issue(issue, time.time())

    assert reason.startswith("No confirmation seen yet")
    assert "Old H1 Exit" in reason
    assert "your move" in reason  # still appended after the warning, not replaced


def test_recompute_all_persists_has_unmet_prerequisite_from_project_link(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="New H1 Deal")
    proj_b = ws_db.create_project_with_new_id(name="Old H1 Exit")
    issue_id = ws_db.create_issue_with_new_id(title="Sign the new deal", state="active", category="other")
    ws_db.update_issue(issue_id, project_id=proj_a)
    ws_db.create_project_link(from_project_id=proj_a, to_project_id=proj_b,
                               link_type="depends_on", reason="needs the old contract exited first")

    nba.recompute_all()

    issue = ws_db.get_issue(issue_id)
    assert issue["has_unmet_prerequisite"] == 1


def test_score_issue_no_project_link_warning_when_target_project_done(ws_db):
    proj_a = ws_db.create_project_with_new_id(name="New H1 Deal")
    proj_b = ws_db.create_project_with_new_id(name="Old H1 Exit")
    issue_id = ws_db.create_issue_with_new_id(title="Sign the new deal", state="active", category="other")
    ws_db.update_issue(issue_id, project_id=proj_a)
    ws_db.create_project_link(from_project_id=proj_a, to_project_id=proj_b,
                               link_type="depends_on", reason="needs the old contract exited first")
    ws_db.set_project_status(proj_b, "done")

    issue = ws_db.get_issue(issue_id)
    score, reason, lesson_id = nba.score_issue(issue, time.time())

    assert not reason.startswith("No confirmation seen yet")


# --- task #374: sequence deviation notes are additive/descriptive only ----
# The detection itself (a real planted-missing-step case, and a no-false-
# positive case for a project that follows the typical chain) is exercised
# in tests/test_workgraph_sequences.py against workgraph_sequences.
# deviation_note_for_project directly - these two just confirm the WIRING
# into score_issue: the note (however it was computed) lands in nba_reason
# verbatim, and never changes priority_score.

def test_score_issue_appends_deviation_note_without_changing_score(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    ws_db.update_issue(issue_id, project_id="proj-1")
    issue = ws_db.get_issue(issue_id)
    now = time.time()
    note = ('Projects in "procurement" have historically also involved "contractpodai_review_requested" '
            'before "signature_requested_docusign" — seen in 2 of 2 completed "procurement" projects, '
            'no evidence of one here yet — worth checking, not a rule.')

    score_without, reason_without, _ = nba.score_issue(issue, now)
    score_with, reason_with, _ = nba.score_issue(
        issue, now, sequence_deviation_notes={"proj-1": note})

    assert score_with == score_without  # descriptive only - never moves the score
    assert note not in reason_without
    assert reason_with == reason_without + " · " + note


def test_score_issue_no_deviation_note_when_project_not_in_dict(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="X", state="active", category="other")
    ws_db.update_issue(issue_id, project_id="proj-1")
    issue = ws_db.get_issue(issue_id)
    now = time.time()

    score, reason, _ = nba.score_issue(
        issue, now, sequence_deviation_notes={"some-other-project": "irrelevant note"})

    assert "irrelevant note" not in reason


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


# --- recompute_issues (review point #7, settlement pass, 2026-08-11) -------

def test_recompute_issues_scores_only_the_requested_ids(ws_db):
    target = ws_db.create_issue_with_new_id(title="Sign this", state="active", category="other")
    untouched = ws_db.create_issue_with_new_id(title="Other issue", state="active", category="other")

    result = nba.recompute_issues([target])

    assert result["scored"] == 1
    assert ws_db.get_issue(target)["priority_score"] is not None
    assert ws_db.get_issue(untouched)["priority_score"] is None


def test_recompute_issues_matches_recompute_all_output_for_the_same_issue(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Sign this", state="active", category="other")
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k-ri", thread_key="k-ri", dedupe_key="k-ri",
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

    nba.recompute_issues([issue_id], now=1000.0)
    targeted = dict(ws_db.get_issue(issue_id))

    nba.recompute_all(now=1000.0)
    full = dict(ws_db.get_issue(issue_id))

    assert targeted["priority_score"] == full["priority_score"]
    assert targeted["nba_reason"] == full["nba_reason"]
    assert targeted["has_unmet_prerequisite"] == full["has_unmet_prerequisite"] == 1


def test_recompute_issues_skips_ids_that_are_now_closed(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Already done", state="active", category="other")
    ws_db.update_issue(issue_id, state="done")

    result = nba.recompute_issues([issue_id])

    assert result["scored"] == 0
    assert ws_db.get_issue(issue_id)["priority_score"] is None


def test_recompute_issues_empty_list_is_a_noop(ws_db):
    result = nba.recompute_issues([])
    assert result["scored"] == 0


def test_recompute_issues_does_not_reset_updated_at(ws_db):
    issue_id = ws_db.create_issue_with_new_id(title="Stale one", state="active", category="other")
    before = ws_db.get_issue(issue_id)["updated_at"]
    time.sleep(0.01)

    nba.recompute_issues([issue_id])

    after = ws_db.get_issue(issue_id)["updated_at"]
    assert after == before


# --- list_issue_ids_updated_since (review point #7) -------------------------

def test_list_issue_ids_updated_since_finds_recently_touched_issues(ws_db):
    older = ws_db.create_issue_with_new_id(title="Older", state="active", category="other")
    cutoff = time.time()
    time.sleep(0.01)
    newer = ws_db.create_issue_with_new_id(title="Newer", state="active", category="other")

    touched = ws_db.list_issue_ids_updated_since(cutoff)

    assert newer in touched
    assert older not in touched


def test_list_issue_ids_updated_since_excludes_nba_rescore_only_touches(ws_db):
    """The settlement pass must never see its OWN NBA rescoring (or the
    cycle's earlier recompute_all calls) as if it were real activity -
    recompute_all/recompute_issues always write with touch_updated_at=False
    for exactly this reason."""
    issue_id = ws_db.create_issue_with_new_id(title="Quiet issue", state="active", category="other")
    cutoff = time.time()
    time.sleep(0.01)

    nba.recompute_all()

    assert issue_id not in ws_db.list_issue_ids_updated_since(cutoff)


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


# --- enhancement idea panel #11: score gated issues lower ------------------

def _gated_issue(ws_db, title="Sign this"):
    """Same real-rule pattern as test_score_issue_prepends_aristotle_warning_
    when_unsatisfied - a genuinely gated issue, not a mocked check_prerequisites."""
    issue_id = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=f"gated-{title}", thread_key=f"gated-{title}", dedupe_key=f"gated-{title}",
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
    return issue_id


def test_score_issue_gated_scores_lower_than_ungated_twin(ws_db):
    now = time.time()
    gated_id = _gated_issue(ws_db, title="Gated")
    ungated_id = ws_db.create_issue_with_new_id(title="Ungated", state="active", category="other")

    gated_score, gated_reason, _ = nba.score_issue(ws_db.get_issue(gated_id), now)
    ungated_score, _, _ = nba.score_issue(ws_db.get_issue(ungated_id), now)

    assert gated_score < ungated_score
    assert gated_reason.startswith("No confirmation seen yet")


def test_score_issue_gated_downweight_is_multiplicative_not_flat(ws_db):
    """A higher-urgency gated issue should score higher than a lower-
    urgency gated issue, in the same proportion _GATED_ISSUE_DOWNWEIGHT
    would predict - proof the downweight scales with the issue's own
    urgency rather than applying a flat subtraction that could invert
    real priority ordering between two differently-urgent gated issues."""
    now = time.time()
    stale_gated_id = _gated_issue(ws_db, title="StaleGated")
    fresh_gated_id = _gated_issue(ws_db, title="FreshGated")
    conn = ws_db._connect()
    conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (now - 20 * nba.DAY, stale_gated_id))
    conn.commit()
    conn.close()

    stale_score, _, _ = nba.score_issue(ws_db.get_issue(stale_gated_id), now)
    fresh_score, _, _ = nba.score_issue(ws_db.get_issue(fresh_gated_id), now)

    assert stale_score > fresh_score  # real urgency difference survives the downweight


def test_gated_issue_downweight_constant_is_a_real_reduction():
    assert 0.0 < nba._GATED_ISSUE_DOWNWEIGHT < 1.0


# --- enhancement idea panel #12: ask density --------------------------------

def _open_ask_claim(ws_db, issue_id, raw_item_id, text="please send the SOW"):
    return ws_db.insert_claim(
        issue_id=issue_id, raw_item_id=raw_item_id, claim_type="ask", text=text,
        author="counterparty", author_basis="direction",
    )


def _issue_with_raw_item(ws_db, title, key):
    issue_id = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject="s", from_actor="a@example.com",
        participants_json="[]", body_preview="",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)
    return issue_id, row_id


def test_ask_density_for_issue_counts_only_ask_type_claims():
    open_claims = [
        {"claim_type": "ask"}, {"claim_type": "ask"},
        {"claim_type": "decision"}, {"claim_type": "commitment"},
    ]
    assert nba.ask_density_for_issue(open_claims) == 2


def test_ask_density_for_issue_zero_for_no_claims():
    assert nba.ask_density_for_issue([]) == 0


def test_apply_ask_density_boost_scales_with_count_and_caps():
    base = 0.5
    one_ask = nba._apply_ask_density_boost(base, 1)
    three_asks = nba._apply_ask_density_boost(base, 3)
    many_asks = nba._apply_ask_density_boost(base, 50)

    assert one_ask == base  # a single open ask is the normal case, no boost
    assert three_asks > one_ask
    # capped at _ASK_DENSITY_BOOST_MAX_ASKS extra asks beyond the first
    expected_cap = base + nba._ASK_DENSITY_BOOST_PER_ASK * nba._ASK_DENSITY_BOOST_MAX_ASKS
    assert many_asks == pytest.approx(expected_cap)
    assert many_asks <= 1.0


def test_score_issue_multi_ask_issue_scores_higher_than_single_ask_twin(ws_db):
    now = time.time()
    busy_id, busy_rid = _issue_with_raw_item(ws_db, "Busy", "ask-busy")
    quiet_id, quiet_rid = _issue_with_raw_item(ws_db, "Quiet", "ask-quiet")
    busy_claims = [
        _open_ask_claim(ws_db, busy_id, busy_rid, text=f"ask {i}") for i in range(4)
    ]
    quiet_claims = [_open_ask_claim(ws_db, quiet_id, quiet_rid)]

    busy_open = ws_db.list_open_claims_for_issue(busy_id, claim_type="ask")
    quiet_open = ws_db.list_open_claims_for_issue(quiet_id, claim_type="ask")

    busy_score, busy_reason, _ = nba.score_issue(
        ws_db.get_issue(busy_id), now, open_claims=busy_open)
    quiet_score, quiet_reason, _ = nba.score_issue(
        ws_db.get_issue(quiet_id), now, open_claims=quiet_open)

    assert busy_score > quiet_score
    assert "4 open asks" in busy_reason
    assert "open asks" not in quiet_reason


def test_score_issue_ask_reason_omitted_below_threshold(ws_db):
    now = time.time()
    issue_id, rid = _issue_with_raw_item(ws_db, "TwoAsks", "ask-two")
    _open_ask_claim(ws_db, issue_id, rid, text="ask 1")
    _open_ask_claim(ws_db, issue_id, rid, text="ask 2")
    open_claims = ws_db.list_open_claims_for_issue(issue_id, claim_type="ask")

    _score, reason, _ = nba.score_issue(ws_db.get_issue(issue_id), now, open_claims=open_claims)

    assert "open asks" not in reason  # 2 asks is below the >= 3 reason threshold


def test_score_issue_handles_missing_open_claims_gracefully(ws_db):
    now = time.time()
    issue_id, _rid = _issue_with_raw_item(ws_db, "NoClaimsArg", "ask-none")

    score, reason, _ = nba.score_issue(ws_db.get_issue(issue_id), now)

    assert score >= 0.0
    assert "open asks" not in reason


# --- enhancement idea panel #13: attached-document value corroboration -----

def test_dollar_values_in_text_finds_multiple_figures_with_suffixes():
    values = nba._dollar_values_in_text("Total is $2.5 million, deposit $10,000")
    assert values == {2_500_000.0, 10_000.0}


def test_dollar_values_in_text_empty_for_no_text():
    assert nba._dollar_values_in_text(None) == set()
    assert nba._dollar_values_in_text("") == set()


def test_attachment_corroborates_value_false_below_value_floor():
    assert nba.attachment_corroborates_value([{"id": 1}], 500.0) is False  # below _VALUE_FLOOR


def test_attachment_corroborates_value_true_when_attachment_has_exact_figure(ws_db):
    item = {"id": 300}
    ws_db.create_attachment(
        entity_type="raw_item", entity_id="300", kind="reference", filename="order_form.pdf",
        stored_path="raw_items/300/order_form.pdf", content_type=None, size_bytes=1234,
        sha256_hex="corrob1", uploaded_by="test",
        extracted_text="The total contract value is $30,500,000 for this term.",
    )

    assert nba.attachment_corroborates_value([item], 30_500_000.0) is True


def test_attachment_corroborates_value_false_when_figures_dont_match(ws_db):
    item = {"id": 301}
    ws_db.create_attachment(
        entity_type="raw_item", entity_id="301", kind="reference", filename="order_form.pdf",
        stored_path="raw_items/301/order_form.pdf", content_type=None, size_bytes=1234,
        sha256_hex="corrob2", uploaded_by="test",
        extracted_text="A completely unrelated figure: $999.",
    )

    assert nba.attachment_corroborates_value([item], 30_500_000.0) is False


def test_attachment_corroborates_value_false_with_no_attachments(ws_db):
    item = {"id": 302}
    assert nba.attachment_corroborates_value([item], 30_500_000.0) is False


def _issue_with_value_and_attachment(ws_db, title, key, amount_text, attach_extracted_text=None):
    issue_id = ws_db.create_issue_with_new_id(title=title, state="active", category="other")
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=time.time(), subject=amount_text, from_actor="a@example.com",
        participants_json="[]", body_preview="",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)
    if attach_extracted_text is not None:
        ws_db.create_attachment(
            entity_type="raw_item", entity_id=str(row_id), kind="reference", filename="doc.pdf",
            stored_path=f"raw_items/{row_id}/doc.pdf", content_type=None, size_bytes=1,
            sha256_hex=f"corrob-{key}", uploaded_by="test", extracted_text=attach_extracted_text,
        )
    return issue_id


def test_score_issue_corroborated_value_scores_higher_than_uncorroborated_twin(ws_db):
    now = time.time()
    corroborated_id = _issue_with_value_and_attachment(
        ws_db, "Corroborated", "corrob-yes", "Total contract value is $5,000,000",
        attach_extracted_text="Order form total: $5,000,000",
    )
    uncorroborated_id = _issue_with_value_and_attachment(
        ws_db, "Uncorroborated", "corrob-no", "Total contract value is $5,000,000",
    )

    corroborated_score, corroborated_reason, _ = nba.score_issue(ws_db.get_issue(corroborated_id), now)
    uncorroborated_score, uncorroborated_reason, _ = nba.score_issue(ws_db.get_issue(uncorroborated_id), now)

    assert corroborated_score > uncorroborated_score
    assert "value confirmed by attachment" in corroborated_reason
    assert "value confirmed by attachment" not in uncorroborated_reason


def test_value_corroboration_boost_constant_is_small_and_bounded():
    assert 0.0 < nba._VALUE_CORROBORATION_BOOST < 0.2


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


def test_candidate_actions_nba_surface_is_type_aware_for_known_signal(monkeypatch):
    # Task #233: the top-line "nba" candidate used to be blind to what kind
    # of issue this actually is - always "Draft a reply" for an active
    # issue, even one whose most recent evidence is a structured Ariba
    # approval notification (nobody drafts an email reply to Ariba). The
    # most recent evidence row's signal_type now wins over the generic
    # state-based label when this system has a real action for it.
    issue = {"nba_reason": "your move · $53,702,143.00", "state": "active", "priority_score": 0.7}
    evidence = [{"signal_type": "ariba_pr_approval_needed", "ts": 200}]
    result = nba.candidate_actions(issue, evidence)
    nba_candidate = next(c for c in result if c["source_surface"] == "nba")
    assert nba_candidate["kind"] == "approve_requisition"
    assert nba_candidate["label"] == "Approve or reject in Ariba"
    # rationale still comes from the issue's own real urgency reasoning,
    # unchanged - only the kind/label were type-corrected.
    assert nba_candidate["rationale"] == "your move · $53,702,143.00"


def test_candidate_actions_nba_surface_ignores_unmapped_signal_type():
    # A signal_type this system has no specific action for (or none at all)
    # must fall back to the original state-based nudge/draft_reply behavior
    # exactly as before - this is additive, not a replacement.
    issue = {"nba_reason": "waiting on vendor", "state": "waiting", "priority_score": 0.4}
    evidence = [{"signal_type": None, "ts": 200}]
    result = nba.candidate_actions(issue, evidence)
    nba_candidate = next(c for c in result if c["source_surface"] == "nba")
    assert nba_candidate["kind"] == "nudge"


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


def test_extract_item_candidates_survives_bare_comma_amount():
    r"""Task #414 (2026-08-21): real live crash. _DOLLAR_RE's amount group is
    [\d,]+, which matches a BARE COMMA, so "total $, TBD" produced
    group(1) == "," and ",".replace(",", "") == "" -> float("") -> ValueError.
    The old guard only skipped `is None`.

    This mattered well beyond NBA: value_amount_for_issue is called from
    workgraph_projects.compute_work_object_signature, which find_candidates
    calls for every candidate, so one such string in any raw_item body or
    attachment extracted_text crashed GROUPING for that work object. Empty
    currency cells in text extracted from PDF/DOCX tables render exactly
    this way."""
    for text in ("total $, TBD", "$ , pending", "$,, blank", "$ ,"):
        assert nba._extract_item_candidates({"subject": text, "body_preview": "", "id": None}) == [], text


def test_extract_item_candidates_still_parses_real_amounts():
    """The guard must not swallow genuine values."""
    got = nba._extract_item_candidates(
        {"subject": "Contract value $1,200,000 firm", "body_preview": "", "id": None})
    assert [round(c[0]) for c in got] == [1200000]
    got = nba._extract_item_candidates(
        {"subject": "$2.5 million ceiling", "body_preview": "", "id": None})
    assert [round(c[0]) for c in got] == [2500000]
