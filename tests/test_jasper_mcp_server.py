"""Regression tests for jasper_mcp_server.py's self-restart-on-code-change
mechanism (real incident, 2026-08-08): the process is long-lived (task
#231), so a code edit while it's running was previously invisible until
someone noticed the assistant using a stale toolset and manually killed
and relaunched it. These tests exercise the single-tick check in
isolation - os.execv is monkeypatched, never actually called for real."""
from __future__ import annotations

import os

import jasper_mcp_server as jms


def test_check_returns_same_mtime_when_file_unchanged(monkeypatch):
    monkeypatch.setattr(os.path, "getmtime", lambda path: 100.0)
    calls = []
    monkeypatch.setattr(os, "execv", lambda *a: calls.append(a))

    result = jms._check_for_code_change_and_restart_if_needed(100.0)

    assert result == 100.0
    assert calls == []


def test_check_restarts_when_mtime_changes(monkeypatch):
    monkeypatch.setattr(os.path, "getmtime", lambda path: 200.0)
    calls = []
    monkeypatch.setattr(os, "execv", lambda *a: calls.append(a))

    result = jms._check_for_code_change_and_restart_if_needed(100.0)

    assert result == 200.0
    assert len(calls) == 1
    assert calls[0][0] == jms.sys.executable


def test_check_tolerates_a_transient_stat_error(monkeypatch):
    def raise_oserror(path):
        raise OSError("mid-write")
    monkeypatch.setattr(os.path, "getmtime", raise_oserror)
    calls = []
    monkeypatch.setattr(os, "execv", lambda *a: calls.append(a))

    result = jms._check_for_code_change_and_restart_if_needed(100.0)

    assert result == 100.0  # unchanged - next tick gets a fresh chance, no restart on a bad read
    assert calls == []
