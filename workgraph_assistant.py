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
import sys
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws

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
    "mcp__jasper__jasper_focus_email",
    "mcp__jasper__jasper_focus_party",
    "mcp__jasper__jasper_request_contract_review",
    "mcp__jasper__jasper_worker_status",
    "mcp__jasper__jasper_message_worker",
    "mcp__jasper__jasper_teach_prerequisite_rule",
    "mcp__jasper__jasper_check_mail_freshness",
    "mcp__jasper__jasper_refresh_mail_now",
    "mcp__jasper__jasper_draft_review_request",
    "mcp__jasper__jasper_todo_list",
    "mcp__jasper__jasper_mark_output_reviewed",
    "mcp__jasper__jasper_focus_today",
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
    "mailbox/calendar content. When Marc asks to focus on a supplier or "
    "person by name, use jasper_focus_party, not jasper_search - it returns "
    "ready-to-render project cards with suggested actions, not raw hits. "
    "When offering a suggested action, actually offer to do it and follow "
    "through if Marc says yes: jasper_draft_reply/jasper_draft_forward for "
    "drafting, jasper_request_contract_review for reviewing an attached "
    "contract (this queues a real worker run, it does not finish instantly - "
    "say so). Keep replies short and concrete - this renders in a narrow "
    "task pane, not a full chat window. Never claim an email was sent; "
    "jasper_draft_reply/jasper_draft_forward only open a draft window in "
    "Outlook, they never send. When Marc asks what a background worker is "
    "doing, whether anyone's online, or what's in progress, use "
    "jasper_worker_status - never guess. Only use jasper_message_worker "
    "when Marc explicitly asks you to tell/ask/message a specific worker "
    "something; get the exact worker slug from jasper_worker_status first, "
    "and say plainly that it's a real message into that worker's thread, "
    "not an instant action - it may take a while, or go unanswered if that "
    "worker has no live session running. When Marc explicitly teaches a new "
    "prerequisite rule (something shouldn't count as ready/actionable until "
    "something else has happened first), use jasper_teach_prerequisite_rule "
    "- prefix his statement with '#addrule ' on the first call if he didn't "
    "already say it, then relay whatever it replies (it may ask a "
    "clarifying question, or offer confirm/reject) and pass his next reply "
    "verbatim on the following call - including if he wants to cancel or "
    "abandon it (still call the tool with his cancel/never-mind wording "
    "rather than just answering conversationally, since only the tool call "
    "actually clears the in-progress state). Never call this for an "
    "ordinary question. "
    "When Marc asks to share a document (e.g. a contract-review redline) "
    "with named people and ask them to review, use jasper_draft_review_"
    "request with a real attachment_id from that issue's own attachments "
    "list - it drafts a real Outlook email with the file attached, never "
    "sends it. Be explicit that this emails a COPY, not live shared "
    "editing on one file (SharePoint/OneDrive-style co-authoring isn't "
    "wired up yet) - never imply this does more than it does. "
    "Task #274: before answering anything scoped to 'today', 'this "
    "morning', 'just now', 'the latest', or similar - anything implying "
    "the freshest possible mailbox state - call jasper_check_mail_"
    "freshness first. If it says data is stale, call jasper_refresh_mail_"
    "now before answering rather than answering from what Jasper already "
    "has tracked; say plainly that you're checking for the latest mail "
    "first if that takes a moment. More often than not Marc needs to act "
    "on today's email specifically, so a stale answer here is a real "
    "miss, not a minor one - though not every question is time-scoped, so "
    "don't run this check for something like 'what's the status of the "
    "Bluefish contract' that isn't asking about anything recent. "
    "Task #281: when Marc asks something like 'what's my to-do list', "
    "'what do I need to do', or 'what's outstanding', use jasper_todo_list "
    "- render its three sections conversationally (outputs waiting on him, "
    "open claims with type counts, active issues by supplier) rather than "
    "dumping raw JSON; if open_claims.truncated is true, say the real total "
    "rather than implying the sample shown is everything. Offer the same "
    "action a card would for a claim you mention (e.g. 'approve in Ariba', "
    "'draft a reply') using the tools you already have, and use "
    "jasper_mark_output_reviewed when Marc says he's seen/reviewed a "
    "specific output you just showed him. "
    "Task #283: when Marc asks 'what should I focus on today', 'what's "
    "urgent today', or similar same-day framing, use jasper_focus_today "
    "instead of jasper_todo_list - it's scoped to today (top actions, "
    "today's meetings, deliverables due within a week) rather than the "
    "full outstanding picture. If a section is empty, say so plainly "
    "(e.g. 'nothing on your calendar today') rather than padding the "
    "answer or omitting the section silently."
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


