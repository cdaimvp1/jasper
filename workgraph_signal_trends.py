"""
workgraph_signal_trends.py — task #66: month-over-month counts of
recognized automated signals (workgraph_signals.classify_signal's output,
stored on raw_items.signal_type at ingest). Lets Marc see whether, say,
Ariba PR-approval volume is trending up or down over time, not just this
morning's queue. Zero LLM, pure aggregation over an already-stored,
deterministic field - same discipline as workgraph_aristotle.py's
detection and workgraph_nba.py's scoring.
"""
from __future__ import annotations

import datetime

import workgraph_store as ws

DEFAULT_MONTHS = 6


def _last_n_month_keys(now: float, n: int) -> list[str]:
    """["2026-02", "2026-03", ..., "2026-07"] - the last n calendar months
    INCLUDING the current one, oldest first, UTC (occurred_ts is itself a
    UTC epoch - bucketing in UTC avoids the local-timezone-near-a-boundary
    drift already named/fixed elsewhere in this codebase, e.g. workgraph_
    nba._due_urgency). Computed independently of what data actually
    exists, so a month with zero activity for every signal type still
    appears as a real "nothing happened" fact, not a gap that looks like
    missing data."""
    now_dt = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
    keys = []
    year, month = now_dt.year, now_dt.month
    for _ in range(n):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(keys))


def monthly_signal_trends(now: float, months: int = DEFAULT_MONTHS) -> dict:
    """Returns {"months": [...], "series": {signal_type: [count, ...]}} -
    every series list is the same length as `months`, zero-filled for any
    month with no occurrences of that signal type. Only signal types that
    appear at least once anywhere in the window get a series - an
    all-zero row for a type that's simply never fired isn't useful trend
    information."""
    month_keys = _last_n_month_keys(now, months)
    since_dt = datetime.datetime.strptime(month_keys[0], "%Y-%m").replace(tzinfo=datetime.timezone.utc)
    since_ts = since_dt.timestamp()
    rows = ws.count_raw_items_by_month_and_signal_type(since_ts)

    month_index = {m: i for i, m in enumerate(month_keys)}
    series: dict[str, list[int]] = {}
    for row in rows:
        month = row["month"]
        if month not in month_index:
            continue  # occurred_ts >= since_ts guarantees this in practice; defensive only
        signal_type = row["signal_type"]
        series.setdefault(signal_type, [0] * months)
        series[signal_type][month_index[month]] = row["count"]

    return {"months": month_keys, "series": series}
