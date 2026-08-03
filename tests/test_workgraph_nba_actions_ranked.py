"""Tests for workgraph_nba.py's NBA v2 additions (design doc Section 11,
Phase 4): score_claim and rank_actions - the global, claims-backed action
ranking, additive and separate from score_issue/recompute_all/
candidate_actions (untouched by this work, per Section 11.5)."""
from __future__ import annotations

import time

import workgraph_claims
import workgraph_nba as nba


def _issue(ws_db, title="Issue", state="active", category="other"):
    return ws_db.create_issue_with_new_id(title=title, state=state, category=category)


def _raw_item(ws_db, issue_id, key, extracted_json, direction="inbound", occurred_ts=None):
    rid = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=occurred_ts if occurred_ts is not None else time.time(),
        subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid, issue_id)
    ws_db.create_extraction(rid, __import__("json").dumps(extracted_json))
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET direction = ? WHERE id = ?", (direction, rid))
    conn.close()
    workgraph_claims.materialize_claims_for_raw_item(rid)
    return rid


# --- score_claim -----------------------------------------------------------

def test_score_claim_fresh_claim_low_staleness(ws_db):
    now = time.time()
    claim = {"first_seen_ts": now, "escalated": 0}
    score, reason = nba.score_claim(claim, date_urgency=0.0, value_urgency_score=0.0, now=now)
    assert score < 0.05
    assert reason == "open"


def test_score_claim_stale_claim_scores_higher(ws_db):
    now = time.time()
    fresh = {"first_seen_ts": now, "escalated": 0}
    stale = {"first_seen_ts": now - 20 * nba.DAY, "escalated": 0}
    fresh_score, _ = nba.score_claim(fresh, date_urgency=0.0, value_urgency_score=0.0, now=now)
    stale_score, reason = nba.score_claim(stale, date_urgency=0.0, value_urgency_score=0.0, now=now)
    assert stale_score > fresh_score
    assert "open 20d" in reason


def test_score_claim_escalation_bonus_applied(ws_db):
    now = time.time()
    plain = {"first_seen_ts": now - nba.DAY, "escalated": 0}
    escalated = {"first_seen_ts": now - nba.DAY, "escalated": 1}
    plain_score, _ = nba.score_claim(plain, date_urgency=0.0, value_urgency_score=0.0, now=now)
    escalated_score, reason = nba.score_claim(escalated, date_urgency=0.0, value_urgency_score=0.0, now=now)
    assert escalated_score > plain_score
    assert "escalated" in reason


def test_score_claim_hard_date_urgency_beats_soft(ws_db):
    now = time.time()
    claim = {"first_seen_ts": now, "escalated": 0}
    hard_score, hard_reason = nba.score_claim(claim, date_urgency=1.0, value_urgency_score=0.0, now=now)
    soft_score, soft_reason = nba.score_claim(claim, date_urgency=0.5, value_urgency_score=0.0, now=now)
    assert hard_score > soft_score
    assert "hard deadline" in hard_reason
    assert "soft deadline" in soft_reason


def test_score_claim_value_urgency_contributes(ws_db):
    now = time.time()
    claim = {"first_seen_ts": now, "escalated": 0}
    low_value, _ = nba.score_claim(claim, date_urgency=0.0, value_urgency_score=0.0, now=now)
    high_value, _ = nba.score_claim(claim, date_urgency=0.0, value_urgency_score=1.0, now=now)
    assert high_value > low_value


# --- _issue_date_urgency ---------------------------------------------------

def test_issue_date_urgency_ignores_owner():
    """Task #57's fix, made concrete: a hard date claim counts in full
    regardless of who it's owned by - owner never enters this function."""
    marc_owned = [{"date_kind": "hard", "owner": "marc"}]
    counterparty_owned = [{"date_kind": "hard", "owner": "counterparty"}]
    assert nba._issue_date_urgency(marc_owned) == nba._issue_date_urgency(counterparty_owned) == 1.0


def test_issue_date_urgency_takes_the_max_across_claims():
    claims = [{"date_kind": "soft"}, {"date_kind": "hard"}, {"date_kind": None}]
    assert nba._issue_date_urgency(claims) == 1.0


def test_issue_date_urgency_zero_when_no_dates():
    assert nba._issue_date_urgency([]) == 0.0


