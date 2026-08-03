"""Regression tests for workgraph_sessionize.py's Teams sub-session
boundaries (identity formalization, 2026-08-03)."""
from __future__ import annotations

import workgraph_sessionize as ws_sess

HOUR = 3600.0


def _msg(occurred_ts, pr_number_base=None, **extra):
    return {"occurred_ts": occurred_ts, "pr_number_base": pr_number_base, **extra}


def test_first_message_starts_session_zero():
    out = ws_sess.sessionize_teams_messages([_msg(0)])
    assert out[0]["session_sequence"] == 0
    assert out[0]["boundary_reason"] == "first_message"


def test_messages_within_gap_same_hours_stay_in_one_session():
    msgs = [_msg(0), _msg(1 * HOUR), _msg(2 * HOUR)]
    out = ws_sess.sessionize_teams_messages(msgs)
    assert [m["session_sequence"] for m in out] == [0, 0, 0]
    assert out[1]["boundary_reason"] is None
    assert out[2]["boundary_reason"] is None


def test_gap_exceeding_72h_starts_new_session():
    msgs = [_msg(0), _msg(80 * HOUR)]
    out = ws_sess.sessionize_teams_messages(msgs)
    assert [m["session_sequence"] for m in out] == [0, 1]
    assert out[1]["boundary_reason"] == "gap_exceeds_72h"


def test_ambiguous_middle_gap_defaults_to_same_session():
    """8-72h with no corroborating signal - v0 doesn't implement the
    corroboration model, so it doesn't guess a split it can't support."""
    msgs = [_msg(0), _msg(24 * HOUR)]
    out = ws_sess.sessionize_teams_messages(msgs)
    assert [m["session_sequence"] for m in out] == [0, 0]


def test_matching_reference_keeps_same_session_regardless_of_gap():
    """A shared exclusive anchor overrides even a 100h gap - the real
    shape this is for: reminders/follow-ups on the same PR days apart."""
    msgs = [_msg(0, pr_number_base="PR1"), _msg(100 * HOUR, pr_number_base="PR1")]
    out = ws_sess.sessionize_teams_messages(msgs)
    assert [m["session_sequence"] for m in out] == [0, 0]
    assert out[1]["boundary_reason"] is None


def test_different_reference_starts_new_session_even_within_gap_same_hours():
    """A real topic shift (different PR) beats a short gap - stronger
    evidence than "it's been quiet" or "it's been recent"."""
    msgs = [_msg(0, pr_number_base="PR1"), _msg(1 * HOUR, pr_number_base="PR2")]
    out = ws_sess.sessionize_teams_messages(msgs)
    assert [m["session_sequence"] for m in out] == [0, 1]
    assert out[1]["boundary_reason"] == "reference_mismatch"


def test_marc_362_shape_multiple_prs_and_unrelated_chatter():
    """The real production shape this was built for: marc-362's Teams
    chat mixed PR1164438, unrelated chatter, PR1174820, and more unrelated
    chatter, all inside one flat container. Confirms it now splits into
    multiple sessions instead of reading as one incoherent blob."""
    msgs = [
        _msg(0, pr_number_base="PR1164438"),           # 0: PR1164438 ask
        _msg(1 * HOUR),                                  # 0: reply, no ref -> same session (short gap)
        _msg(2 * HOUR, pr_number_base="PR1174820"),      # 1: different ref -> new session
        _msg(3 * HOUR),                                  # 1: reply, no ref -> stays
        _msg(4 * HOUR),                                  # 1: unrelated chatter, still close in time
    ]
    out = ws_sess.sessionize_teams_messages(msgs)
    assert [m["session_sequence"] for m in out] == [0, 0, 1, 1, 1]


def test_sequence_of_gaps_and_references_combined():
    msgs = [
        _msg(0, pr_number_base="PR1"),
        _msg(1 * HOUR, pr_number_base="PR1"),   # same ref, same session
        _msg(90 * HOUR),                         # long gap, no ref on either side to compare -> new session
        _msg(91 * HOUR),                         # short gap from previous -> stays
    ]
    out = ws_sess.sessionize_teams_messages(msgs)
    assert [m["session_sequence"] for m in out] == [0, 0, 1, 1]


def test_empty_list_returns_empty():
    assert ws_sess.sessionize_teams_messages([]) == []


def test_does_not_mutate_input_messages():
    original = [_msg(0), _msg(1 * HOUR)]
    snapshot = [dict(m) for m in original]
    ws_sess.sessionize_teams_messages(original)
    assert original == snapshot


def test_custom_thresholds_are_respected():
    msgs = [_msg(0), _msg(3 * HOUR)]
    out = ws_sess.sessionize_teams_messages(msgs, gap_new_hours=2.0)
    assert [m["session_sequence"] for m in out] == [0, 1]
