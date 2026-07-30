"""Regression tests for personal_patterns.py - Phase 1 (task #45, app_chat)
and Phase 2 (task #49, sent_mail) of Personal Response Learning: deterministic
keyword mining, gated off by default behind config toggles."""
import subprocess
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


class _FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


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


def test_mine_sent_mail_populates_response_patterns(pp_env, monkeypatch):
    import workgraph_store as ws
    now = time.time()
    lines = "\n".join([
        '{"entry_id":"e1","subject":"Ariba PO","sent_epoch":%f,"body_excerpt":"please check the Ariba status"}' % (now - 200),
        '{"entry_id":"e2","subject":"SOW draft","sent_epoch":%f,"body_excerpt":"draft attached for review"}' % (now - 100),
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeCompletedProcess(stdout=lines))

    result = pp_env.mine_sent_mail(since_ts=0)

    assert result["scanned"] == 2
    assert result["matched"] == 2
    assert result["cursor"] == pytest.approx(now - 100)  # %f-formatted fixture text loses sub-microsecond precision
    patterns = {p["pattern_key"]: p["hit_count"] for p in ws.list_response_patterns("sent_mail")}
    assert patterns["ariba"] == 1
    assert patterns["draft reply"] == 1


def test_mine_sent_mail_skips_malformed_lines(pp_env, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                         lambda *a, **kw: _FakeCompletedProcess(stdout="not json\n\n"))
    result = pp_env.mine_sent_mail(since_ts=0)
    assert result["scanned"] == 0
    assert result["matched"] == 0


def test_mine_sent_mail_reports_error_on_nonzero_exit_without_raising(pp_env, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                         lambda *a, **kw: _FakeCompletedProcess(returncode=1, stderr="Outlook not running"))
    result = pp_env.mine_sent_mail(since_ts=0)
    assert result["error"] == "Outlook not running"
    assert result["scanned"] == 0


def test_mine_sent_teams_populates_response_patterns(pp_env):
    import workgraph_store as ws, config
    config.set_value({"id": "Marc Lane"}, "manager")
    ws.insert_raw_item(source="teams_chat", stable_key="c1:m1", thread_key="c1",
                        dedupe_key="d1", occurred_ts=100.0, subject=None,
                        from_actor="Marc Lane", participants_json="[]",
                        body_preview="can you check the Ariba status")
    ws.insert_raw_item(source="teams_chat", stable_key="c1:m2", thread_key="c1",
                        dedupe_key="d2", occurred_ts=200.0, subject=None,
                        from_actor="Someone Else", participants_json="[]",
                        body_preview="ariba is fine on my end")

    result = pp_env.mine_sent_teams(since_ts=0)

    assert result["scanned"] == 1  # only Marc's own message
    assert result["matched"] == 1
    patterns = {p["pattern_key"]: p["hit_count"] for p in ws.list_response_patterns("sent_teams")}
    assert patterns["ariba"] == 1


def test_mine_sent_teams_no_manager_id_is_a_safe_noop(pp_env):
    result = pp_env.mine_sent_teams(since_ts=42.0)
    assert result == {"scanned": 0, "matched": 0, "cursor": 42.0}


def test_run_daily_if_due_runs_only_sent_teams_when_that_is_the_only_toggle_on(pp_env):
    import config, workgraph_store as ws
    config.set_value({"id": "Marc Lane"}, "manager")
    config.set_value(True, "personal_learning", "enabled")
    config.set_value({"sent_teams": True}, "personal_learning", "surfaces")
    ws.insert_raw_item(source="teams_chat", stable_key="c1:m1", thread_key="c1",
                        dedupe_key="d1", occurred_ts=time.time() - 60, subject=None,
                        from_actor="Marc Lane", participants_json="[]", body_preview="check sap")

    result = pp_env.run_daily_if_due()
    assert result is not None
    assert result["sent_teams"]["scanned"] == 1
    assert "app_chat" not in result and "sent_mail" not in result


def test_run_daily_if_due_returns_none_when_disabled(pp_env):
    assert pp_env.run_daily_if_due() is None


def test_run_daily_if_due_returns_none_when_master_on_but_no_surface_enabled(pp_env):
    import config
    config.set_value(True, "personal_learning", "enabled")
    config.set_value({"app_chat": False, "sent_mail": False}, "personal_learning", "surfaces")
    assert pp_env.run_daily_if_due() is None


def test_run_daily_if_due_runs_only_app_chat_when_that_is_the_only_toggle_on(pp_env):
    import config, workgraph_store as ws
    config.set_value(True, "personal_learning", "enabled")
    config.set_value({"app_chat": True}, "personal_learning", "surfaces")
    _log_question(ws, time.time() - 60, "check the Ariba status please")

    result = pp_env.run_daily_if_due()
    assert result is not None
    assert result["app_chat"]["scanned"] == 1
    assert "sent_mail" not in result

    second = pp_env.run_daily_if_due()  # same day - gated, must not re-run
    assert second is None


def test_run_daily_if_due_runs_only_sent_mail_when_that_is_the_only_toggle_on(pp_env, monkeypatch):
    import config
    config.set_value(True, "personal_learning", "enabled")
    config.set_value({"sent_mail": True}, "personal_learning", "surfaces")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeCompletedProcess(
        stdout='{"entry_id":"e1","subject":"Ariba","sent_epoch":%f,"body_excerpt":"ariba status"}' % time.time()))

    result = pp_env.run_daily_if_due()
    assert result is not None
    assert result["sent_mail"]["scanned"] == 1
    assert "app_chat" not in result


