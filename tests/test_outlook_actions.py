"""Regression tests for outlook_actions.py (task #46) - no live Outlook
involved (the .ps1 is a COM wrapper that can't run outside a real Outlook
session), so subprocess.run is monkeypatched. What's actually under test:
the entry_id validation, the argument shape passed to PowerShell, and the
error-translation on a non-zero exit."""
from __future__ import annotations

import subprocess

import pytest

import outlook_actions as oa


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout='{"ok":true}', stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_open_email_requires_entry_id():
    with pytest.raises(ValueError):
        oa.open_email("")
    with pytest.raises(ValueError):
        oa.open_email(None)


def test_open_email_success_calls_script_with_entry_id(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = oa.open_email("entryid-ABC123")

    assert result == {"ok": True}
    assert "entryid-ABC123" in captured["args"]
    assert str(oa._OPEN_ITEM_SCRIPT) in captured["args"]


def test_open_email_raises_runtime_error_on_nonzero_exit(monkeypatch):
    def fake_run(args, **kwargs):
        return _FakeCompletedProcess(returncode=2, stderr="No item found for that EntryID")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="No item found"):
        oa.open_email("stale-entry-id")


def test_open_email_raises_with_exit_code_when_stderr_empty(monkeypatch):
    def fake_run(args, **kwargs):
        return _FakeCompletedProcess(returncode=1, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="exit code 1"):
        oa.open_email("some-entry-id")
