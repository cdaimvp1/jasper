"""Regression tests for config.py / members.py (task #27):
a malformed settings.json/members.json used to be silently swallowed (bare
`except: pass`), falling back to the last-known-good cache with zero signal
the file on disk had gone bad. Now logs to stderr; behavior otherwise
unchanged (must never crash a live server over a bad file)."""
import io
import contextlib


def test_config_load_logs_parse_failure_and_falls_back(tmp_path, monkeypatch):
    import config
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(config, "_cache", {"stale": "value"})
    monkeypatch.setattr(config, "_cache_mtime", 0.0)

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = config._load()

    assert result == {"stale": "value"}
    assert "[config] failed to parse" in buf.getvalue()


def test_members_load_logs_parse_failure_and_falls_back(tmp_path, monkeypatch):
    import members
    members_path = tmp_path / "members.json"
    members_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(members, "MEMBERS_PATH", members_path)
    monkeypatch.setattr(members, "_cache", [{"id": "stale_worker"}])
    monkeypatch.setattr(members, "_cache_mtime", 0.0)

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = members._load()

    assert result == [{"id": "stale_worker"}]
    assert "[members] failed to parse" in buf.getvalue()
