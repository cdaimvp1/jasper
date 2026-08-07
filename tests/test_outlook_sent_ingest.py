"""Tests for task #270 Phase A: outlook_com_sent_ingest.py, the sent-items
counterpart to outlook_com_ingest.py.

No live Outlook involved - outlook_scan_sent.ps1 is a COM wrapper that can't
run in CI/off-Windows-Outlook anyway, so subprocess.run is monkeypatched to
return JSON-lines shaped exactly like the real script's output, same
discipline as test_outlook_ingest.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BODY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BODY / "ingest"))
import outlook_com_sent_ingest as osi  # noqa: E402


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _stage_sent_item(name: str, entry_id: str, sent_epoch: float = 1_800_000_000.0,
                      subject: str = "Re: SOW", body_preview: str = "Approved, thanks.",
                      participants=None) -> dict:
    return {
        "conversation_id": f"conv-{name}",
        "entry_id": entry_id,
        "subject": subject,
        "participants": participants or ["vendor@example.com"],
        "sent_epoch": sent_epoch,
        "body_preview": body_preview,
        "body_excerpt": body_preview,
        "attachments": [],
        "body_text_file": "",
        "body_html_file": "",
        "item_staged_dir": None,
    }


def test_run_inserts_with_outbound_confirmed_direction(ws_db, isolated_paths, monkeypatch):
    item = _stage_sent_item("a", "entryid-SENT-AAA")

    def fake_run(*a, **kw):
        return _FakeCompletedProcess(json.dumps(item) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = osi.run()

    assert result["ok"] is True
    assert result["inserted"] == 1

    rows = ws_db._connect().execute("SELECT * FROM raw_items").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["source"] == "outlook_mail"  # NOT a new source value - same as inbound mail, on purpose
    assert row["thread_key"] == "conv-a"  # conversation_id, same convention as inbound
    assert json.loads(row["meta_json"])["confirmed_direction"] == "outbound"


def test_run_uses_manager_identity_as_from_actor(ws_db, isolated_paths, monkeypatch):
    """No 'sender' field exists on a sent item's own JSON (Marc is always
    the sender) - from_actor must come from config.get('manager','id')."""
    import config
    monkeypatch.setattr(config, "get", lambda *a, **kw: "lane_marc@lilly.com" if a == ("manager", "id") else None)
    item = _stage_sent_item("b", "entryid-SENT-BBB")

    def fake_run(*a, **kw):
        return _FakeCompletedProcess(json.dumps(item) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    osi.run()

    row = dict(ws_db._connect().execute("SELECT * FROM raw_items").fetchone())
    assert row["from_actor"] == "lane_marc@lilly.com"


def test_run_dedupes_via_same_dedupe_key_as_inbound(ws_db, isolated_paths, monkeypatch):
    """A sent item re-scanned twice (a replayed cursor) must not double-insert -
    same dedupe_key mechanism run() already reuses from outlook_com_ingest."""
    item = _stage_sent_item("c", "entryid-SENT-CCC")

    def fake_run(*a, **kw):
        return _FakeCompletedProcess(json.dumps(item) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    osi.run()
    result2 = osi.run()

    assert result2["duplicates"] == 1
    assert result2["inserted"] == 0


def test_run_advances_cursor_forward_only(ws_db, isolated_paths, monkeypatch):
    item1 = _stage_sent_item("d1", "entryid-SENT-DDD1", sent_epoch=1_800_000_100.0)

    def fake_run(*a, **kw):
        return _FakeCompletedProcess(json.dumps(item1) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result1 = osi.run()
    assert result1["cursor"] == 1_800_000_100.0

    def fake_run_empty(*a, **kw):
        return _FakeCompletedProcess("")

    monkeypatch.setattr(subprocess, "run", fake_run_empty)
    result2 = osi.run()
    assert result2["cursor"] == 1_800_000_100.0  # never regresses on an empty batch


def test_run_reports_error_on_nonzero_exit_but_salvages_valid_lines(ws_db, isolated_paths, monkeypatch):
    item = _stage_sent_item("e", "entryid-SENT-EEE")

    def fake_run(*a, **kw):
        return _FakeCompletedProcess(json.dumps(item) + "\n", returncode=1, stderr="some COM error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = osi.run()

    assert result["ok"] is False
    assert result["inserted"] == 1  # salvaged despite the non-zero exit
    assert "error" in result


def test_backfill_does_not_advance_live_cursor(ws_db, isolated_paths, monkeypatch):
    """A manual catch-up pull must never fast-forward the live daily cursor
    past mail the live run() hasn't seen yet."""
    item = _stage_sent_item("f", "entryid-SENT-FFF", sent_epoch=1_800_000_200.0)

    def fake_run(*a, **kw):
        return _FakeCompletedProcess(json.dumps(item) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = osi.backfill(days_back=90)

    assert result["ok"] is True
    assert result["inserted"] == 1
    assert ws_db.get_cursor("outlook_mail", "folder:Sent Items") is None
