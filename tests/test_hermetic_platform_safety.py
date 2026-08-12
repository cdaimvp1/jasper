"""Task #368 - a live external-review run of this archive reported 302/14
against a clean 100% pass locally, almost certainly from running
off-Windows (subprocess.CREATE_NEW_PROCESS_GROUP doesn't exist there - a
bare attribute access on a non-Windows subprocess module raises
AttributeError before Popen is even called, regardless of mocking) or
against a stale scratch DB. Confirms the actual fix: every module that used
to reference subprocess.CREATE_NEW_PROCESS_GROUP directly now computes it
once via getattr(..., 0) - a real no-op creationflags value on any
platform, not just a Windows-only literal - so importing/using these
modules never crashes for this reason regardless of OS."""
from __future__ import annotations

import subprocess

import workgraph_assistant
import workgraph_discovery
import workgraph_pipeline2
import workgraph_status_report
import workgraph_synthesis_light

_EXPECTED = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def test_pipeline2_create_process_group_constant_never_raises():
    assert workgraph_pipeline2._CREATE_NEW_PROCESS_GROUP == _EXPECTED


def test_assistant_create_process_group_constant_never_raises():
    assert workgraph_assistant._CREATE_NEW_PROCESS_GROUP == _EXPECTED


def test_discovery_create_process_group_constant_never_raises():
    assert workgraph_discovery._CREATE_NEW_PROCESS_GROUP == _EXPECTED


def test_status_report_create_process_group_constant_never_raises():
    assert workgraph_status_report._CREATE_NEW_PROCESS_GROUP == _EXPECTED


def test_synthesis_light_create_process_group_constant_never_raises():
    assert workgraph_synthesis_light._CREATE_NEW_PROCESS_GROUP == _EXPECTED


def test_fallback_scratch_dir_starts_clean_this_session():
    """Confirms pytest_configure's wipe (conftest.py) actually ran before
    this test executed - the known real leftovers found on this machine
    (dbg.db/dbg2.db/dbg3.db from old ad hoc debugging, none from any
    current test) must not survive into a fresh session."""
    import conftest
    scratch_data = conftest.BODY / "tests" / "_pytest_scratch" / "data"
    for stale_name in ("dbg.db", "dbg2.db", "dbg3.db"):
        assert not (scratch_data / stale_name).exists()
