"""Follow-up to task #376 (2026-08-12): run_relay_oneshot/run_deepdive_oneshot
directed their worker to call named M365 connector tools while only ever
declaring --allowedTools "Bash" - a real mismatch that happened to work only
because this repo's permissions bypass ignores --allowedTools entirely.
Locks in the honest, enumerated allowlist plus the permission-denial
visibility heuristic - see ingest/scheduled_refresh.py's own docstrings for
the full reasoning."""
from __future__ import annotations

import subprocess
import sys
import types

import pytest

# outlook_com_ingest/outlook_com_sent_ingest are genuinely absent from this
# checkout (never tracked in git - almost certainly production-machine-only
# COM automation files, confirmed via `git log --all` finding zero history).
# Pre-existing, unrelated to this task's fix - stubbed here only so
# ingest.scheduled_refresh's module-level `import outlook_com_ingest` etc.
# doesn't block testing the two functions this task actually changed.
for _missing in ("outlook_com_ingest", "outlook_com_sent_ingest"):
    if _missing not in sys.modules:
        sys.modules[_missing] = types.ModuleType(_missing)

import ingest.scheduled_refresh as sr


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_relay_allowedtools_includes_every_tool_its_prompt_calls_by_name(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _fake_completed()

    monkeypatch.setattr(sr, "_run_headless_with_tree_kill", fake_run)
    monkeypatch.setattr(sr.ws, "get_cursor", lambda *a, **k: None)

    sr.run_relay_oneshot()

    idx = captured["args"].index("--allowedTools")
    allowed = captured["args"][idx + 1].split(",")
    for tool in ("teams_list_chats", "outlook_calendar_search", "sharepoint_search", "read_resource"):
        assert any(tool in a for a in allowed), f"{tool} missing from relay's allowlist"
    assert "Bash" in allowed


def test_deepdive_allowedtools_includes_every_tool_its_prompt_calls_by_name(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _fake_completed()

    monkeypatch.setattr(sr, "_run_headless_with_tree_kill", fake_run)
    monkeypatch.setattr(sr.workgraph_deepdive, "list_deepdive_candidates", lambda: [{"id": "proj-1"}])

    sr.run_deepdive_oneshot()

    idx = captured["args"].index("--allowedTools")
    allowed = captured["args"][idx + 1].split(",")
    for tool in ("chat_message_search", "outlook_email_search", "sharepoint_search", "read_resource"):
        assert any(tool in a for a in allowed), f"{tool} missing from deep-dive's allowlist"
    assert "Bash" in allowed


def test_looks_like_permission_denial_detects_a_real_denial_message():
    # Real wording confirmed live via a direct probe (task #376 follow-up) -
    # never a fixed exact string, since it's the model's own paraphrase.
    real_denial = ("The Write tool is requesting permission to create the file "
                   "at that location. Your permission settings require approval "
                   "before the file can be written.")
    assert sr._looks_like_permission_denial(real_denial, "") is True


def test_looks_like_permission_denial_false_on_ordinary_success_text():
    ordinary = "Pulled 3 Teams chats, 12 messages, wrote drop files, cursor advanced."
    assert sr._looks_like_permission_denial(ordinary, "") is False


def test_relay_reports_possible_permission_denial_field(monkeypatch):
    def fake_run(args, **kwargs):
        return _fake_completed(stdout="Permission denied for that tool call.")

    monkeypatch.setattr(sr, "_run_headless_with_tree_kill", fake_run)
    monkeypatch.setattr(sr.ws, "get_cursor", lambda *a, **k: None)

    result = sr.run_relay_oneshot()

    assert result["possible_permission_denial"] is True


def test_deepdive_reports_possible_permission_denial_field(monkeypatch):
    def fake_run(args, **kwargs):
        return _fake_completed(stdout="Permission denied for that tool call.")

    monkeypatch.setattr(sr, "_run_headless_with_tree_kill", fake_run)
    monkeypatch.setattr(sr.workgraph_deepdive, "list_deepdive_candidates", lambda: [{"id": "proj-1"}])

    result = sr.run_deepdive_oneshot()

    assert result["possible_permission_denial"] is True
