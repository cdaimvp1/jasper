"""Regression test for members.py's member_state() path traversal guard
(task #27) - active_file is roster config data, but a bad entry
("../../../../some/file") would otherwise let member_state() read and
excerpt anything reachable from WORKSPACE_ROOT."""
import pytest


@pytest.fixture
def isolated_members(tmp_path, monkeypatch):
    import members
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(members, "WORKSPACE_ROOT", workspace_root)
    return members


def test_traversal_active_file_is_blocked(isolated_members, monkeypatch):
    def fake_get_member(mid):
        return {"id": mid, "name": "Evil", "short": "ev", "role": "x", "kind": "claude_cli",
                "active_file": "../../../../Windows/System32/drivers/etc/hosts"}
    monkeypatch.setattr(isolated_members, "get_member", fake_get_member)

    state = isolated_members.member_state("evil")
    assert "active_md_excerpt" not in state


def test_normal_active_file_still_reads(isolated_members, monkeypatch):
    scratch = isolated_members.WORKSPACE_ROOT / "_test_active.md"
    scratch.write_text("## Right now\nDoing the thing.\n", encoding="utf-8")

    def fake_get_member(mid):
        return {"id": mid, "name": "Good", "short": "gd", "role": "x", "kind": "claude_cli",
                "active_file": "_test_active.md"}
    monkeypatch.setattr(isolated_members, "get_member", fake_get_member)

    state = isolated_members.member_state("good")
    assert "active_md_excerpt" in state
