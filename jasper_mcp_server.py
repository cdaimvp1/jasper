"""
jasper_mcp_server.py — MCP server exposing Jasper's own REST API
(server_lean.py) as tools for a live Claude Code session (see
workgraph_assistant.py, the orchestrator that spawns `claude -p --resume`
with this as its --mcp-config).

Deliberately a thin proxy, not new business logic: every tool here is a
direct call to a route that already exists and is already tested
(server_lean.py). This keeps the MCP layer free of duplicated logic and
means a fix to the underlying route is automatically visible here too.

Read tools (search/get_project/get_issue/ranked_actions) are unrestricted.
Action tools are deliberately limited to open-email/draft-reply/draft-
forward - all three create a local Outlook draft or open a window, never
send anything (outlook_actions.py's own contract: Reply()/ReplyAll()/
Forward() + Display(), never Send()) - so a live session calling them
autonomously carries the same risk as Marc clicking the existing cockpit
buttons himself, not a new capability.
"""
from __future__ import annotations

import os
import sys

import requests
from mcp.server.mcpserver import MCPServer

JASPER_API = os.environ.get("JASPER_API_BASE", "http://127.0.0.1:8700")
TIMEOUT = 15

mcp = MCPServer("jasper")


def _get(path: str, params: dict | None = None) -> dict:
    r = requests.get(f"{JASPER_API}{path}", params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    r = requests.post(f"{JASPER_API}{path}", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def jasper_search(query: str, limit: int = 15) -> dict:
    """Full-text search over Jasper's own ingested evidence (emails, Teams
    messages, calendar items already tracked in the work graph). Use this
    FIRST for anything that might be tracked work - it's the curated,
    already-grouped ground truth. Returns raw hits with raw_item_id,
    issue_id, and a matched-text snippet; issue_id may point to a raw
    cluster not yet grouped into a confirmed project (call jasper_get_issue
    to check - a 404 there means "not yet grouped", not an error)."""
    return _get("/api/workgraph/evidence-search", {"q": query, "limit": limit})


@mcp.tool()
def jasper_get_project(project_id: str) -> dict:
    """Full detail for one tracked project: name, category, status, and
    every real tracked issue under it (each with title, state, priority,
    parties). Use after jasper_search or jasper_ranked_actions surfaces a
    project_id, to get the full picture before answering or drafting
    anything about it."""
    return _get(f"/api/workgraph/projects/{project_id}")


@mcp.tool()
def jasper_get_issue(issue_id: str) -> dict:
    """Full detail for one tracked issue: title, project, evidence items
    (each with a raw_item_id you can pass to jasper_open_email/draft_reply/
    draft_forward), parties, and synthesis. 404s if issue_id is actually a
    raw cluster not yet grouped into a confirmed project - treat that as
    'not yet grouped', not a failure."""
    return _get(f"/api/workgraph/issues/{issue_id}")


@mcp.tool()
def jasper_ranked_actions(limit: int = 5) -> dict:
    """Marc's current top next-best-actions, already ranked by Jasper's own
    deterministic scoring (value, urgency, staleness, prerequisites, etc.).
    Use this when asked something like 'what should I look at' or 'what's
    outstanding' rather than re-deriving priority yourself."""
    return _get("/api/workgraph/actions/ranked", {"limit": limit})


@mcp.tool()
def jasper_open_email(raw_item_id: int) -> dict:
    """Open the exact source email in Marc's own Outlook (real COM
    Display(), does not send or modify anything). Use when Marc asks to see
    the original message behind something you found."""
    return _post("/api/action/open-email", {"raw_item_id": raw_item_id})


@mcp.tool()
def jasper_draft_reply(raw_item_id: int, reply_all: bool = False) -> dict:
    """Create a REAL Outlook draft reply to the exact source email (Reply()/
    ReplyAll() + Display() - never Send()). The draft opens empty in
    Outlook; Marc still has to write and send it himself unless you're
    explicitly asked to also compose the body - this tool only opens the
    draft window. Use when Marc asks you to draft or start a reply."""
    return _post("/api/action/draft-reply", {"raw_item_id": raw_item_id, "reply_all": reply_all})


@mcp.tool()
def jasper_draft_forward(raw_item_id: int) -> dict:
    """Create a REAL Outlook draft forward of the exact source email
    (Forward() + Display() - never Send()). Same non-sending contract as
    jasper_draft_reply."""
    return _post("/api/action/draft-forward", {"raw_item_id": raw_item_id})


if __name__ == "__main__":
    mcp.run(transport="stdio")
