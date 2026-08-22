"""Tests for workgraph_awayreport.py (task #372, "what changed while I was
away?"). Zero LLM in the module under test, so these are all real reads
against a real isolated DB - no mocking at all.
"""
from __future__ import annotations

import time

import workgraph_awayreport as away

HOUR = 3600.0


def _issue(ws_db, title, state="active"):
    return ws_db.create_issue_with_new_id(title=title, category="other", state=state)


def _item(ws_db, issue_id, subject, ts, key=None, source="outlook_mail"):
    rid = ws_db.insert_raw_item(
        source=source, stable_key=key or subject, thread_key=key or subject,
        dedupe_key=key or subject, occurred_ts=ts, subject=subject,
        from_actor="rep@vendor.com", participants_json="[]", body_preview=subject,
    )
    if issue_id:
        ws_db.link_raw_item_to_issue(rid, issue_id)
    return rid


def test_empty_window_reports_zeroes_not_an_error(ws_db):
    now = time.time()
    s = away.build_away_summary(now - 24 * HOUR, now=now)
    assert s["counts"]["new_items"] == 0
    assert s["activity"] == []
    assert s["closed"] == [] and s["reopened"] == []


def test_window_block_states_the_span_it_actually_covers(ws_db):
    """A report must never misrepresent how far back it looked."""
    now = 1_800_000_000.0
    s = away.build_away_summary(now - 48 * HOUR, now=now)
    assert s["window"]["since_ts"] == now - 48 * HOUR
    assert s["window"]["until_ts"] == now
    assert s["window"]["hours"] == 48.0


def test_new_evidence_is_grouped_per_issue_with_counts_and_sources(ws_db):
    now = time.time()
    a = _issue(ws_db, "Acme renewal")
    _item(ws_db, a, "Acme msg 1", now - 2 * HOUR, key="k1")
    _item(ws_db, a, "Acme msg 2", now - 1 * HOUR, key="k2", source="teams")

    s = away.build_away_summary(now - 24 * HOUR, now=now)

    assert s["counts"]["new_items"] == 2
    assert s["counts"]["issues_with_new_items"] == 1
    row = s["activity"][0]
    assert row["issue_id"] == a
    assert row["new_item_count"] == 2
    assert row["sources"] == ["outlook_mail", "teams"]


def test_evidence_older_than_the_window_is_excluded(ws_db):
    now = time.time()
    a = _issue(ws_db, "Old thread")
    _item(ws_db, a, "ancient", now - 500 * HOUR, key="old")
    _item(ws_db, a, "recent", now - 1 * HOUR, key="new")

    s = away.build_away_summary(now - 24 * HOUR, now=now)
    assert s["counts"]["new_items"] == 1
    assert s["activity"][0]["new_item_count"] == 1


def test_activity_is_ordered_by_recency_not_by_any_invented_priority(ws_db):
    """The module deliberately does no ranking - time order only."""
    now = time.time()
    a = _issue(ws_db, "Older activity")
    b = _issue(ws_db, "Newer activity")
    _item(ws_db, a, "a1", now - 5 * HOUR, key="a1")
    _item(ws_db, b, "b1", now - 1 * HOUR, key="b1")

    s = away.build_away_summary(now - 24 * HOUR, now=now)
    assert [r["issue_id"] for r in s["activity"]] == [b, a]


def test_a_closure_during_the_window_is_reported(ws_db):
    """The whole reason this module needs its own state reader: an issue that
    CLOSED while away is invisible to list_issue_ids_updated_since, which
    filters to open states only."""
    now = time.time()
    a = _issue(ws_db, "Finished while away")
    ws_db.update_issue(a, state="done", actor="test")

    s = away.build_away_summary(now - 24 * HOUR, now=now + HOUR)

    assert s["counts"]["closed"] == 1
    assert s["closed"][0]["issue_id"] == a
    assert s["closed"][0]["to_state"] == "done"
    assert s["closed"][0]["title"] == "Finished while away"


def test_a_reopen_during_the_window_is_reported_separately(ws_db):
    now = time.time()
    a = _issue(ws_db, "Came back to life")
    ws_db.update_issue(a, state="done", actor="test")
    ws_db.update_issue(a, state="active", actor="test")

    s = away.build_away_summary(now - 24 * HOUR, now=now + HOUR)

    assert s["counts"]["reopened"] == 1
    assert s["reopened"][0]["issue_id"] == a


def test_two_transitions_in_one_window_are_both_kept(ws_db):
    """issues.state can only show the final resting place - reading the
    history table is what makes a round trip visible at all."""
    now = time.time()
    a = _issue(ws_db, "Bounced")
    ws_db.update_issue(a, state="waiting", actor="test")
    ws_db.update_issue(a, state="active", actor="test")

    s = away.build_away_summary(now - 24 * HOUR, now=now + HOUR)
    moves = [(c["from_state"], c["to_state"]) for c in s["state_changes"] if c["issue_id"] == a]
    assert ("active", "waiting") in moves
    assert ("waiting", "active") in moves


