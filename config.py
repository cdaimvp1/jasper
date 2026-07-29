"""
config.py — runtime-editable configuration.

Single source of truth for caps, routing rules, scheduler intervals, manager id.
Backed by config/settings.json. Hot-reloadable: get() always re-reads if the
file mtime changed.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from paths import CONFIG_DIR

SETTINGS_PATH = CONFIG_DIR / "settings.json"

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
        SETTINGS_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
        global _cache_mtime
        _cache_mtime = SETTINGS_PATH.stat().st_mtime


def all_settings() -> dict[str, Any]:
    with _lock:
        return dict(_load())
