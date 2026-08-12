"""Task #377: targeted tests for the SSE push companion to
/api/addin/output-badge (server_lean.py).

The new route (`GET /api/addin/output-badge/stream`) re-checks the exact
same two counts the pre-existing poll route already exposes -
wg.count_unreviewed_worker_outputs() + wg.count_unacknowledged_proactive_
actions() - and writes an SSE `data:` frame whenever that number changes.
These tests confirm:
  1. the stream's first frame matches whatever the poll route itself
     currently reports (same underlying trigger condition, not a fake
     timer-driven value), and
  2. the frame reflects a real state change (a worker attachment landing,
     then being marked reviewed) rather than a value that never moves.

Environment note (expected, not a bug in this change): importing
server_lean requires the `fastapi` package, which is NOT installed in the
shell task #377 was built in - a separate, pre-existing environment gap
in the same category as tests/test_jasper_mcp_server.py's missing `mcp`
package (see that file's own situation). This file will fail to COLLECT
in that shell for that reason alone; it was not executed there as part of
verifying this task, and server_lean.py was instead verified with
`python -m py_compile server_lean.py`. It is written to run cleanly once
fastapi is available (e.g. the real deployed environment), matching every
other assumption this test suite already makes about its own dependencies.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import server_lean


@pytest.fixture
def client(ws_db, bus_db):
    return TestClient(server_lean.app)


def _first_data_frame(line_iter):
    """Pull the first real `data: ...` SSE frame out of a streamed
    response, skipping any leading `: keep-alive` comment lines."""
    for raw_line in line_iter:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError("stream closed with no data frame")


def test_stream_content_type_is_event_stream(client):
    with client.stream("GET", "/api/addin/output-badge/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        _first_data_frame(resp.iter_lines())


def test_stream_first_frame_matches_poll_route_count(client, ws_db):
    ws_db.create_attachment(
        entity_type="issue", entity_id="issue-1", kind="docx", filename="redline.docx",
        stored_path="redline.docx", content_type="application/octet-stream",
        size_bytes=10, sha256_hex=None, uploaded_by="relay",
    )
    poll = client.get("/api/addin/output-badge")
    assert poll.json()["count"] == 1

    with client.stream("GET", "/api/addin/output-badge/stream") as resp:
        frame = _first_data_frame(resp.iter_lines())
    assert frame == {"count": 1}


def test_stream_reflects_zero_after_the_real_trigger_clears(client, ws_db):
    """The badge's real trigger is Marc's explicit 'mark reviewed' click
    (mark_attachment_reviewed), not a timer - confirm the stream's very
    first frame already reflects that, i.e. it computes fresh on connect
    rather than caching a stale value from before the review."""
    attachment_id = ws_db.create_attachment(
        entity_type="issue", entity_id="issue-2", kind="docx", filename="out.docx",
        stored_path="out.docx", content_type="application/octet-stream",
        size_bytes=10, sha256_hex=None, uploaded_by="curator",
    )
    ws_db.mark_attachment_reviewed(attachment_id)

    with client.stream("GET", "/api/addin/output-badge/stream") as resp:
        frame = _first_data_frame(resp.iter_lines())
    assert frame == {"count": 0}


def test_poll_route_still_works_unchanged(client, ws_db):
    """Task #377 must not remove/alter the existing poll route - it stays
    the pane's initial-load/fallback fetch."""
    resp = client.get("/api/addin/output-badge")
    assert resp.status_code == 200
    assert resp.json() == {"count": 0}