def test_ungrouped_arrivals_are_surfaced_not_silently_dropped(ws_db):
    """Evidence that landed but isn't attached to any issue yet is a real gap
    in the report's own coverage; hiding it would imply full accounting."""
    now = time.time()
    _item(ws_db, None, "Nobody's message", now - 2 * HOUR, key="orphan")

    s = away.build_away_summary(now - 24 * HOUR, now=now)

    assert s["counts"]["ungrouped_new_items"] == 1
    assert s["counts"]["issues_with_new_items"] == 0
    assert s["ungrouped_new_items"][0]["subject"] == "Nobody's message"


def test_the_report_writes_nothing_and_is_repeatable(ws_db):
    """Calling it must not consume its own backlog - 'let me look again' has
    to keep working."""
    now = time.time()
    a = _issue(ws_db, "Repeatable")
    _item(ws_db, a, "msg", now - 1 * HOUR, key="rep")

    first = away.build_away_summary(now - 24 * HOUR, now=now)
    second = away.build_away_summary(now - 24 * HOUR, now=now)
    assert first == second


def _claim(ws_db, issue_id, raw_item_id, text, first_seen_ts, claim_type="ask"):
    conn = ws_db._connect()
    conn.execute(
        """INSERT INTO claims (issue_id, raw_item_id, claim_type, text, author,
                               author_basis, status, first_seen_ts, last_seen_ts)
           VALUES (?, ?, ?, ?, 'counterparty', 'direction', 'open', ?, ?)""",
        (issue_id, raw_item_id, claim_type, text, first_seen_ts, first_seen_ts))
    conn.commit()
    conn.close()


def test_a_backfill_is_not_reported_as_new_asks(ws_db):
    """THE trap this module exists to avoid, measured live 2026-08-22: of
    9,097 claims first-seen in 14 days, 6,709 were materialized in one
    three-hour backfill and 94% sat on evidence older than the window. Keying
    on claims.first_seen_ts would have told Marc ~940 new asks landed on him
    while he was out. A claim is new to the window only if the MESSAGE
    carrying it arrived in the window."""
    now = time.time()
    a = _issue(ws_db, "Long-running thread")
    # The realistic mixed case, and the one that actually discriminates: the
    # issue DID see new mail in the window, so it is genuinely in the report -
    # and separately, a catch-up sweep extracted a claim from evidence months
    # older. Only the second must be kept out of new_asks.
    _item(ws_db, a, "unrelated new message", now - 2 * HOUR, key="fresh")
    old_item = _item(ws_db, a, "message from months ago", now - 1000 * HOUR, key="old")
    _claim(ws_db, a, old_item, "please send the signed SOW", first_seen_ts=now - 1 * HOUR)

    s = away.build_away_summary(now - 24 * HOUR, now=now)

    assert s["counts"]["new_asks"] == 0, "a backfilled claim is not a new ask"
    assert s["counts"]["claims_materialized_from_older_evidence"] == 1
    assert s["new_claims"]["ask"] == []
    # ...and the issue itself is still reported, on the strength of the real
    # new message. Suppressing the claim must not suppress the activity.
    assert s["counts"]["issues_with_new_items"] == 1


def test_a_claim_on_evidence_that_really_arrived_is_reported(ws_db):
    """The other half: don't over-correct into reporting nothing."""
    now = time.time()
    a = _issue(ws_db, "Live thread")
    fresh = _item(ws_db, a, "new message", now - 2 * HOUR, key="fresh")
    _claim(ws_db, a, fresh, "can you approve PR1164442 today?", first_seen_ts=now - 1 * HOUR)

    s = away.build_away_summary(now - 24 * HOUR, now=now)

    assert s["counts"]["new_asks"] == 1
    assert s["counts"]["claims_materialized_from_older_evidence"] == 0
    assert s["new_claims"]["ask"][0]["text"] == "can you approve PR1164442 today?"
    assert s["new_claims"]["ask"][0]["raw_item_id"] == fresh


def test_claim_types_are_reported_separately(ws_db):
    """An ask (someone waiting on you) reads very differently from a date."""
    now = time.time()
    a = _issue(ws_db, "Mixed")
    r = _item(ws_db, a, "msg", now - 2 * HOUR, key="mix")
    _claim(ws_db, a, r, "send the redline", now - HOUR, claim_type="ask")
    _claim(ws_db, a, r, "renewal lands 30 Sep", now - HOUR, claim_type="date")

    s = away.build_away_summary(now - 24 * HOUR, now=now)
    assert s["counts"]["new_asks"] == 1
    assert s["counts"]["new_dates"] == 1
    assert s["counts"]["new_commitments"] == 0


def test_hours_wrapper_matches_the_explicit_timestamp_form(ws_db):
    now = 1_800_000_000.0
    a = _issue(ws_db, "Same either way")
    _item(ws_db, a, "msg", now - 2 * HOUR, key="w")
    assert (away.build_away_summary_for_hours(24.0, now=now)
            == away.build_away_summary(now - 24 * HOUR, now=now))
