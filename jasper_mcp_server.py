"""
jasper_mcp_server.py — MCP server exposing Jasper's own REST API
(server_lean.py) as tools for a live Claude Code session (see
workgraph_assistant.py, the orchestrator that spawns `claude -p --resume`
per conversational turn).

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

Task #231 (2026-08-06): runs over SSE as one persistent, long-lived
process (started once, alongside server_lean.py - see START_SERVICES.md
or the equivalent launch step) rather than being spawned fresh by every
`claude -p` turn's stdio --mcp-config. The old stdio-per-turn shape was
correct but wasteful: this server holds no real per-turn state (every
tool call is a fresh, stateless HTTP round-trip to server_lean.py
anyway), so respawning the whole Python process + import graph on every
single conversational turn was pure latency with no benefit.
jasper_mcp_config.json now points `claude -p --mcp-config` at this
process's SSE endpoint instead of a stdio command.
"""
from __future__ import annotations

import os
import sys

import requests
from mcp.server.mcpserver import MCPServer

JASPER_API = os.environ.get("JASPER_API_BASE", "http://127.0.0.1:8700")
TIMEOUT = 15
MCP_HOST = os.environ.get("JASPER_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("JASPER_MCP_PORT", "8701"))

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


@mcp.tool()
def jasper_focus_email(conversation_id: str) -> dict:
    """Focus on the email Marc currently has open in Outlook/Teams. Pass
    Office.js's Office.context.mailbox.item.conversationId verbatim. Returns
    {"matched": false} if that thread isn't tracked yet, or {"matched": true,
    "card": {...}} with the project's summary, every open issue with real
    suggested actions (each carrying open_email/draft_reply/draft_forward
    when it points at a specific message), attachments, and parties. Use
    this when Marc asks something like 'what's this about' or 'what should
    I do with this' about his currently open message."""
    return _get("/api/addin/focus-email", {"conversation_id": conversation_id})


@mcp.tool()
def jasper_focus_party(query: str) -> dict:
    """Focus on a supplier or person by name/company, independent of
    whatever email Marc currently has open. Fuzzy-matches against every
    known contact and returns one focus card (same shape as
    jasper_focus_email's "card") per distinct project that party touches,
    most-recently-active first. Use when Marc asks to 'pull up <supplier>'
    or 'what's going on with <person>'."""
    return _get("/api/addin/focus-party", {"q": query})


@mcp.tool()
def jasper_request_contract_review(issue_id: str, instructions: str = "") -> dict:
    """Dispatch a REAL contract-review skill run for this issue via Jasper's
    existing worker action-bridge (the same mechanism the cockpit's 'Review
    contract' button uses) - a worker wakes, reads the issue's attached
    document(s), and produces a real review. This does not happen instantly;
    it queues a pending action. Use when Marc says something like 'review
    that contract' or accepts an offered contract-review action."""
    return _post("/api/cockpit/actions", {
        "issue_id": issue_id, "action_kind": "review_contract",
        "worker": "bridge", "instructions": instructions,
    })


if __name__ == "__main__":
    mcp.run(transport="sse", host=MCP_HOST, port=MCP_PORT)
