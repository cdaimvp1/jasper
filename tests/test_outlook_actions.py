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


def test_draft_reply_requires_entry_id():
    with pytest.raises(ValueError):
        oa.draft_reply("")


def test_draft_reply_calls_script_without_reply_all_by_default(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = oa.draft_reply("entryid-ABC")

    assert result == {"ok": True}
    assert "entryid-ABC" in captured["args"]
    assert str(oa._DRAFT_REPLY_SCRIPT) in captured["args"]
    assert "-ReplyAll" not in captured["args"]


def test_draft_reply_all_passes_reply_all_flag(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    oa.draft_reply("entryid-ABC", reply_all=True)

    assert "-ReplyAll" in captured["args"]


def test_draft_reply_passes_ref_tag_when_given(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    oa.draft_reply("entryid-ABC", ref_tag="JW-marc-308")

    assert "-RefTag" in captured["args"]
    assert "JW-marc-308" in captured["args"]


def test_draft_reply_omits_ref_tag_when_not_given(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    oa.draft_reply("entryid-ABC")

    assert "-RefTag" not in captured["args"]


def test_draft_reply_raises_runtime_error_on_failure(monkeypatch):
    def fake_run(args, **kwargs):
        return _FakeCompletedProcess(returncode=2, stderr="stale entry id")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="stale entry id"):
        oa.draft_reply("stale-id")


def test_open_email_timeout_raises_runtime_error_not_timeout_expired(monkeypatch):
    """Fixed (adversarial review, task #61): subprocess.run's own timeout
    used to raise subprocess.TimeoutExpired, a different exception than the
    module's own documented "raises RuntimeError" contract - callers
    catching only RuntimeError would have seen an undocumented, unhandled
    exception on a genuinely slow/hung Outlook COM call instead of a clean
    HTTP 500."""
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 20))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out"):
        oa.open_email("some-entry-id")


def test_draft_reply_timeout_raises_runtime_error_not_timeout_expired(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 20))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out"):
        oa.draft_reply("some-entry-id")


def test_draft_forward_requires_entry_id():
    with pytest.raises(ValueError):
        oa.draft_forward("")
    with pytest.raises(ValueError):
        oa.draft_forward(None)


def test_draft_forward_calls_script_with_entry_id(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = oa.draft_forward("entryid-ABC")

    assert result == {"ok": True}
    assert "entryid-ABC" in captured["args"]
    assert str(oa._DRAFT_FORWARD_SCRIPT) in captured["args"]


def test_draft_forward_passes_ref_tag_when_given(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    oa.draft_forward("entryid-ABC", ref_tag="JW-marc-308")

    assert "-RefTag" in captured["args"]
    assert "JW-marc-308" in captured["args"]


def test_draft_forward_omits_ref_tag_when_not_given(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    oa.draft_forward("entryid-ABC")

    assert "-RefTag" not in captured["args"]


def test_draft_forward_raises_runtime_error_on_failure(monkeypatch):
    def fake_run(args, **kwargs):
        return _FakeCompletedProcess(returncode=2, stderr="stale entry id")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="stale entry id"):
        oa.draft_forward("stale-id")


def test_draft_forward_timeout_raises_runtime_error_not_timeout_expired(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 20))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out"):
        oa.draft_forward("some-entry-id")
