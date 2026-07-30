"""Regression tests for task #43: outlook_com_ingest.py used to truncate every
body to 500 chars and never persist Outlook's own EntryID - both fixed by
staging the full plain-text/HTML body to files (same pattern attachments
already use) and adding a real entry_id column to raw_items.

No live Outlook involved - outlook_scan.ps1 is a COM wrapper that can't run
in CI/off-Windows-Outlook anyway, so subprocess.run is monkeypatched to return
JSON-lines shaped exactly like the real script's new output, and item_staged_dir
points at a real tmp_path directory containing real body.txt/body.html files -
this is the same "shape the mock data 1:1 with the real emitter" discipline
used for every other ingestion test this session.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BODY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BODY / "ingest"))
import outlook_com_ingest as oci  # noqa: E402


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _stage_item(tmp_path, name: str, entry_id: str, body_text: str, body_html: str,
                 received_epoch: float = 1_800_000_000.0) -> tuple[dict, Path]:
    staged_dir = tmp_path / f"staged_{name}"
    staged_dir.mkdir()
    (staged_dir / "body.txt").write_text(body_text, encoding="utf-8")
    (staged_dir / "body.html").write_text(body_html, encoding="utf-8")
    item = {
        "conversation_id": f"conv-{name}",
        "entry_id": entry_id,
        "subject": f"Subject {name}",
        "sender": "vendor@example.com",
        "sender_name": "Vendor Example",
        "participants": ["vendor@example.com", "marc@example.com"],
        "received_epoch": received_epoch,
        "body_preview": body_text[:500],
        "attachments": [],
        "body_text_file": "body.txt",
        "body_html_file": "body.html",
        "item_staged_dir": str(staged_dir),
    }
    return item, staged_dir


def test_full_body_and_entry_id_persisted(ws_db, isolated_paths, monkeypatch, tmp_path):
    full_text = "line one\n" * 200  # far past the old 500-char truncation
    full_html = "<html><body>" + ("<p>hi</p>" * 200) + "</body></html>"
    item, staged_dir = _stage_item(tmp_path, "a", "entryid-AAA", full_text, full_html)

    def fake_run(*a, **kw):
        return _FakeCompletedProcess(json.dumps(item) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = oci.run(folder="Careful")

    assert result["ok"] is True
    assert result["inserted"] == 1

    rows = ws_db._connect().execute("SELECT * FROM raw_items").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["entry_id"] == "entryid-AAA"
    assert row["body_preview"] == full_text[:500]  # unchanged short-preview behavior

    ref = json.loads(row["raw_ref"])
    text_path = isolated_paths.DOCUMENTS_DIR / ref["body_text"]
    html_path = isolated_paths.DOCUMENTS_DIR / ref["body_html"]
    assert text_path.read_text(encoding="utf-8") == full_text
    assert html_path.read_text(encoding="utf-8") == full_html


def test_staged_dir_cleaned_up_after_absorb(ws_db, isolated_paths, monkeypatch, tmp_path):
    item, staged_dir = _stage_item(tmp_path, "b", "entryid-BBB", "body", "<p>body</p>")

    def fake_run(*a, **kw):
        return _FakeCompletedProcess(json.dumps(item) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    oci.run(folder="Careful")

    assert not staged_dir.exists()


def test_duplicate_item_still_cleans_staged_dir(ws_db, isolated_paths, monkeypatch, tmp_path):
    """Same dedupe_key twice (a replayed cursor) - the second insert is a
    no-op duplicate, but its staging folder must still be reclaimed, exactly
    the same guarantee run() already made for attachments before this change."""
    item1, dir1 = _stage_item(tmp_path, "c1", "entryid-CCC1", "body", "<p>b</p>",
                               received_epoch=1_800_000_100.0)
    item2, dir2 = _stage_item(tmp_path, "c2", "entryid-CCC1", "body", "<p>b</p>",
                               received_epoch=1_800_000_100.0)
    # Force an identical dedupe_key: same participants/day/source_ref as item1.
    item2["entry_id"] = item1["entry_id"]
    item2["conversation_id"] = item1["conversation_id"]
    item2["participants"] = item1["participants"]

    calls = iter([
        _FakeCompletedProcess(json.dumps(item1) + "\n"),
        _FakeCompletedProcess(json.dumps(item2) + "\n"),
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: next(calls))

    r1 = oci.run(folder="Careful")
    r2 = oci.run(folder="Careful")

    assert r1["inserted"] == 1
    assert r2["duplicates"] == 1
    assert not dir1.exists()
    assert not dir2.exists()


def test_absorb_body_handles_missing_files_gracefully(ws_db, isolated_paths, tmp_path):
    staged_dir = tmp_path / "staged_missing"
    staged_dir.mkdir()  # no body.txt / body.html actually written inside

    ref = oci._absorb_body(row_id=999, item_staged_dir=str(staged_dir),
                            text_file="body.txt", html_file="body.html")
    assert ref is None  # neither file existed - nothing to point at, no crash


def test_absorb_body_returns_none_when_no_staged_dir(ws_db, isolated_paths):
    assert oci._absorb_body(row_id=1, item_staged_dir=None,
                             text_file="body.txt", html_file="body.html") is None


def test_sweep_unread_also_persists_entry_id_and_body(ws_db, isolated_paths, monkeypatch, tmp_path):
    item, staged_dir = _stage_item(tmp_path, "d", "entryid-DDD", "unread body", "<p>unread</p>")

    def fake_run(*a, **kw):
        return _FakeCompletedProcess(json.dumps(item) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = oci.sweep_unread(folder="Careful")

    assert result["inserted"] == 1
    row = dict(ws_db._connect().execute("SELECT * FROM raw_items").fetchall()[0])
    assert row["entry_id"] == "entryid-DDD"
    assert row["raw_ref"] is not None
    assert not staged_dir.exists()


def test_schema_migration_entry_id_column_idempotent(ws_db):
    """init_workgraph()'s ALTER TABLE ADD COLUMN entry_id must be safe to run
    against an already-migrated DB (every real wake calls init_workgraph()
    again, it doesn't run once ever) - the try/except OperationalError pattern
    already used for signal_type/pr_number, applied the same way here."""
    ws_db.init_workgraph()  # second call, same db - must not raise
    ws_db.init_workgraph()  # third call for good measure
    cols = {r["name"] for r in ws_db._connect().execute("PRAGMA table_info(raw_items)").fetchall()}
    assert "entry_id" in cols
    assert "raw_ref" in cols
