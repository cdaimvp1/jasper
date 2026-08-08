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

Task #234 (2026-08-07): jasper_worker_status/jasper_message_worker expose
the SAME cockpit "Back Office" worker chat Marc already uses from the web
app (GET /api/workers/status, POST /api/post) - not a new capability, just
this same real, already-tested internal messaging surface reachable from
the add-in's own chat box. Distinct from the Outlook-facing action tools
above: this never touches Marc's mailbox or any external recipient, only
Jasper's own cohort of AI workers he already set up and already talks to
via the cockpit - jasper_message_worker only fires on Marc's explicit
ask, same "the human's own request is the approval" posture as
jasper_request_contract_review below.
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
def jasper_worker_status() -> dict:
    """Task #234: real, live status of every background worker in Marc's
    Symphony cohort (the same Claude Code sessions the cockpit's "Back
    Office" chat panel already shows) - liveness (last_activity, bucketed
    live/idle/stale by the caller), and each worker's own self-reported
    current_task/detail when it has one. Read-only, no side effect. Use
    this when Marc asks something like 'what is Colleen doing', 'is anyone
    working on X', or 'are my workers online' - never guess a worker's
    state from anything else."""
    return _get("/api/workers/status")


@mcp.tool()
def jasper_check_mail_freshness() -> dict:
    """Task #274: call this FIRST whenever Marc asks something scoped to
    "today", "this morning", "just now", "the latest", or anything else
    implying the freshest possible mailbox state - never answer a
    time-scoped question from Jasper's own tracked data without checking
    this first. Cheap and read-only (no live Outlook call - just checks
    when mail ingestion last actually ran). Returns {ok, minutes_since_
    last_run}. If ok is False, call jasper_refresh_mail_now before
    answering - more often than not Marc needs to act on today's email
    first, so a stale answer here is a real miss, not a minor one."""
    return _get("/api/mail/freshness")


@mcp.tool()
def jasper_refresh_mail_now() -> dict:
    """Task #274: the "fill the gap" call once jasper_check_mail_freshness
    says data is stale - a real, bounded mail pull (~15-75s, cold Outlook
    start possible), not instant. Say so if Marc is waiting on the reply.
    Only call this after checking freshness first, never speculatively -
    it's a real, if modest, cost each time."""
    return _post("/api/mail/refresh-now", {})


@mcp.tool()
def jasper_message_worker(worker: str, message: str) -> dict:
    """Send a REAL message into one background worker's own DM thread - the
    exact same mechanism as typing '@<worker> ...' into the cockpit's Back
    Office chat. `worker` must be the exact slug from jasper_worker_status
    (e.g. "colleen", not "Colleen" or a guess) - call that tool first if you
    don't already have it from this conversation. This does not happen
    instantly and does not guarantee a reply: it lands in that worker's
    inbox and wakes its notification poller if a live session is already
    running, but a worker with no active session won't see it until someone
    starts one (jasper_worker_status's liveness tells you which). Use this
    only when Marc explicitly asks to tell/ask/message a specific worker
    something - never send a worker instructions on your own initiative."""
    return _post("/api/post", {"from": "marc", "to": f"@{worker}", "body": message})


@mcp.tool()
def jasper_teach_prerequisite_rule(statement: str) -> dict:
    """Task #236: teach Jasper's Aristotle gating a new prerequisite rule -
    "X shouldn't be treated as ready until Y has already happened" (e.g. "a
    signature request shouldn't count as actionable until the contract
    review is done"). Wraps the SAME real, already-built, already-tested
    conversational rule-teaching flow the cockpit's Back Office chat uses
    ("#addrule ..." -> best-effort structuring -> confirm/reject, or a
    clarifying back-and-forth when the statement wasn't specific enough) -
    POST /api/socrates/ask, entirely server-side state keyed by asker, not
    a new mechanism.

    ONLY use this when Marc is explicitly teaching a new rule, or
    continuing/confirming/rejecting one from earlier in THIS SAME
    conversation - never for an ordinary question, which the other
    jasper_* tools already answer better. On the FIRST call for a new rule,
    prefix `statement` with "#addrule " if Marc's own words didn't already
    include it (that prefix is what tells the server this is a rule to
    capture, not a question) - the reflected structure comes back in the
    reply, along with confirm/reject instructions. On every FOLLOW-UP call
    (answering a clarifying question, replying confirm/reject/yes/no), pass
    Marc's literal reply text verbatim, unprefixed - relay whatever this
    tool's reply says back to Marc exactly, since it may be asking a
    specific follow-up question you should wait for his answer to before
    calling this again."""
    return _post("/api/socrates/ask", {"question": statement, "asker": "marc"})


