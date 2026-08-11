"""Tests for workgraph_nba.py's task #318 additions: the rewrite-severity
correlation/judgment block (_find_likely_sent_reply, classify_rewrite_
severity, attempt_rewrite_judgment, run_rewrite_judgment_daily_if_due).
The deterministic accept/dismiss logging itself lives entirely in
server_lean.py's action routes and workgraph_store.py's nba_outcome_log
functions (see test_workgraph_store.py) - nothing to unit-test here that
isn't already a store-level test, since the routes just call
ws.create_nba_outcome_event with no judgment of their own."""
from __future__ import annotations

import time

import workgraph_nba as nba


def _issue(ws_db, title="Issue", state="active", category="other"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category=category)


def _raw_item(ws_db, issue_id, key, direction="outbound", occurred_ts=None):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=occurred_ts if occurred_ts is not None else time.time(),
        subject="s", from_actor="marc", participants_json="[]",
        body_preview="the actually sent text",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET direction = ? WHERE id = ?", (direction, rid))
    conn.close()
    return rid


# --- _find_likely_sent_reply -----------------------------------------------

def test_find_likely_sent_reply_finds_outbound_item_after_ts(ws_db):
    issue_id = _issue(ws_db)
    after_ts = time.time()
    rid = _raw_item(ws_db, issue_id, "k1", direction="outbound", occurred_ts=after_ts + 60)

    found = nba._find_likely_sent_reply(issue_id, after_ts)

    assert found is not None
    assert found["id"] == rid


def test_find_likely_sent_reply_ignores_inbound_items(ws_db):
    issue_id = _issue(ws_db)
    after_ts = time.time()
    _raw_item(ws_db, issue_id, "k1", direction="inbound", occurred_ts=after_ts + 60)

    assert nba._find_likely_sent_reply(issue_id, after_ts) is None


def test_find_likely_sent_reply_ignores_items_before_the_cutoff(ws_db):
    issue_id = _issue(ws_db)
    after_ts = time.time()
    _raw_item(ws_db, issue_id, "k1", direction="outbound", occurred_ts=after_ts - 60)

    assert nba._find_likely_sent_reply(issue_id, after_ts) is None


def test_find_likely_sent_reply_ignores_items_outside_the_window(ws_db):
    issue_id = _issue(ws_db)
    after_ts = time.time()
    _raw_item(ws_db, issue_id, "k1", direction="outbound",
              occurred_ts=after_ts + nba.SENT_TEXT_CORRELATION_WINDOW_SECONDS + 60)

    assert nba._find_likely_sent_reply(issue_id, after_ts) is None


def test_find_likely_sent_reply_picks_the_closest_when_several(ws_db):
    issue_id = _issue(ws_db)
    after_ts = time.time()
    _raw_item(ws_db, issue_id, "far", direction="outbound", occurred_ts=after_ts + 3600)
    near_id = _raw_item(ws_db, issue_id, "near", direction="outbound", occurred_ts=after_ts + 60)

    found = nba._find_likely_sent_reply(issue_id, after_ts)

    assert found["id"] == near_id


# --- classify_rewrite_severity ----------------------------------------------

def test_classify_rewrite_severity_identical_text_is_zero_severity():
    result = nba.classify_rewrite_severity("Same text here.", "Same text here.")
    assert result["severity"] == 0.0


def test_classify_rewrite_severity_wildly_different_text_is_high_severity():
    result = nba.classify_rewrite_severity(
        "Here is a short status update on the open asks.",
        "Completely unrelated sentence about a totally different topic entirely.",
    )
    assert result["severity"] > 0.5


def test_classify_rewrite_severity_uses_judge_fn_when_given():
    sentinel = {"severity": 0.42, "note": "custom judge"}
    result = nba.classify_rewrite_severity("a", "b", judge_fn=lambda s, t: sentinel)
    assert result == sentinel


# --- attempt_rewrite_judgment ------------------------------------------------

