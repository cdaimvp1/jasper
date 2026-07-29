"""
config.py — runtime-editable configuration.

Single source of truth for caps, routing rules, scheduler intervals, manager id.
Backed by config/settings.json. Hot-reloadable: get() always re-reads if the
file mtime changed.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from paths import CONFIG_DIR

SETTINGS_PATH = CONFIG_DIR / "settings.json"


def write_json_atomic(path: Path, data: Any) -> None:
    """Write-temp-then-rename, not a direct write_text(). A crash mid-write
    (or two writers landing at once) used to be able to leave settings.json
    truncated or half-written - every reader of it (config.get, several
    server_lean.py endpoints) would then silently degrade or throw on the
    next read, with no obvious cause. os.replace() is atomic on both
    Windows and POSIX: the target is always either the old complete content
    or the new complete content, never a partial write."""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_cache_mtime: float = 0.0


def _load() -> dict[str, Any]:
    global _cache, _cache_mtime
    try:
        m = SETTINGS_PATH.stat().st_mtime
    except OSError:
        return _cache or {}
    if m == _cache_mtime and _cache:
        return _cache
    try:
        _cache = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        _cache_mtime = m
    except Exception:
        pass
    return _cache or {}


def get(*keys: str, default: Any = None) -> Any:
    """Read a nested key, e.g. config.get('dispatcher', 'max_concurrent')."""
    with _lock:
        d = _load()
    for k in keys:
        if not isinstance(d, dict): return default
        if k not in d: return default
        d = d[k]
    return d


def set_value(value: Any, *keys: str) -> None:
    """Write a nested key. Persists to disk."""
    if not keys:
        raise ValueError("set_value requires at least one key")
    with _lock:
        d = _load()
        if not isinstance(d, dict): d = {}
        cur = d
        for k in keys[:-1]:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = value
        write_json_atomic(SETTINGS_PATH, d)
        global _cache_mtime
        _cache_mtime = SETTINGS_PATH.stat().st_mtime


def all_settings() -> dict[str, Any]:
    with _lock:
        return dict(_load())