def test_run_daily_if_due_runs_both_surfaces_together(pp_env, monkeypatch):
    import config, workgraph_store as ws
    config.set_value(True, "personal_learning", "enabled")
    config.set_value({"app_chat": True, "sent_mail": True}, "personal_learning", "surfaces")
    _log_question(ws, time.time() - 60, "check ariba")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeCompletedProcess(
        stdout='{"entry_id":"e1","subject":"SAP","sent_epoch":%f,"body_excerpt":"sap update"}' % time.time()))

    result = pp_env.run_daily_if_due()
    assert set(result.keys()) == {"app_chat", "sent_mail"}


def test_citation_for_text_none_when_no_pattern_match(pp_env):
    assert pp_env.citation_for_text("just a normal message") is None


def test_citation_for_text_none_below_min_hit_count(pp_env, monkeypatch):
    import workgraph_store as ws
    ws.upsert_response_pattern("app_chat", "ariba", "x", 1.0)
    ws.upsert_response_pattern("app_chat", "ariba", "x", 2.0)  # hit_count=2, below MIN_CITATION_HIT_COUNT(3)
    assert pp_env.citation_for_text("check the ariba status") is None


def test_citation_for_text_cites_pattern_at_or_above_threshold(pp_env):
    import workgraph_store as ws
    for i in range(3):
        ws.upsert_response_pattern("app_chat", "ariba", "x", float(i))

    result = pp_env.citation_for_text("check the ariba status")

    assert result["pattern_key"] == "ariba"
    assert result["hit_count"] == 3
    assert "3 times" in result["note"]


def test_citation_for_text_picks_highest_hit_count_when_multiple_match(pp_env):
    import workgraph_store as ws
    for i in range(3):
        ws.upsert_response_pattern("app_chat", "ariba", "x", float(i))
    for i in range(5):
        ws.upsert_response_pattern("sent_mail", "check status", "x", float(i))

    result = pp_env.citation_for_text("check the ariba status")

    assert result["pattern_key"] == "check status"
    assert result["hit_count"] == 5


def test_attach_citations_off_when_personal_learning_disabled(pp_env):
    import workgraph_store as ws
    for i in range(5):
        ws.upsert_response_pattern("app_chat", "ariba", "x", float(i))
    evidence = [{"summary": "check the ariba status"}]

    pp_env.attach_citations(evidence)

    assert evidence[0]["learned_citation"] is None


def test_attach_citations_cites_when_enabled(pp_env):
    import config, workgraph_store as ws
    config.set_value(True, "personal_learning", "enabled")
    for i in range(5):
        ws.upsert_response_pattern("app_chat", "ariba", "x", float(i))
    evidence = [{"summary": "check the ariba status"}, {"summary": "nothing relevant here"}]

    pp_env.attach_citations(evidence)

    assert evidence[0]["learned_citation"]["pattern_key"] == "ariba"
    assert evidence[1]["learned_citation"] is None


def test_run_daily_if_due_persists_cursor_across_calls(pp_env):
    import config, workgraph_store as ws
    config.set_value(True, "personal_learning", "enabled")
    config.set_value({"app_chat": True}, "personal_learning", "surfaces")
    now = time.time()
    _log_question(ws, now - 3600, "check ariba")

    day1 = pp_env.run_daily_if_due(now=now)
    assert day1["app_chat"]["scanned"] == 1

    # simulate the next day - cursor should already be past this question,
    # so a second question is the only new thing scanned.
    _log_question(ws, now - 100, "draft a reply")
    day2 = pp_env.run_daily_if_due(now=now + 90000)
    assert day2["app_chat"]["scanned"] == 1
