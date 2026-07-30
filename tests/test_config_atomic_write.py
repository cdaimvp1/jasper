"""Regression test for config.write_json_atomic - write-temp-then-os.replace
so a crash mid-write (or two writers landing at once) can't leave
settings.json truncated or half-written."""
import json

import config


def test_write_json_atomic_produces_valid_readable_json(tmp_path):
    path = tmp_path / "settings.json"
    config.write_json_atomic(path, {"manager": {"id": "Marc Lane"}})
    assert json.loads(path.read_text(encoding="utf-8")) == {"manager": {"id": "Marc Lane"}}


def test_write_json_atomic_leaves_no_tmp_file_behind(tmp_path):
    path = tmp_path / "settings.json"
    config.write_json_atomic(path, {"a": 1})
    tmp_files = list(tmp_path.glob("*.tmp*"))
    assert tmp_files == []


def test_write_json_atomic_overwrites_existing_file_completely(tmp_path):
    path = tmp_path / "settings.json"
    config.write_json_atomic(path, {"a": 1, "b": 2, "c": 3})
    config.write_json_atomic(path, {"a": 1})  # much shorter content
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