def test_attempt_rewrite_judgment_records_a_real_correlate(ws_db):
    issue_id = _issue(ws_db)
    now = time.time()
    event_id = ws_db.create_nba_outcome_event(
        issue_id=issue_id, action_kind="hero_draft_reply", outcome="accepted_as_is",
        suggested_text="the actually sent text",
    )
    _raw_item(ws_db, issue_id, "sent1", direction="outbound", occurred_ts=now + 120)

    result = nba.attempt_rewrite_judgment(now=now)

    assert result == {"judged": 1, "abandoned": 0, "still_pending": 0}
    row = ws_db.get_nba_outcome_event(event_id)
    assert row["sent_text"] == "the actually sent text"
    assert row["outcome"] == "accepted_as_is"  # identical text - below the rewrite threshold
    assert row["rewrite_severity"] == 0.0


def test_attempt_rewrite_judgment_flips_to_rewritten_above_threshold(ws_db):
    issue_id = _issue(ws_db)
    now = time.time()
    ws_db.create_nba_outcome_event(
        issue_id=issue_id, action_kind="hero_draft_reply", outcome="accepted_as_is",
        suggested_text="Here is a short status update on the open asks for this thread.",
    )
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="sent2", thread_key="sent2", dedupe_key="sent2",
        occurred_ts=now + 120, subject="s", from_actor="marc", participants_json="[]",
        body_preview="Completely unrelated final wording that shares almost nothing with the draft.",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET direction = 'outbound' WHERE id = ?", (rid,))
    conn.close()

    result = nba.attempt_rewrite_judgment(now=now)

    assert result["judged"] == 1
    outcomes = ws_db.list_nba_outcomes(action_kind="hero_draft_reply")
    assert outcomes[0]["outcome"] == "rewritten"
    assert outcomes[0]["rewrite_severity"] >= nba.REWRITE_SEVERITY_THRESHOLD


def test_attempt_rewrite_judgment_leaves_recent_rows_pending_with_no_correlate(ws_db):
    issue_id = _issue(ws_db)
    now = time.time()
    ws_db.create_nba_outcome_event(
        issue_id=issue_id, action_kind="hero_draft_reply", outcome="accepted_as_is",
        suggested_text="Draft body",
    )

    result = nba.attempt_rewrite_judgment(now=now)

    assert result == {"judged": 0, "abandoned": 0, "still_pending": 1}


def test_attempt_rewrite_judgment_abandons_old_rows_with_no_correlate(ws_db):
    issue_id = _issue(ws_db)
    now = time.time()
    event_id = ws_db.create_nba_outcome_event(
        issue_id=issue_id, action_kind="hero_draft_reply", outcome="accepted_as_is",
        suggested_text="Draft body",
    )
    conn = ws_db._connect()
    conn.execute(
        "UPDATE nba_outcome_log SET detected_ts = ? WHERE id = ?",
        (now - nba.SENT_TEXT_CORRELATION_WINDOW_SECONDS - 3600, event_id),
    )
    conn.close()

    result = nba.attempt_rewrite_judgment(now=now)

    assert result == {"judged": 0, "abandoned": 1, "still_pending": 0}
    row = ws_db.get_nba_outcome_event(event_id)
    assert row["sent_text"] == ""
    assert row["outcome"] == "accepted_as_is"
    assert row["rewrite_severity"] is None


def test_attempt_rewrite_judgment_ignores_rows_with_no_suggested_text(ws_db):
    """Plain draft_reply/draft_forward rows never carry suggested_text -
    list_nba_outcomes_pending_rewrite_judgment already filters these out at
    the store layer (see its own test), confirmed here end-to-end too."""
    issue_id = _issue(ws_db)
    now = time.time()
    ws_db.create_nba_outcome_event(issue_id=issue_id, action_kind="draft_reply", outcome="accepted_as_is")

    result = nba.attempt_rewrite_judgment(now=now)

    assert result == {"judged": 0, "abandoned": 0, "still_pending": 0}


# --- run_rewrite_judgment_daily_if_due --------------------------------------

def test_run_rewrite_judgment_daily_if_due_gates_second_call_same_day(ws_db):
    issue_id = _issue(ws_db)
    now = time.time()
    ws_db.create_nba_outcome_event(
        issue_id=issue_id, action_kind="hero_draft_reply", outcome="accepted_as_is",
        suggested_text="Draft body",
    )

    first = nba.run_rewrite_judgment_daily_if_due(now=now)
    assert first is not None
    assert first["still_pending"] == 1

    second = nba.run_rewrite_judgment_daily_if_due(now=now)
    assert second is None