def _call_claude_once(message: str, *, session_id: str, is_new: bool, timeout: int) -> dict:
    try:
        proc = _run_claude(message, session_id=session_id, is_new=is_new, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "session_id": session_id, "reply": "Jasper didn't respond in time - try again.",
                "error": "timeout"}
    if proc.returncode != 0:
        return {"ok": False, "session_id": session_id, "reply": "Jasper hit an error.",
                "error": (proc.stderr or "")[-2000:]}
    try:
        result = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "session_id": session_id, "reply": "Jasper returned something unexpected.",
                "error": proc.stdout[-2000:]}
    if result.get("is_error"):
        return {"ok": False, "session_id": session_id, "reply": "Jasper hit an error.", "error": json.dumps(result)[-2000:]}
    return {"ok": True, "session_id": result.get("session_id", session_id), "reply": result.get("result", ""),
            "cost_usd": result.get("total_cost_usd")}


def ask(message: str, session_id: Optional[str] = None, *, timeout: int = _TIMEOUT_SECONDS,
        reset: bool = False) -> dict:
    """One turn of the live assistant. Returns {ok, session_id, reply}.

    Task #232 (2026-08-06): conversation continuity is now server-side,
    not the task pane's own JS variable's job. An explicit session_id
    still wins if the caller passes one (keeps this function's existing
    per-turn contract intact for anyone chaining turns manually); when
    omitted, this looks up the persisted session from workgraph_store
    instead of always minting a fresh one - so a reloaded/reopened pane
    (or a future second host) picks the same ongoing conversation back up.
    reset=True explicitly drops the persisted session first (a real "new
    conversation" action, not just "no session_id happened to be passed").

    A --resume against a persisted session_id can fail on its own (the
    underlying Claude Code session log expired or was never written, e.g.
    right after this feature first shipped) - that failure is handled
    here by falling back to ONE fresh-session retry rather than surfacing
    a confusing error for something the caller had no way to avoid.

    Task #271: also appends both sides of the turn to the visible chat
    log (workgraph_store.append_assistant_chat_turn) - separate from the
    --resume session_id above, which only keeps Claude's own reasoning
    context alive, not what the task pane can redraw after a reload. The
    user's own message is logged unconditionally (even on a failed/timed-
    out turn - Marc did say that, whether or not Jasper answered), the
    reply only on success (a failure's own error text isn't a real
    Jasper reply worth replaying into a restored transcript)."""
    if reset:
        ws.clear_assistant_session_id()
        ws.clear_assistant_chat_turns()

    ws.append_assistant_chat_turn("you", message)

    explicit = session_id is not None
    sid = session_id or ws.get_assistant_session_id()
    is_new = sid is None
    if is_new:
        sid = str(uuid.uuid4())

    result = _call_claude_once(message, session_id=sid, is_new=is_new, timeout=timeout)

    if not result["ok"] and not is_new and not explicit:
        # The persisted session likely no longer exists on disk - retry
        # once as a brand-new conversation rather than leaving Marc stuck.
        fresh_sid = str(uuid.uuid4())
        result = _call_claude_once(message, session_id=fresh_sid, is_new=True, timeout=timeout)

    if result["ok"]:
        ws.set_assistant_session_id(result["session_id"])
        ws.append_assistant_chat_turn("jasper", result["reply"])
    return result
