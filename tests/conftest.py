"""
tests/conftest.py — shared pytest fixtures for the whole suite.

Sets TEAM_DATA_DIR/TEAM_WORKSPACE_ROOT/TEAM_CONFIG_DIR to a safe, throwaway
location BEFORE anything under test imports paths.py (which hard-requires
TEAM_DATA_DIR at import time - see paths.py's own comment on why that's
non-negotiable). This must happen at collection time, before any test module
is imported, which is exactly what conftest.py guarantees.

Every real test then gets its OWN isolated set of DB files via the ws_db /
bus_db fixtures below (same "reassign the module's DB path, call init_*()"
pattern used ad hoc, by hand, throughout this session's verification work -
now permanent and reusable instead of retyped per test).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BODY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BODY))

# Must be set before ANY test module (or conftest fixture) imports paths.py.
_FALLBACK_DATA_DIR = str(BODY / "tests" / "_pytest_scratch" / "data")
os.environ.setdefault("TEAM_DATA_DIR", _FALLBACK_DATA_DIR)
os.environ.setdefault("TEAM_WORKSPACE_ROOT", str(BODY / "tests" / "_pytest_scratch" / "workspace"))
os.environ.setdefault("TEAM_CONFIG_DIR", str(BODY / "tests" / "_pytest_scratch" / "config"))

import pytest


def pytest_configure(config):
    """Task #368: an external review's run of this same archive reported
    302/14 against a 100%-clean local run - one suspected cause (besides
    the CREATE_NEW_PROCESS_GROUP portability bug fixed in source this same
    task) was "an uninitialized scratch DB". Confirmed live: the fallback
    dir above (_FALLBACK_DATA_DIR, used only when nothing already set
    TEAM_DATA_DIR) had real accumulated leftovers on this very machine -
    dbg.db/dbg2.db/dbg3.db/workgraph.db/bus.db from old ad hoc `python -c`
    debugging, none of it from any current test (every real test already
    goes through ws_db/bus_db/isolated_paths below, each on its own
    pytest tmp_path). Wiping this fallback dir once, here, before ANY test
    runs, means whatever ends up in it (a test/helper that someday forgets
    to override TEAM_DATA_DIR) always starts from a clean, deterministic
    state - never from whatever a previous, unrelated run happened to
    leave behind. A hook, not a fixture, so it can't be skipped by
    collection order or -k filtering."""
    import shutil
    scratch_root = BODY / "tests" / "_pytest_scratch"
    if scratch_root.exists():
        shutil.rmtree(scratch_root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clear_nba_value_cache():
    """workgraph_nba._value_cache is intentionally process-global (see its
    own comment) - real leakage found 2026-08-03: a test file using small,
    sequential raw_item ids that don't happen to mention a dollar figure
    caches an EMPTY candidate list for that id; a later test FILE (in the
    same pytest process) that reuses the same numeric id for a real
    dollar-figured raw_item silently inherits the stale empty entry instead
    of computing its own (confirmed live - test_workgraph_suppliers.py
    failing with 0.0 instead of a real $2M figure, caused by
    test_workgraph_nba_actions_ranked.py running first alphabetically and
    priming that same id). This used to be a `test_workgraph_nba.py`-only
    autouse fixture, which only protected tests within that one file -
    promoted here so every test file sharing this process-global cache
    gets the same guarantee, regardless of collection order."""
    import workgraph_nba as nba
    nba._value_cache.clear()
    yield
    nba._value_cache.clear()


@pytest.fixture
def ws_db(tmp_path, monkeypatch):
    """A fresh, isolated workgraph.db for one test. Returns the workgraph_store
    module with its WORKGRAPH_DB pointed at a per-test tmp file - the exact
    pattern (`ws.WORKGRAPH_DB = tmp; ws.init_workgraph()`) used by hand
    throughout this session's own verification work, now a fixture so every
    test gets a clean, isolated DB with zero cross-test leakage."""
    import workgraph_store as ws
    db_path = tmp_path / "workgraph_test.db"
    monkeypatch.setattr(ws, "WORKGRAPH_DB", db_path)
    ws.init_workgraph()
    return ws


@pytest.fixture
def bus_db(tmp_path, monkeypatch):
    """Same idea as ws_db, for bus.py's events/worker_notifications tables."""
    import bus
    db_path = tmp_path / "bus_test.db"
    monkeypatch.setattr(bus, "BUS_DB", db_path)
    bus.init_bus()
    return bus


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """For tests that need paths.* constants themselves redirected (backup.py,
    retention.py, anything that reads paths.DATA_DIR/DOCUMENTS_DIR/etc
    directly rather than through workgraph_store/bus). Returns the paths
    module with every relevant constant repointed under tmp_path."""
    import paths
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "DATA_DIR", data_dir)
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(paths, "WORKGRAPH_DB", data_dir / "workgraph.db")
    monkeypatch.setattr(paths, "BUS_DB", data_dir / "bus.db")
    monkeypatch.setattr(paths, "DOCUMENTS_DIR", data_dir / "documents")
    monkeypatch.setattr(paths, "DOCUMENTS_RAW_ITEMS_DIR", data_dir / "documents" / "raw_items")
    monkeypatch.setattr(paths, "ATTACHMENT_STAGING_DIR", data_dir / "raw_ingest_inbox" / "_mail_attachments_staging")
    monkeypatch.setattr(paths, "BACKUPS_DIR", data_dir / "backups")
    monkeypatch.setattr(paths, "DB_SNAPSHOTS_DIR", data_dir / "backups" / "db_snapshots")
    monkeypatch.setattr(paths, "CONFIG_SNAPSHOTS_DIR", data_dir / "backups" / "config_snapshots")
    return paths
