"""Regression tests for personal_patterns.py (task #45, Phase 1 of Personal
Response Learning) - deterministic keyword mining over Marc's own questions
in socrates_retrieval_log, gated off by default behind two config toggles."""
import time

import pytest


@pytest.fixture
def pp_env(isolated_paths, monkeypatch):
    import workgraph_store as ws, config, personal_patterns as pp
    monkeypatch.setattr(ws, "WORKGRAPH_DB", isolated_paths.WORKGRAPH_DB)
    monkeypatch.setattr(config, "SETTINGS_PATH", isolated_paths.CONFIG_DIR / "settings.json")
    monkeypatch.setattr(config, "_cache", {})
    monkeypatch.setattr(config, "_cache_mtime", 0.0)
    ws.init_workgraph()
    return pp


def _log_question(ws, asked_ts: float, question: str):
    ws.append_socrates_log(asked_ts=asked_ts, asker="marc", question=question,
                            signature="sig", tier="recall", band="high",
                            contributed=True, outcome="answered")


def test_extract_patterns_matches_multiple_and_is_case_insensitive(pp_env):
    keys = pp_env.extract_patterns("Can you check the ARIBA PO status for this?")
    assert "ariba" in keys
    assert "check status" in keys


def test_extract_patterns_empty_for_no_match(pp_env):
    assert pp_env.extract_patterns("what's the weather like") == []


def test_extract_patterns_handles_empty_and_none(pp_env):
    assert pp_env.extract_patterns("") == []
    assert pp_env.extract_patterns(None) == []


def test_mine_app_chat_populates_response_patterns(pp_env):
    import workgraph_store as ws
    now = time.time()
    _log_question(ws, now - 300, "Can you check the Ariba PO status?")
    _log_question(ws, now - 200, "Please draft a reply to Acme about the SOW")
    _log_question(ws, now - 100, "Ariba again - any update on that approval?")

    result = pp_env.mine_app_chat(since_ts=0)

    assert result["scanned"] == 3
    assert result["matched"] == 3
    assert result["cursor"] == now - 100

    patterns = ws.list_response_patterns("app_chat")
    by_key = {p["pattern_key"]: p for p in patterns}
    assert by_key["ariba"]["hit_count"] == 2
    assert by_key["draft reply"]["hit_count"] == 1
    assert by_key["ariba"]["example_text"] == "Ariba again - any update on that approval?"


def test_mine_app_chat_incremental_since_cursor(pp_env):
    import workgraph_store as ws
    now = time.time()
    _log_question(ws, now - 300, "check the Ariba status")
    first = pp_env.mine_app_chat(since_ts=0)

    _log_question(ws, now - 100, "check the SAP status")
    second = pp_env.mine_app_chat(since_ts=first["cursor"])

    assert second["scanned"] == 1  # only the new row, not re-scanning the first
    patterns = {p["pattern_key"]: p["hit_count"] for p in ws.list_response_patterns("app_chat")}
    assert patterns["ariba"] == 1
    assert patterns["sap"] == 1


def test_mine_app_chat_no_new_questions_is_a_safe_noop(pp_env):
    since = time.time()
    result = pp_env.mine_app_chat(since_ts=since)
    assert result == {"scanned": 0, "matched": 0, "cursor": since}


def test_run_daily_if_due_returns_none_when_disabled(pp_env):
    assert pp_env.run_daily_if_due() is None


def test_run_daily_if_due_returns_none_when_master_on_but_surface_off(pp_env):
    import config
    config.set_value(True, "personal_learning", "enabled")
    config.set_value({"app_chat": False}, "personal_learning", "surfaces")
    assert pp_env.run_daily_if_due() is None


def test_run_daily_if_due_runs_once_when_both_toggles_on(pp_env):
    import config, workgraph_store as ws
    config.set_value(True, "personal_learning", "enabled")
    config.set_value({"app_chat": True}, "personal_learning", "surfaces")
    _log_question(ws, time.time() - 60, "check the Ariba status please")

    result = pp_env.run_daily_if_due()
    assert result is not None
    assert result["scanned"] == 1

    second = pp_env.run_daily_if_due()  # same day - gated, must not re-run
    assert second is None


def test_run_daily_if_due_persists_cursor_across_calls(pp_env):
    import config, workgraph_store as ws
    config.set_value(True, "personal_learning", "enabled")
    config.set_value({"app_chat": True}, "personal_learning", "surfaces")
    now = time.time()
    _log_question(ws, now - 3600, "check ariba")

    day1 = pp_env.run_daily_if_due(now=now)
    assert day1["scanned"] == 1

    # simulate the next day - cursor should already be past this question,
    # so a second question is the only new thing scanned.
    _log_question(ws, now - 100, "draft a reply")
    day2 = pp_env.run_daily_if_due(now=now + 90000)
    assert day2["scanned"] == 1
