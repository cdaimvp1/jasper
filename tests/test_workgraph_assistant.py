"""
tests/test_workgraph_assistant.py — task #271 coverage.

Two things under test, both new this task:
  1. workgraph_store's assistant_chat_turns table (append/list/clear) - the
     server-side visible-transcript log that lets a reloaded add-in pane
     redraw its chat bubbles after New Outlook wipes the DOM.
  2. workgraph_assistant.ask()'s wiring into that log: the user's own
     message is always logged (even on a failed turn), the reply only on
     success, and reset clears both the --resume pointer and the visible
     log together.

_call_claude_once is mocked throughout - these tests are about the
logging wiring, not about actually spawning `claude -p`.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import workgraph_assistant


def test_run_claude_sends_prompt_via_stdin_not_argv():
    """External-review finding #358 (2026-08-13): the prompt used to be a
    literal `claude -p <prompt>` command-line argument, stacked on top of
    an already-large --append-system-prompt and a 29-tool --allowedTools
    list in the same Windows command line - the exact crash class task
    #309 already fixed elsewhere. Locks in the fix: the prompt text must
    never appear as one of the constructed argv elements, and must be the
    exact `input=` passed to Popen.communicate()."""
    fake_proc = MagicMock()
    fake_proc.communicate.return_value = ("{}", "")
    fake_proc.returncode = 0
    long_prompt = "this is Marc's message " * 50  # long enough to matter if it leaked into argv
    with patch("workgraph_assistant.subprocess.Popen", return_value=fake_proc) as mocked_popen:
        workgraph_assistant._run_claude(long_prompt, session_id="sid-1", is_new=True, timeout=30)

    args = mocked_popen.call_args[0][0]
    assert long_prompt not in args
    assert all(long_prompt not in a for a in args)
    assert args[0:2] == ["claude", "-p"]
    fake_proc.communicate.assert_called_once_with(input=long_prompt, timeout=30)
    assert mocked_popen.call_args.kwargs.get("stdin") is not None


def test_chat_turns_roundtrip_ordered(ws_db):
    ws_db.append_assistant_chat_turn("you", "hello")
    ws_db.append_assistant_chat_turn("jasper", "hi there")
    ws_db.append_assistant_chat_turn("you", "second message")

    turns = ws_db.list_assistant_chat_turns()
    assert [t["sender"] for t in turns] == ["you", "jasper", "you"]
    assert [t["text"] for t in turns] == ["hello", "hi there", "second message"]


def test_chat_turns_empty_when_nothing_logged(ws_db):
    assert ws_db.list_assistant_chat_turns() == []


def test_clear_chat_turns_removes_everything(ws_db):
    ws_db.append_assistant_chat_turn("you", "hello")
    ws_db.append_assistant_chat_turn("jasper", "hi")
    ws_db.clear_assistant_chat_turns()
    assert ws_db.list_assistant_chat_turns() == []


def test_ask_logs_both_sides_on_success(ws_db, monkeypatch):
    monkeypatch.setattr(workgraph_assistant, "ws", ws_db)
    with patch.object(workgraph_assistant, "_call_claude_once") as mocked:
        mocked.return_value = {"ok": True, "session_id": "sid-1", "reply": "here's the answer"}
        result = workgraph_assistant.ask("what's on my plate")

    assert result["ok"] is True
    turns = ws_db.list_assistant_chat_turns()
    assert len(turns) == 2
    assert turns[0]["sender"] == "you"
    assert turns[0]["text"] == "what's on my plate"
    assert turns[1]["sender"] == "jasper"
    assert turns[1]["text"] == "here's the answer"


def test_ask_logs_user_turn_but_not_reply_on_failure(ws_db, monkeypatch):
    """A failed/timed-out turn still gets the user's own message logged
    (Marc did say that, whether or not Jasper answered) but no jasper-side
    turn, since there's no real reply worth replaying into a restored
    transcript - matches ask()'s own docstring for this task."""
    monkeypatch.setattr(workgraph_assistant, "ws", ws_db)
    with patch.object(workgraph_assistant, "_call_claude_once") as mocked:
        mocked.return_value = {"ok": False, "session_id": "sid-1", "reply": "Jasper hit an error."}
        result = workgraph_assistant.ask("a message that fails", session_id="sid-1")

    assert result["ok"] is False
    turns = ws_db.list_assistant_chat_turns()
    assert len(turns) == 1
    assert turns[0]["sender"] == "you"
    assert turns[0]["text"] == "a message that fails"


def test_ask_reset_clears_session_and_chat_log_before_new_turn(ws_db, monkeypatch):
    monkeypatch.setattr(workgraph_assistant, "ws", ws_db)
    ws_db.set_assistant_session_id("stale-session")
    ws_db.append_assistant_chat_turn("you", "old turn")
    ws_db.append_assistant_chat_turn("jasper", "old reply")

    with patch.object(workgraph_assistant, "_call_claude_once") as mocked:
        mocked.return_value = {"ok": True, "session_id": "fresh-session", "reply": "fresh reply"}
        workgraph_assistant.ask("start over", reset=True)

    turns = ws_db.list_assistant_chat_turns()
    assert [t["text"] for t in turns] == ["start over", "fresh reply"]
    assert ws_db.get_assistant_session_id() == "fresh-session"
