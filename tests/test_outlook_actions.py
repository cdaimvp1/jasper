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


def test_mark_read_requires_entry_id():
    with pytest.raises(ValueError):
        oa.mark_read("")
    with pytest.raises(ValueError):
        oa.mark_read(None)


def test_mark_read_success_calls_script_with_entry_id(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = oa.mark_read("entryid-ABC123")

    assert result == {"ok": True}
    assert "entryid-ABC123" in captured["args"]
    assert str(oa._MARK_READ_SCRIPT) in captured["args"]


def test_mark_read_raises_runtime_error_on_failure(monkeypatch):
    def fake_run(args, **kwargs):
        return _FakeCompletedProcess(returncode=2, stderr="No item found for that EntryID")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="No item found"):
        oa.mark_read("stale-entry-id")


def test_mark_read_timeout_raises_runtime_error_not_timeout_expired(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 20))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out"):
        oa.mark_read("some-entry-id")


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


def test_draft_reply_body_and_save_only_are_additive(monkeypatch):
    """task #287: with neither passed, behaves exactly as before (no
    -BodyFile/-SaveOnly noise); both are real, separate flags when given.

    External-review finding #358 (2026-08-13): body used to be passed as a
    literal -Body <text> command-line argument - Windows' CreateProcess
    has a hard ~32K character total-command-line limit, and an unbounded
    drafted body could hit it. Now written to a private temp file and
    passed as -BodyFile <path> instead - the body text itself must never
    appear as an argv element, and the temp file must be cleaned up after
    the call returns (success or failure)."""
    import os
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    oa.draft_reply("entryid-ABC")
    assert "-BodyFile" not in captured["args"]
    assert "-SaveOnly" not in captured["args"]

    body_text = "Still on track, will follow up Friday."
    oa.draft_reply("entryid-ABC", body=body_text, save_only=True)
    assert "-BodyFile" in captured["args"]
    assert body_text not in captured["args"]  # never a raw argv element
    assert "-SaveOnly" in captured["args"]

    body_file_path = captured["args"][captured["args"].index("-BodyFile") + 1]
    assert not os.path.exists(body_file_path)  # cleaned up after the call


def test_draft_reply_writes_real_body_content_to_the_temp_file(monkeypatch):
    """The temp file -BodyFile points at must actually contain the real
    body text (checked before cleanup, since the fixture below deletes it
    the instant fake_run returns - matching real _run_powershell timing)."""
    import os
    captured = {}
    body_text = "Still on track, will follow up Friday."

    def fake_run(args, **kwargs):
        body_file_path = args[args.index("-BodyFile") + 1]
        with open(body_file_path, "r", encoding="utf-8") as f:
            captured["body_file_content"] = f.read()
        assert os.path.exists(body_file_path)
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    oa.draft_reply("entryid-ABC", body=body_text)
    assert captured["body_file_content"] == body_text


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


# --- compose_new (task #35, 2026-08-04) -------------------------------------
# Replaces the interim client-only mailto: link the cockpit UI used for the
# stakeholder multi-select + compose action. Unlike draft_reply/draft_forward,
# there's no EntryID here (a fresh thread, not a reply to an existing item) -
# the recipient list is the only real required input.

def test_compose_new_requires_to_emails():
    with pytest.raises(ValueError):
        oa.compose_new([], "subject")
    with pytest.raises(ValueError):
        oa.compose_new(None, "subject")


def test_compose_new_joins_recipients_with_semicolons(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = oa.compose_new(["a@x.com", "b@y.com"], "Re: pricing - Ref: JW-marc-004")

    assert result == {"ok": True}
    assert "a@x.com;b@y.com" in captured["args"]
    assert "Re: pricing - Ref: JW-marc-004" in captured["args"]
    assert str(oa._DRAFT_COMPOSE_SCRIPT) in captured["args"]


def test_compose_new_raises_runtime_error_on_failure(monkeypatch):
    def fake_run(args, **kwargs):
        return _FakeCompletedProcess(returncode=1, stderr="Outlook is not running")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Outlook is not running"):
        oa.compose_new(["a@x.com"], "subject")


def test_compose_new_timeout_raises_runtime_error_not_timeout_expired(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 20))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out"):
        oa.compose_new(["a@x.com"], "subject")


def test_compose_new_body_and_attachment_paths_are_additive(monkeypatch):
    """2026-08-08 follow-on (Marc's 'share this output and ask them to
    review' ask): body/attachment_paths are optional and additive - with
    neither, the args list must be identical to the pre-existing bare
    to/subject call (no accidental -BodyFile "" or -AttachmentPaths ""
    noise). External-review finding #358 (2026-08-13): body now goes via
    a temp file (-BodyFile), never as a raw argv element - see
    test_draft_reply_body_and_save_only_are_additive's own docstring for
    why."""
    import os
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    oa.compose_new(["a@x.com"], "subject")
    assert "-BodyFile" not in captured["args"]
    assert "-AttachmentPaths" not in captured["args"]

    oa.compose_new(["a@x.com"], "subject", body="Please review.",
                    attachment_paths=[r"C:\docs\redline.docx", r"C:\docs\cover.pdf"])
    assert "-BodyFile" in captured["args"]
    assert "Please review." not in captured["args"]  # never a raw argv element
    assert "-AttachmentPaths" in captured["args"]
    assert r"C:\docs\redline.docx;C:\docs\cover.pdf" in captured["args"]

    body_file_path = captured["args"][captured["args"].index("-BodyFile") + 1]
    assert not os.path.exists(body_file_path)  # cleaned up after the call


def test_compose_new_returns_real_missing_attachments_from_powershell(monkeypatch):
    """External-review finding #356 (2026-08-13): _run_powershell used to
    return a bare {"ok": True} unconditionally, discarding the real
    attached/missing_attachments JSON outlook_draft_compose.ps1 already
    emits on success. compose_new()'s own docstring promises callers can
    check missing_attachments - this locks in that the real PowerShell
    JSON result now actually reaches the caller instead of being thrown
    away."""
    def fake_run(args, **kwargs):
        return _FakeCompletedProcess(
            returncode=0,
            stdout='{"ok":true,"attached":["C:\\\\docs\\\\redline.docx"],"missing_attachments":["C:\\\\docs\\\\gone.pdf"]}',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = oa.compose_new(["a@x.com"], "subject",
                             attachment_paths=[r"C:\docs\redline.docx", r"C:\docs\gone.pdf"])
    assert result["ok"] is True
    assert result["attached"] == [r"C:\docs\redline.docx"]
    assert result["missing_attachments"] == [r"C:\docs\gone.pdf"]


def test_run_powershell_falls_back_to_ok_true_on_unparseable_stdout(monkeypatch):
    """Defensive fallback: a returncode-0 result with empty/malformed stdout
    (not expected against any of this module's real scripts today, all
    five of which emit real JSON) must never crash the caller - same
    honest "assume success, no extra data" behavior as before this fix."""
    def fake_run(args, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert oa.open_email("entryid-ABC") == {"ok": True}