# --- rank_actions ------------------------------------------------------

def test_rank_actions_empty_when_no_open_claims(ws_db):
    _issue(ws_db)
    assert nba.rank_actions() == []


def test_rank_actions_includes_marc_owed_ask(ws_db):
    iid = _issue(ws_db, "Renewal")
    _raw_item(ws_db, iid, "r1", {"asks": ["can you approve this"]}, direction="inbound")

    actions = nba.rank_actions()

    assert len(actions) == 1
    assert actions[0]["issue_id"] == iid
    assert actions[0]["claim_type"] == "ask"
    assert actions[0]["text"] == "can you approve this"


def test_rank_actions_excludes_counterparty_owed_ask(ws_db):
    """An outbound ask (Marc asking someone else) puts the obligation on
    the counterparty, not Marc - correctly excluded from Marc's own
    ranked action list."""
    iid = _issue(ws_db)
    _raw_item(ws_db, iid, "r2", {"asks": ["please send the SOW"]}, direction="outbound")

    assert nba.rank_actions() == []


def test_rank_actions_excludes_decisions(ws_db):
    """decisions have owner=None by design (a joint fact, not an
    obligation) - never in the ranked action list."""
    iid = _issue(ws_db)
    _raw_item(ws_db, iid, "r3", {"decisions": ["going with vendor B"]}, direction="outbound")

    assert nba.rank_actions() == []


def test_rank_actions_excludes_dismissed_and_closed_issues(ws_db):
    iid = _issue(ws_db, "Closed", state="done")
    _raw_item(ws_db, iid, "r4", {"asks": ["approve this"]}, direction="inbound")

    assert nba.rank_actions() == []


def test_rank_actions_caps_per_issue(ws_db):
    iid = _issue(ws_db, "Chatty thread")
    for i in range(5):
        _raw_item(ws_db, iid, f"r5-{i}", {"asks": [f"approve item {i}"]}, direction="inbound")

    actions = nba.rank_actions()

    assert len([a for a in actions if a["issue_id"] == iid]) == nba._MAX_ACTIONS_PER_ISSUE


def test_rank_actions_respects_limit(ws_db):
    for i in range(3):
        iid = _issue(ws_db, f"Issue {i}")
        _raw_item(ws_db, iid, f"r6-{i}", {"asks": [f"approve {i}"]}, direction="inbound")

    assert len(nba.rank_actions(limit=2)) == 2


def test_rank_actions_ranks_escalated_and_stale_higher(ws_db):
    iid_fresh = _issue(ws_db, "Fresh")
    iid_stale = _issue(ws_db, "Stale and escalated")
    now = time.time()
    _raw_item(ws_db, iid_fresh, "r7", {"asks": ["a brand new ask"]}, direction="inbound", occurred_ts=now)

    import json
    rid1 = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="r8", thread_key="r8", dedupe_key="r8",
        occurred_ts=now - 20 * nba.DAY, subject="s", from_actor="a@example.com", participants_json="[]",
    )
    ws_db.link_raw_item_to_issue(rid1, iid_stale)
    ws_db.create_extraction(rid1, json.dumps({"asks": ["please sign the SOW"]}))
    conn = ws_db._connect()
    conn.execute("UPDATE raw_item_extractions SET extracted_ts = ? WHERE raw_item_id = ?",
                 (now - 20 * nba.DAY, rid1))
    conn.execute("UPDATE raw_items SET direction = 'inbound' WHERE id = ?", (rid1,))
    conn.close()
    workgraph_claims.materialize_claims_for_raw_item(rid1)

    _raw_item(ws_db, iid_stale, "r9", {
        "asks": ["please sign the SOW"],
        "repeat_signals": [{"ask_text": "please sign the SOW", "escalated": True}],
    }, direction="inbound", occurred_ts=now)

    actions = nba.rank_actions(now=now)
    ranked_issue_ids = [a["issue_id"] for a in actions]

    assert ranked_issue_ids.index(iid_stale) < ranked_issue_ids.index(iid_fresh)


def test_rank_actions_raw_item_id_present_for_deep_link(ws_db):
    iid = _issue(ws_db)
    rid = _raw_item(ws_db, iid, "r10", {"asks": ["approve this"]}, direction="inbound")

    actions = nba.rank_actions()

    assert actions[0]["raw_item_id"] == rid
