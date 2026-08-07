# Design: Jasper as a Microsoft 365 surface (Outlook / Teams)

**Status (updated 2026-08-06):** design, plus a real, verified proof-of-concept and
the start of a real build. Grounded directly in this codebase (file:line citations
throughout) plus `docs/design/CLAUDE_COWORK_INTEGRATION.md` (task #61), which this
document extends rather than duplicates. Where the current mechanism is aspirational
rather than proven, that is stated plainly rather than implied to be working -
several claims below required correcting an initial assumption after reading the
real code.

**What's actually been verified live, not just designed on paper:** Outlook add-in
sideloading works on Marc's own account (found via the modern Outlook's "Apps" →
"Add a custom add-in" → "Add from file" path, `https://aka.ms/olksideload` as a
direct shortcut). A real, hand-built task pane (no npm/webpack - the sample's own
npm install was blocked by this network the same way Ollama's model registry was
earlier, so the working proof-of-concept was built from a hand-written
`taskpane.html` loading Office.js from Microsoft's real CDN, served over a locally-
generated, Windows-trusted HTTPS certificate via Python's stdlib `http.server`, with
Microsoft's own real `manifest.outlook.xml` unmodified except for content) loaded
successfully, rendered inside a real ribbon button, and correctly read the subject
line of the open email via `Office.context.mailbox.item` - full proof the whole
pipe works end to end, not just that the upload dialog exists. This resolves §9.2
below from "unknown" to "confirmed yes."

## 1. Problem this solves

Jasper's own UI (`cockpit.html`/`server_lean.py`) is a second surface Marc has to
check alongside Outlook - a dual-inbox problem no amount of UI polish removes,
because the actual work (email, Teams, meetings) never happens inside Jasper. Jasper's
real job is "watch everything, compute what matters, tell you" - and "tell you"
does not require "look at a separate screen," it requires "reach you where you
already are." This document designs that reach: Jasper's existing graph and
deterministic scoring, delivered into Outlook and Teams, instead of a bespoke
web app competing with them.

**What does not change:** `workgraph_store.py`'s schema, `workgraph_nba.py`'s scoring,
`workgraph_pipeline2.py`'s grouping/extraction pipeline, and `server_lean.py`'s REST
API are the system of record and stay exactly as they are. Every surface below is a
new *client* of that existing API, not a replacement for it. This is a UI/delivery
project, not a backend rewrite.

## 2. Non-negotiable constraints (Marc's own, stated directly)

1. **No Power Automate, anywhere, for any of this.** All orchestration stays inside
   Jasper's own worker/queue mechanism plus direct Microsoft Graph API or MCP calls.
   A second orchestration platform would fragment state and audit trail outside
   Jasper's own claims ledger - the exact problem this whole direction exists to avoid.
2. **The graph stays the canon.** No delivery surface re-derives NBA/priority
   judgment fresh from raw signal each time it's asked - it reads Jasper's own
   precomputed, deterministic answer (`workgraph_nba.recompute_all()`,
   `GET /api/workgraph/actions/ranked`), the same discipline already established for
   Cowork in the prior design doc.
3. **Confirm-before for anything with real consequence; confirm-after only for pure
   information.** Established across this whole design thread (the Ariba-approval
   case, Tia's policy-Q&A case) - carried forward into every action surfaced below.
4. **No new network exposure by default.** `server_lean.py` binds to
   `127.0.0.1:8700` only, has zero authentication, and CLAUDE_COWORK_INTEGRATION.md
   already flagged local-only vs. remote-reachable as "the one real decision" needing
   Marc's sign-off before any build touching that boundary. This document inherits
   that same open decision rather than re-deciding it - see §9.

## 3. Grounded current state - what's real vs. what's aspirational

This section exists because several pieces of this design initially looked more
built than they are. Stated plainly so nothing below overclaims:

### 3.1 The REST API is a real, broad resource surface already
`server_lean.py` has 135 routes. `/api/workgraph/*` (62 routes) is almost entirely
read (`GET`) state - issues, projects, claims, parties, suppliers, ranked actions,
synthesis, attachments/timeline, relationships - with narrow, scoped mutation routes
(status transitions, dismiss/resolve, correct). This is exactly the shape an M365
surface needs to read from: broad, mostly read-only, already exists, no new backend
work required to expose *reading* Jasper's state.

### 3.2 The ranked-actions feed already exists
`workgraph_nba.rank_actions()` (`workgraph_nba.py:936-1003`) returns a flat,
globally-sorted, deterministic list:
```
{claim_id, issue_id, project_id, text, claim_type, score, reason, raw_item_id}
```
already exposed at `GET /api/workgraph/actions/ranked` (`server_lean.py:1740-1749`,
task #106) - a thin, direct passthrough. **This is the exact feed a pushed digest or
a drawer needs** - it is not something to build, only something to call.

### 3.3 The skills registry is data-driven; the trigger logic is not
`skills_registry.json` holds 33 registered skills (`skill_name`, `skill_dir`,
`display_name`, `produces`, `output_kind`, version history) - genuinely data-driven,
no domain names in `skills_registry.py` itself. **But** what decides *when* a skill
fires is `workgraph_recommend.py:72-89` - three hardcoded Python regexes
(`_INVOICE_AUDIT_RE`, `_SCOPE_SOW_RE`, `_MARKET_BENCHMARK_RE`) matched against evidence
text, each gated by a registry-presence check. This is exactly generalization-roadmap
gap #4 (see memory `jasper-generalization-roadmap`) - worth fixing as part of this
work rather than adding a fourth hardcoded regex for an M365-specific trigger.

### 3.4 The action-execution path is a chat message waking a human-equivalent agent, not a job queue - this is the load-bearing gap
This is the single most important correction to make before designing "confirm → runs
in the background → notifies me": that loop does not exist today the way it sounds.

Concretely, `POST /api/cockpit/actions` (`server_lean.py:2937-3001`):
1. Creates a `prepared_actions` row **already in state `"approved"`** - the HTTP
   click *is* the approval, there is no separate policy gate today.
2. Flips it to `"executing"`.
3. Posts a **team_room chat message** (`@bridge [COCKPIT-ACTION] {...}`) - this is
   the actual dispatch mechanism. It relies on the F9 notification fanout waking
   the named worker's armed poller.
4. Creates a `pending_actions` row (`status="requested"`).
5. **Returns to the caller immediately** - before any real work has happened.

The worker side (`ingest/ACTION_BRIDGE_ROUTINE.md`) is a documented *routine*, not a
code-level executor: a Claude Code session wakes, reads the routine doc, follows it -
checks the skill registry, reads the skill's own `SKILL.md`, runs its scripts itself,
saves output as an attachment, writes an evidence row, marks `pending_actions` done.
**There is no code that executes a skill; there is a human-equivalent agent that
executes a documented process**, the same way a real employee would follow a runbook.

And critically: `prepared_actions`' own 7-state lifecycle
(`proposed→ready_for_approval→approved→executing→{succeeded,failed,uncertain}` or
`rejected/expired/cancelled`, `workgraph_store.py:1373-1391`) is **mostly
aspirational**. Only `proposed→approved` and `approved→executing` actually happen in
code today; nothing transitions a row to `succeeded`/`failed`/`uncertain` - the
codebase's own comment says outright that "nothing reports a worker's real-world
outcome back" (`workgraph_store.py:1367-1371`). The only other real transition is a
1-hour expiry sweep that force-marks anything still open as `expired`
(`PREPARED_ACTION_STALE_AFTER_SECONDS = 3600`).

**Implication for this design:** "click Run in Outlook, it works in the background,
you get notified when done" is a genuinely good UX goal, but building it *reliably*
on top of today's mechanism means either (a) accepting a real latency/reliability
profile bounded by "is a cohort worker currently awake and does it follow the routine
correctly, with a hard 1-hour ceiling and no automatic retry," or (b) building a real
completion-reporting path (the worker actually calls back into
`update_prepared_action_state`/`update_pending_action_status` with a terminal result,
which the routine doc's own steps already imply it *should* do but nothing enforces).
Phase 2 below treats (b) as a prerequisite, not an afterthought - notifying Marc
about something that never actually resolves would be worse than not notifying at all.

I also could not find verified evidence of a completed end-to-end contract-review
skill run through this path (no logged attachment, no evidence-row dump) - only the
routine's own note that a *different*, generic draft/summarize run was confirmed once
on 2026-07-28. State this to Marc directly rather than assume it: **the
contract-review skill path is documented and plausible, not proven executed.**

### 3.5 Outlook COM actions map cleanly onto Graph API drafts
`outlook_actions.py`'s 4 functions (`open_email`, `draft_reply`, `draft_forward`,
`compose_new`) all share one invariant: **draft-only, `Send()` is never called.**
Direct Graph equivalents, all equally draft-safe:

| Today (COM, via PowerShell) | Graph API equivalent |
|---|---|
| `open_email` → `Display()` | No direct verb; deep-link the client to `GET /me/messages/{id}` |
| `draft_reply` → `Reply()`/`ReplyAll()` + `Display()` | `POST /me/messages/{id}/createReply` (or `createReplyAll`) |
| `draft_forward` → `Forward()` + `Display()` | `POST /me/messages/{id}/createForward` |
| `compose_new` → `CreateItem(0)` + `Display()` | `POST /me/messages` |

This matters directly for reliability: COM automation is why this session spent real
effort fixing a 20s→120s timeout and an orphaned-`OUTLOOK.EXE` cold-start problem
(`outlook_actions.py:22-36`, fixed 2026-08-06) - Graph API calls have no such problem,
because there's no desktop process to cold-start. **An M365-native rebuild of these
four actions is a reliability upgrade on its own**, independent of the rest of this
document.

### 3.6 The MCP adapter this all sits on top of is already designed, not yet built
CLAUDE_COWORK_INTEGRATION.md (task #61) already proposes `workgraph_mcp_server.py` -
a thin MCP wrapper exposing read resources (issues, suggestions, NBA ranking) and
tools (dismiss/snooze, skill dispatch via the exact mechanism in §3.4, compose) over
the existing REST API, local-only (`127.0.0.1`) to start. This document does not
re-propose that server; it assumes it as the substrate every surface below talks
through, and treats "should it ever become remote-reachable" as the same still-open
decision that doc already flagged.

## 4. Architecture: three layers

```
Connectors            Ariba / ServiceNow / SAP / Adobe Sign / Outlook / Teams / SharePoint
     |                (each adds ground-truth evidence; M365 connector already live -
     v                 outlook_email_search, calendar_search, chat_message_search,
Graph + Scoring         teams_list_chats, sharepoint_search, get_me, find_meeting_availability)
(workgraph_store.py,
 workgraph_nba.py,           <- durable canon, deterministic, auditable, unchanged by this doc
 workgraph_pipeline2.py)
     |
     v
workgraph_mcp_server.py  <- thin, local-only MCP adapter (§3.6, already designed)
     |
     v
Delivery surfaces        On-demand query -> Pushed digest -> Embedded drawer  (§5, in this order)
(Claude/Cowork in
 Outlook or Teams,
 or a real Add-in)
```

Investment in the top two layers compounds regardless of what happens in delivery -
every new connector and every scoring improvement pays off whether the eventual
delivery surface is a chat answer, a digest, or a drawer. Delivery is the only layer
this document treats as genuinely undecided, which is why §5 is structured as a
sequence to validate cheaply, not a single committed design.

## 5. Delivery surfaces, in recommended build order

### 5.0 Execution model - who actually performs a Microsoft 365 action (resolved 2026-08-06)

Every surface below eventually needs to *do* something in M365 (send a digest,
create a draft, write to OneDrive) - and there are two structurally different ways
to make that happen:

- **(A) Jasper's own backend acts autonomously** - a real, separate Azure AD app
  registration, its own delegated Graph token (acquired and refreshed locally, under
  Marc's identity), calling Graph directly from `server_lean.py`/a new module. Fully
  autonomous, no Claude invocation required to fire an action, but a new service
  identity needing its own IT/security review - on top of, not instead of, the §5c
  tenant-approval question.
- **(B) Claude performs it**, using the M365 connector already authorized on Marc's
  account today. Jasper computes *what* should happen (the ranked digest, the
  decision to run a skill); a Claude invocation (scheduled, or on-demand) is the thing
  that actually calls into M365 to do it. No new app registration, no new approval
  gate, ships against everything already working.

**Decision: (B) for now.** Marc's own framing: "right now Claude can do it... eventually
I may want Jasper to do it and point it at our own internal GPUs." This is a two-phase
answer, not just a preference for B - it's an explicit near-term/long-term split:

- **Near-term (this document's Phases 0-4):** Claude is the executor, via the already-
  working M365 connector. §5b's digest and §6's OneDrive-write/notify steps are
  designed against this - a Claude invocation (scheduled or on-demand) performs the
  actual Graph call, Jasper never touches Microsoft's APIs directly.
- **Long-term, explicitly deferred, a separate initiative, not part of this document's
  phased plan:** Jasper becomes its own autonomous executor (option A above), powered
  by a model hosted on Lilly's own internal GPU infrastructure rather than calling
  Claude's API - a shift in *what runs Jasper's judgment/extraction*, not only in who
  calls Graph. Important to flag precisely rather than conflate with the earlier local-
  model test: that test failed because of *this specific laptop's* hardware (no
  discrete GPU) and *this network's* throttling on model downloads - neither
  constraint says anything about Lilly's actual internal GPU infrastructure, which is
  a genuinely separate, currently-unscoped feasibility question (nothing is known yet
  about what that infrastructure actually offers). Revisit as its own real assessment
  once there's something concrete to evaluate, not assumed to inherit the earlier
  "no."

**Why this split doesn't cost anything architecturally:** the graph and scoring
(§4's middle layer) stay identical regardless of which actor sits in the execution
slot - Claude-executes-now and Jasper-executes-later-on-internal-GPUs are two
different actors filling the same role, not two different designs. Nothing in Phases
0-4 needs to be rebuilt when/if the long-term shift happens.

### 5a. On-demand conversational query (near-zero build)

Marc already successfully asked Claude (via the live M365 connector) to triage two
weeks of unread mail - confirmed working today, no build required for that half. The
gap it leaves is statelessness: ask again tomorrow, Claude re-reads everything from
scratch with no memory of yesterday's conclusions. Closing that gap is exactly
standing up `workgraph_mcp_server.py` (§3.6) alongside the M365 connector, so the same
conversation can pull *both* fresh raw signal (M365 connector) and durable graph state
(Jasper MCP server) - "what's new" and "what's the accumulated state of this deal"
answered together, in the same place Marc already tested successfully.

**Build:** the MCP server from §3.6. **Nothing else.** Lowest-risk, most validating
first step - proves whether the graph's answers are actually useful to consume this
way before investing further.

### 5b. Pushed digest (low build)

A scheduled job (extends `ingest/scheduled_refresh.py`'s existing cadence) calls
`GET /api/workgraph/actions/ranked`, formats the top N with `reason` strings already
generated by `score_claim`, and sends it - Teams message via the same mechanism the
cohort workers already use to post, or an Outlook draft-and-notify via the Graph
mapping in §3.5. This requires no new UI surface at all, and answers the "scan a
short list" need without a persistent panel.

**Build:** a formatter + one send call. Reuses `rank_actions()` and the Graph-mapped
send/draft calls from §3.5-3.6 entirely.

### 5c. Embedded drawer: Outlook Add-in task pane, backed by a live Claude session

**Confirmed buildable, not just theoretically feasible** - see the Status note above.
Sideloading, local HTTPS serving, and Office.js's real mailbox access all work today,
proven live, not assumed. The design below is the real target architecture for this
surface, refined through direct back-and-forth with Marc past the original "dumb
webpage client" framing - that framing undersold what's actually possible and is
superseded by this section.

**The core shift: the pane is not a static client of the REST API - it's the front
end for a standing, persistent Claude session tied to the pane's own lifecycle.**
Rather than the pane's JavaScript making its own `fetch()` calls to `server_lean.py`
and rendering the results (the original framing), the pane opens a fast local
connection (WebSocket, to a small local process using the Claude Agent SDK) to a real,
conversational Claude session that starts when the add-in loads. That session:

- Holds an actual live conversation with Marc - the real thing, typed directly in the
  pane, not a canned dashboard of buttons.
- Has tool access to Jasper's own graph (via `workgraph_mcp_server.py`, §3.6) and
  whatever else a Claude Code/Cowork session can reach, the M365 connector included.
- Can execute background work itself (matching the pattern already used constantly in
  this very build process - kick off a long task, keep working, get notified when it
  resolves) rather than always relaying through `team_room`.

**Two execution paths, not one, matching two different real needs:**

1. **Live path (Marc is actively in Outlook right now).** The standing session backing
   the pane either runs a task itself as a background tool call (fire-and-forget,
   session stays free to keep talking) or explicitly delegates it to a cohort worker
   over `team_room` (a genuinely separate process, matching Marc's own mental model of
   "another agent running it in the background") - either way, asking for a contract
   review on Project A and immediately jumping to draft a response on Project B both
   proceed concurrently; the conversational thread never blocks on the first task.
2. **Ambient path (nothing needs to be open).** The existing `scheduled_refresh.py`
   cadence and cohort workers remain the path for anything that has to happen whether
   or not Marc has Outlook open - including a genuinely new capability this design
   unlocks: **assigning a specific worker an overnight job** (e.g. "draft responses to
   these flagged items for my review tomorrow morning") as a scheduled task riding the
   same mechanism, not a new one.

These two paths are complementary, not competing - the live session is the fast path
for "I'm here right now," the cohort/team_room mechanism stays the path for "handle
this whether or not I'm looking."

**Notification: push, not poll, and unified with the chat itself.** Polling
`team_room` on an interval adds a real, avoidable delay on top of however long a
worker actually takes to do the work (which push cannot shrink - that part is real
agentic work, not a network hop). Fix: `server_lean.py` gains a Server-Sent-Events (or
WebSocket) stream that pushes the moment a new `team_room` message lands, instead of
the pane asking on a timer. And rather than inventing a separate "completed work"
notification tray, the same chat surface used to talk to workers is where their
replies land - when a cohort worker finishes a skill run, it posts back into
`team_room` the way it already would, and the pane's chat view surfaces that reply
exactly like any other message, with a link to the real output artifact (the skill's
existing attachment-row mechanism, §3.4 - no new storage, just a link surfaced in an
existing chat).

**This is a hard dependency on §3.4's completion-reporting gap being fixed first, not
optional polish for this specific capability** - without a worker reliably reporting
a real terminal state, there is nothing genuine to push or display; the pane would
have nothing real to show beyond "still executing" forever. Phase 1 (§10) exists
specifically because this capability needs it, not as generic hygiene.

**Project-context-following, with a single source of truth.** The pane tracks exactly
one piece of state - "which project is currently shown" - settable two ways that must
never fight each other:
- **Automatic:** Office.js's `ItemChanged` event fires when Marc selects a different
  email; the pane resolves that item's identity against Jasper's existing
  identity/matching machinery to find its associated project, if one exists. Real
  caveat: this only resolves for mail Jasper has already ingested and grouped - a
  message that landed seconds ago and hasn't been through a classify pass has no
  project to jump to yet.
- **Explicit:** Marc can just ask the live session directly ("show me the current
  Authenticx projects"), which runs a normal project search/lookup - identical
  mechanism to any other conversational query, no special-casing needed.

Both write to the same "current project" state, so an explicit ask cleanly overrides
whatever the open email would have auto-selected, with no conflict between the two
triggers.

**Pinning**, so the pane persists across message navigation instead of reopening per
email: the manifest's `<SupportsPinning>` element (`VersionOverrides` v1.1) plus a
registered `ItemChanged` handler - real, Microsoft-documented capability, confirmed
via direct lookup rather than assumed. One honest caveat from Microsoft's own docs:
a pinned pane can still be torn down and reloaded on certain security-context changes
(switching mail accounts, for example); their own fix is persisting small state via
the `RoamingSettings` API so a reload isn't jarring - worth building in from the start
rather than retrofitting.

**Lifecycle discipline, learned directly from this session's own Outlook-COM
timeout/orphan-process work (§3.5):** the standing Claude session has to start when
the pane loads and actually terminate when Outlook closes or the pane goes idle, or
this recreates the exact orphaned-background-process problem already fixed once this
session, just in a new place. Build the teardown path deliberately, not as an
afterthought.

**Teams personal-app tab** remains the Teams equivalent of this same drawer concept -
a pinned tab in the left rail, same underlying architecture, not designed further
here since Outlook is the immediate target.

**The real gate is not technical feasibility, it's tenant approval** for wider
deployment beyond Marc's own sideloaded testing - already resolved for *personal use*
(confirmed live), but a from-scratch org-wide rollout would still be an IT/security-
review question, the same category of "not an engineering decision" gate already
flagged for the Ariba API idea.

**Design note carried from earlier discussion, still true:** a Project (40 emails, 3
meetings, a checklist) doesn't map cleanly onto "the one item currently open" as a
*default* view - which is exactly why the pane's content is Jasper's own persistent,
cross-cutting state (the ranked list, the current project via the logic above)
rather than "info about this one email," and why `cockpit.html` (unchanged, still
running) remains the occasional deep-dive view for a full project's rollup - an
asymmetric division of labor between two surfaces, not a straight replacement.

## 6. Worked example: ambient contract-review workflow, mapped end to end

Marc's own example, mapped against real mechanism, showing what's built vs. genuinely
new:

| Step | Mechanism | Status |
|---|---|---|
| Detect a contract-review request in Outlook/Teams | `workgraph_classify.py`'s ingestion + classification pass | **Built** |
| Grab the attachment | Attachment ingestion + DOCX text extraction (task #129) | **Built** |
| Ask Marc to confirm before running | A confirm-before UI action (drawer, digest link, or conversational yes/no) → `POST /api/cockpit/actions` | **Built** (the route), **new** (the M365-native confirm surface) |
| Run the skill in the background | Team_room dispatch → bridge worker follows `ACTION_BRIDGE_ROUTINE.md`, reads `contract_review`'s `SKILL.md` | **Built but unproven** (§3.4) - hardening the completion-report path is a real prerequisite, not optional polish |
| Notify Marc when complete | Nothing today reaches Marc via Outlook/Teams on completion - only Claude Code's own internal task-notification exists | **New** - a Claude invocation performs the send via the M365 connector (§5.0-B), same mechanism as 5b |
| Tag the right SMEs on the right clauses | An existing AI-native SME-panel design already scoped for `contract_review` specifically (tasks #45/#50) | **Designed**, re-read before building fresh rather than reinventing |
| Track what's closed | `workgraph_claims.py`'s claims ledger + issue state machine generalizes to this; would need a claim/issue shape specific to "clause under SME review" | **Mostly built**, needs a shape extension |
| File to OneDrive | Current M365 connector tools are read/search only - writing is new | **New** - `POST /me/drive/items/{parent-id}:/{filename}:/content` via Graph; per §5.0-B, a Claude invocation performs this, not Jasper's backend directly - confirm the connector's existing consent actually covers a write scope (Files.ReadWrite) before assuming it does, see §9 |
| Decide it's ready to return to the supplier | A real supplier-facing action | **Should stay confirm-before**, same reasoning as the Ariba-approval case - not covered by the same single upfront yes that started the chain |

## 7. Auth & identity model

Under §5.0's resolved near-term decision (Claude executes, via the connector),
**Jasper itself requests no new Microsoft permissions and holds no Graph credentials
at all** - it never talks to Microsoft's APIs directly:

- **Every M365 read or write is performed by a Claude invocation**, using the
  delegated Graph consent already granted to the M365 connector on Marc's account.
  Whether that existing consent already covers the *write* scopes this design needs
  (Files.ReadWrite for OneDrive, Chat.ReadWrite for posting to Teams) is unconfirmed -
  the connector's exposed tools today are read/search only, which doesn't prove the
  underlying grant is read-only, but shouldn't be assumed either way. Confirm before
  building §6's OneDrive-write step specifically (see §9).
- **`workgraph_mcp_server.py` stays local-only** (`127.0.0.1`), inheriting
  CLAUDE_COWORK_INTEGRATION.md's own recommendation - no new network exposure, no new
  auth story needed for it, since it never leaves Marc's own machine, and it never
  proxies to Microsoft either (it only wraps Jasper's own local REST API).
- **The graph (SQLite, `workgraph_store.py`) never leaves the local machine.** M365
  connector calls and any future Ariba/ServiceNow/SAP connectors (per the earlier
  evolution-vision memory) are read *into* the graph as evidence; the graph itself is
  not synced or mirrored anywhere.
- **If/when the long-term shift (§5.0) happens** and Jasper becomes its own executor,
  this section needs a real rewrite - a genuine Azure AD app registration, delegated
  (or possibly app-only) Graph permissions under Jasper's own service identity, and
  local token acquisition/refresh plumbing that doesn't exist today. Not designed here
  since it's explicitly deferred; flagged so it isn't forgotten when that day comes.

## 8. Governance & guardrails (carried forward, not re-decided)

| Action class | Example | Gate |
|---|---|---|
| Pure information | Answering a policy question (Tia's case) | Confirm-*after* - answer, log as evidence, spot-check later |
| Draft-only mail action | Draft reply/forward/compose (§3.5) | Already safe by construction - never sends |
| Run a registered skill | Contract review | Confirm-*before* - and per §3.4, needs a real completion signal before Marc should trust "it's running" |
| Write to an internal system | File to OneDrive, tag an SME | Confirm-*before* the chain starts is enough IF the target is verified server-side, never inferred from LLM-rendered text (same prompt-injection discipline as v2.8/§12.10) |
| Write to an external/supplier-facing system | Approve a PR, send a document back to a supplier | Confirm-*before*, per-action, never inherited from an earlier yes in the same chain - and gated on real compliance sign-off for anything touching an actual approval control, exactly as already flagged for the Ariba case |

Per-org configurability of this table is generalization-roadmap gap #5 - today these
are judgment calls made for Marc's own procurement context; a second org would need
its own version of this table, not a hardcoded one.

## 9. Open decisions (ask, don't guess)

1. **Local-only vs. remote-reachable MCP server** - inherited unresolved from
   CLAUDE_COWORK_INTEGRATION.md; this document does not change that recommendation
   (local-only first).
2. ~~Lilly tenant app-approval policy for personal sideloaded use~~ - **resolved,
   confirmed live 2026-08-06**: sideloading a custom add-in works on Marc's account
   today. Still open specifically for *org-wide* deployment beyond personal use, which
   remains a real, separate IT/security-review question, not the same question.
3. **Whether to harden §3.4's completion-reporting path before or as part of** the
   first skill-dispatch build - recommended: before, and now a hard prerequisite for
   §5c's push-notification/chat-reply design specifically, not just general hygiene.
4. **Does the M365 connector's existing consent cover the write scopes this design
   needs** (Files.ReadWrite for OneDrive, Chat.ReadWrite for Teams posting), or does
   that consent need to be extended? Under §5.0's resolved decision this is a smaller
   ask than a new app registration (no new service identity, no new app to review) -
   but it's still a real, unconfirmed question, not something to assume either way.
5. **Standing-session cost and lifecycle policy** - a live Claude Agent SDK session per
   open-Outlook-session has a real, non-zero ongoing resource cost compared to today's
   one-off headless calls. Worth a real decision on idle-timeout behavior (how long
   does it stay warm after Marc stops interacting) rather than either leaving it
   running indefinitely or tearing it down so aggressively it feels sluggish to resume.

## 10. Phased build plan

**Marc's go-ahead to start building landed 2026-08-06** - the plan below is now a real
build sequence, not a hypothetical. Sequencing logic unchanged: prove the cheapest
thing first, harden reliability before layering more on top of it, save the largest
new architecture (the live-session drawer) for last since it depends on the most
prerequisites.

- **Phase 0a (building now)** - the first real, testable increment on the already-
  proven sideload rig: swap the proof-of-concept's placeholder Contoso content for
  real Jasper branding and a live call to `GET /api/workgraph/actions/ranked`, so
  Marc sees genuine Jasper data rendered inside real Outlook. Still the "dumb client"
  shape at this stage, deliberately - proves the data path before the live-session
  architecture is layered on top of it.
- **Phase 0b** - `workgraph_mcp_server.py` (§3.6/§5a), local-only, read-only resources.
- **Phase 1** - Harden the action-completion path (§3.4's real gap) - now a hard
  prerequisite for §5c's push/chat-reply design, not just corrective hygiene.
- **Phase 2** - Pushed digest (§5b), reusing `rank_actions()` + the hardened send path.
- **Phase 3** - OneDrive write + Graph-based Outlook/Teams completion-notify (§6's two
  genuinely-new pieces), contingent on §9.4's scope question.
- **Phase 4** - The real target architecture from §5c: the live Claude-Agent-SDK-backed
  pane, push notifications, dual live/ambient execution paths, project-context-
  following, and pinning. The largest phase, built once 0-3 are real and Phases 0-2
  have shown the underlying data/notification paths actually work.
- **Deferred, separate initiative (§5.0's long-term path)** - Jasper becomes its own
  executor: a real Azure AD app registration, its own Graph credentials, and a model
  hosted on Lilly's internal GPU infrastructure powering its judgment/extraction
  instead of Claude's API. Explicitly not scheduled as a phase of this plan - revisit
  once there's something concrete to evaluate about that infrastructure. The Graph-
  native rebuild of `outlook_actions.py`'s four COM functions (§3.5) belongs here too,
  not in an earlier phase: it would require Jasper's own Graph credentials the same
  way, even though the actions themselves are low-risk and draft-only. The existing
  COM-based versions already work (fixed 2026-08-06) and don't need to change until
  this phase is actually taken up.

## 11. Explicitly out of scope for this document

- Multi-org generalization mechanics themselves (per-org signal-vocabulary discovery,
  the config layer) - tracked separately in memory `jasper-generalization-roadmap`;
  this document assumes Marc's own single-org config throughout.
- Any specific external system connector (Ariba/ServiceNow/SAP/Adobe Sign) - tracked
  in `jasper-cowork-evolution-vision`; this document is scoped to the M365 surface
  specifically, which is a prerequisite-independent piece of that larger vision.
