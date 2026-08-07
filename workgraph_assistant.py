"""
workgraph_assistant.py - orchestrator for Jasper's live conversational
assistant surface (the Outlook add-in's chat box). Spawns a real, tool-
capable Claude Code session per turn via `claude -p`, NOT the Claude Agent
SDK - confirmed 2026-08-06 that only the CLI itself, not the separate
Agent SDK package, is allowed to use the machine owner's own claude.ai
subscription login (and therefore the already-authorized M365 connector).
See docs/design/M365_PLUGIN_INTEGRATION.md Section 5c.

Each turn is its own short-lived subprocess (same safety primitive as
workgraph_pipeline2._run_headless_claude - CREATE_NEW_PROCESS_GROUP +
taskkill /T /F on timeout, since a `claude -p` subprocess can spawn its
own tool-use grandchildren that survive a naive timeout as orphans),
chained via --resume so the conversation reads as continuous from the
pane's side even though nothing stays alive between turns. Verified live
(2026-08-06): a --resume'd call successfully invoked jasper_ranked_actions
and answered from real Jasper data.

Tool access is an explicit enumerated allowlist, not a wildcard - two
families: this repo's own jasper_mcp_server.py (read + non-sending
Outlook actions only), and the M365 connector's already-authorized,
read-oriented tools (no send/write tool is exposed by that connector
today, confirmed from the live tool list - nothing here grants a NEW
capability beyond what's already authorized).
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

_TIMEOUT_SECONDS = 120
_MCP_CONFIG = str(Path(__file__).resolve().parent / "jasper_mcp_config.json")
_CWD = str(Path(__file__).resolve().parent)

_ALLOWED_TOOLS = [
    # this repo's own Jasper API, wrapped as MCP tools (jasper_mcp_server.py)
    "mcp__jasper__jasper_search",
    "mcp__jasper__jasper_get_project",
    "mcp__jasper__jasper_get_issue",
    "mcp__jasper__jasper_ranked_actions",
    "mcp__jasper__jasper_open_email",
    "mcp__jasper__jasper_draft_reply",
    "mcp__jasper__jasper_draft_forward",
    # the already-authorized M365 claude.ai connector - read/search only,
    # no send/write tool is exposed by it today (confirmed from the live
    # tool list); re-check this list if the connector's tool set changes.
    "mcp__claude_ai_Microsoft_365__chat_message_search",
    "mcp__claude_ai_Microsoft_365__find_meeting_availability",
    "mcp__claude_ai_Microsoft_365__get_me",
    "mcp__claude_ai_Microsoft_365__outlook_calendar_search",
    "mcp__claude_ai_Microsoft_365__outlook_email_search",
    "mcp__claude_ai_Microsoft_365__outlook_find_available_time",
    "mcp__claude_ai_Microsoft_365__read_resource",
    "mcp__claude_ai_Microsoft_365__sharepoint_folder_search",
    "mcp__claude_ai_Microsoft_365__sharepoint_search",
    "mcp__claude_ai_Microsoft_365__teams_list_chats",
]

_SYSTEM_PROMPT = (
    "You are Jasper, a work assistant embedded in Marc's Outlook task pane. "
    "Prefer the jasper_* tools first for anything that might already be "
    "tracked work - they're curated, deduped ground truth. Only reach for "
    "the Microsoft 365 tools when Jasper's own data doesn't have what you "
    "need (something not yet tracked) or the ask is explicitly about live "
    "mailbox/calendar content. Keep replies short and concrete - this "
    "renders in a narrow task pane, not a full chat window. Never claim an "
    "email was sent; jasper_draft_reply/jasper_draft_forward only open a "
    "draft window in Outlook, they never send."
)


def _run_claude(prompt: str, *, session_id: str, is_new: bool, timeout: int) -> subprocess.CompletedProcess:
    args = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--mcp-config", _MCP_CONFIG,
        "--allowedTools", ",".join(_ALLOWED_TOOLS),
        "--append-system-prompt", _SYSTEM_PROMPT,
    ]
    args += ["--session-id", session_id] if is_new else ["--resume", session_id]
    env = os.environ.copy()
    # Load-bearing (confirmed 2026-08-06): connector access requires the
    # cached claude.ai login, not an API key. If ANTHROPIC_API_KEY somehow
    # leaked into this process's environment it would silently kill M365
    # connector access for every turn - strip both defensively.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    proc = subprocess.Popen(
        args, cwd=_CWD, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        # explicit encoding, not just text=True: Windows' default locale
        # codec (cp1252) mangles any non-ASCII char in claude -p's UTF-8
        # JSON output (confirmed live 2026-08-06 - an em dash in a reply
        # came back as "\udc9d" mojibake) before json.loads even runs.
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def ask(message: str, session_id: Optional[str] = None, *, timeout: int = _TIMEOUT_SECONDS) -> dict:
    """One turn of the live assistant. Returns {ok, session_id, reply}.
    A fresh session_id is minted here when none is passed in (new
    conversation) - the caller just needs to persist and resend whatever
    session_id comes back to continue the same conversation next turn."""
    is_new = session_id is None
    sid = session_id or str(uuid.uuid4())
    try:
        proc = _run_claude(message, session_id=sid, is_new=is_new, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "session_id": sid, "reply": "Jasper didn't respond in time - try again.", "error": "timeout"}
    if proc.returncode != 0:
        return {"ok": False, "session_id": sid, "reply": "Jasper hit an error.", "error": (proc.stderr or "")[-2000:]}
    try:
        result = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "session_id": sid, "reply": "Jasper returned something unexpected.", "error": proc.stdout[-2000:]}
    if result.get("is_error"):
        return {"ok": False, "session_id": sid, "reply": "Jasper hit an error.", "error": json.dumps(result)[-2000:]}
    return {"ok": True, "session_id": result.get("session_id", sid), "reply": result.get("result", ""), "cost_usd": result.get("total_cost_usd")}
