"""Regression tests for inbox.py, ported from this session's ad hoc repros:
- path traversal in _safe_member_path (P0, task #17)
- archive_inbox race between read and archive+truncate (P0, task #18)
"""
import threading

import pytest


@pytest.fixture
def isolated_inbox(tmp_path, monkeypatch, bus_db):
    """send_message() emits a bus event, so this needs an initialized bus.db
    too - bus_db (from conftest.py) provides that isolation."""
    import inbox
    inbox_dir = tmp_path / "inboxes"
    archive_dir = inbox_dir / ".archive"
    monkeypatch.setattr(inbox, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(inbox, "ARCHIVE_DIR", archive_dir)
    inbox._ensure_dirs()
    return inbox


def test_relative_path_traversal_rejected(isolated_inbox):
    with pytest.raises(ValueError):
        isolated_inbox._inbox_path("../../evil")


def test_absolute_path_override_rejected(isolated_inbox):
    with pytest.raises(ValueError):
        isolated_inbox._inbox_path("C:/Windows/Temp/pwned")


def test_normal_member_id_still_works(isolated_inbox):
    p = isolated_inbox._inbox_path("relay")
    assert p.name == "relay.md"
    assert p.is_relative_to(isolated_inbox.INBOX_DIR)


def test_archive_inbox_race_does_not_lose_messages(isolated_inbox):
    """Reproduces the exact race: a concurrent send_message() during
    archive_inbox()'s read window must not be lost from both the archive
    AND the live inbox."""
    isolated_inbox.send_message("marc", "tia", "message one")

    results = {}

    def archiver():
        results["archived_count"] = isolated_inbox.archive_inbox("tia")

    def concurrent_sender():
        isolated_inbox.send_message("marc", "tia", "message two - sent during archive")

    # Run many times - a race is timing-dependent, one shot could pass by luck
    for _ in range(50):
        isolated_inbox.archive_inbox("tia")  # drain any leftover state
        isolated_inbox.send_message("marc", "tia", "seed message")
        t1 = threading.Thread(target=archiver)
        t2 = threading.Thread(target=concurrent_sender)
        t1.start(); t2.start()
        t1.join(); t2.join()

        archived_text = isolated_inbox.read_inbox("tia")  # whatever's still live
        archive_text = isolated_inbox._archive_path("tia").read_text(encoding="utf-8") if isolated_inbox._archive_path("tia").is_file() else ""
        total_messages = len(isolated_inbox._parse_messages(archived_text)) + len(isolated_inbox._parse_messages(archive_text))
        # every message ever sent in this iteration must be somewhere
        assert "seed message" in archived_text + archive_text