@mcp.tool()
def jasper_list_review_queue() -> dict:
    """Task #260/#262: everything currently waiting on Marc's yes/no across
    every live review queue - claim-resolution suggestions (a raw_item
    said an open ask/commitment was fulfilled) and prerequisite-rule
    suggestions (Aristotle proposing a new "X isn't ready until Y" gate).
    Each item carries a plain-language `description`, a `kind`
    ("claim_suggestion" or "prerequisite_suggestion") and an `id` - pass
    both straight to jasper_resolve_review_item once Marc gives a verdict.
    Use when Marc asks something like "what's waiting on me" or "anything
    to review" - this is read-only, never resolves anything on its own."""
    return _get("/api/workgraph/review-queue")


@mcp.tool()
def jasper_resolve_review_item(kind: str, suggestion_id: int, decision: str) -> dict:
    """Task #260/#262: resolve ONE item from jasper_list_review_queue with
    Marc's explicit verdict - same confirm/reject discipline as
    jasper_teach_prerequisite_rule, just for an already-fully-formed
    proposal rather than one that needs structuring first. `kind` must be
    exactly what jasper_list_review_queue returned for that item
    ("claim_suggestion" or "prerequisite_suggestion"); `decision` must be
    "confirmed" or "rejected". ONLY call this after Marc has given a real
    verdict on a SPECIFIC item you already described to him - never guess
    which item he means from a vague reply, and never resolve something he
    hasn't actually weighed in on."""
    if decision not in ("confirmed", "rejected"):
        raise ValueError('decision must be "confirmed" or "rejected"')
    if kind == "claim_suggestion":
        return _post(f"/api/workgraph/claim-suggestions/{suggestion_id}/resolve",
                     {"status": decision, "actor": "marc"})
    if kind == "prerequisite_suggestion":
        # This route's own body shape uses action="confirm"/"reject" (not
        # status="confirmed"/"rejected" like the claim-suggestion route
        # above) - confirmed by reading server_lean.py's
        # PrerequisiteSuggestionResolveBody directly rather than assuming
        # the two review-queue routes share one convention.
        action = "confirm" if decision == "confirmed" else "reject"
        return _post(f"/api/settings/prerequisite-rule-suggestions/{suggestion_id}/resolve",
                     {"action": action})
    raise ValueError(f'kind must be "claim_suggestion" or "prerequisite_suggestion", got {kind!r}')


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


@mcp.tool()
def jasper_draft_review_request(issue_id: str, to_emails: list[str], attachment_id: int,
                                 message: str = "") -> dict:
    """Task #35 follow-on: drafts a REAL Outlook email (via COM, same
    never-Send()s-itself contract as every other Outlook action here) with
    a real document attached, addressed to to_emails, asking them to
    review it. attachment_id must be one of THIS issue's own attachments
    (get it from jasper_get_issue's/jasper_focus_email's own attachments
    list - never guess an id). Use when Marc says something like 'send
    this to <people> and ask them to review' or 'share the redline with
    my team'.

    Be honest about what this is NOT: this is an emailed COPY of the file,
    not live SharePoint/OneDrive co-authoring on one shared document - if
    Marc specifically asks for native Share-button-style sharing (everyone
    editing the same file), say plainly that isn't wired up yet (it needs
    a new permission grant Jasper doesn't have) rather than implying this
    call does that."""
    return _post("/api/action/draft-review-request", {
        "issue_id": issue_id, "to_emails": to_emails,
        "attachment_id": attachment_id, "message": message,
    })


if __name__ == "__main__":
    mcp.run(transport="sse", host=MCP_HOST, port=MCP_PORT)
