"""Regression tests for workgraph_signal_trends.py (task #66, month-over-
month signal trend view). Pure aggregation over raw_items.signal_type -
zero LLM, no interpretation, so tests use a fixed reference `now` to avoid
month-boundary flakiness rather than time.time()."""
from __future__ import annotations

import datetime

import workgraph_signal_trends as wst

_NOW = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc).timestamp()


def _ts_in_month(year: int, month: int, day: int = 10) -> float:
    return datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc).timestamp()


def _set_signal_type(ws_db, raw_item_id: int, signal_type: str) -> None:
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", (signal_type, raw_item_id))
    conn.close()


def _insert(ws_db, key: str, occurred_ts: float, signal_type: str | None) -> int:
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=occurred_ts, subject="s", from_actor="a@example.com", participants_json="[]",
    )
    if signal_type:
        _set_signal_type(ws_db, rid, signal_type)
    return rid


def test_last_n_month_keys_includes_current_month_last():
    keys = wst._last_n_month_keys(_NOW, 3)
    assert keys == ["2026-05", "2026-06", "2026-07"]


def test_last_n_month_keys_crosses_year_boundary():
    jan = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc).timestamp()
    keys = wst._last_n_month_keys(jan, 3)
    assert keys == ["2025-11", "2025-12", "2026-01"]


def test_monthly_signal_trends_empty_when_no_signals(ws_db):
    trends = wst.monthly_signal_trends(_NOW, months=3)
    assert trends["months"] == ["2026-05", "2026-06", "2026-07"]
    assert trends["series"] == {}


def test_monthly_signal_trends_buckets_current_month(ws_db):
    _insert(ws_db, "k1", _ts_in_month(2026, 7), "ariba_pr_approval_needed")

    trends = wst.monthly_signal_trends(_NOW, months=3)

    assert trends["series"]["ariba_pr_approval_needed"] == [0, 0, 1]


def test_monthly_signal_trends_buckets_past_month_within_window(ws_db):
    _insert(ws_db, "k2", _ts_in_month(2026, 5), "signature_requested_docusign")

    trends = wst.monthly_signal_trends(_NOW, months=3)

    assert trends["series"]["signature_requested_docusign"] == [1, 0, 0]


def test_monthly_signal_trends_excludes_months_outside_window(ws_db):
    _insert(ws_db, "k3", _ts_in_month(2026, 1), "ariba_pr_approval_needed")  # 6 months before window

    trends = wst.monthly_signal_trends(_NOW, months=3)

    assert trends["series"] == {}


def test_monthly_signal_trends_excludes_items_with_no_signal_type(ws_db):
    _insert(ws_db, "k4", _ts_in_month(2026, 7), None)

    trends = wst.monthly_signal_trends(_NOW, months=3)

    assert trends["series"] == {}


def test_monthly_signal_trends_multiple_signal_types_independent_series(ws_db):
    _insert(ws_db, "k5", _ts_in_month(2026, 6), "ariba_pr_approval_needed")
    _insert(ws_db, "k6", _ts_in_month(2026, 6), "ariba_pr_approval_needed")
    _insert(ws_db, "k7", _ts_in_month(2026, 7), "signature_requested")

    trends = wst.monthly_signal_trends(_NOW, months=3)

    assert trends["series"]["ariba_pr_approval_needed"] == [0, 2, 0]
    assert trends["series"]["signature_requested"] == [0, 0, 1]


def test_monthly_signal_trends_respects_months_param(ws_db):
    _insert(ws_db, "k8", _ts_in_month(2026, 3), "ariba_pr_approval_needed")

    trends6 = wst.monthly_signal_trends(_NOW, months=6)
    assert trends6["months"] == ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]
    assert trends6["series"]["ariba_pr_approval_needed"][1] == 1

    trends3 = wst.monthly_signal_trends(_NOW, months=3)
    assert trends3["series"] == {}  # March falls outside a 3-month window from July
