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
    assert nba._extract_item_value(item) == 3_000_000.0


def test_billion_suffix_recognized():
    item = {"id": 2, "subject": "This is a $1.2 billion contract", "body_preview": ""}
    assert nba._extract_item_value(item) == 1_200_000_000.0


def test_value_cache_avoids_recomputation():
    item_v1 = {"id": 42, "subject": "Worth $2.5 million", "body_preview": ""}
    v1 = nba._extract_item_value(item_v1)
    assert v1 == 2_500_000.0

    # same id, DIFFERENT text - cache should still return the ORIGINAL value
    item_v2 = {"id": 42, "subject": "Now says $999 billion", "body_preview": ""}
    v2 = nba._extract_item_value(item_v2)
    assert v2 == v1, "cache was not used - recomputed from new text for a known id"


def test_default_weights_is_immutable():
    with pytest.raises(TypeError):
        nba.DEFAULT_WEIGHTS["value"] = 999


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
