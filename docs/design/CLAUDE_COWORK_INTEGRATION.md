# Research + design: Claude Cowork on top of Jasper (task #61)

**Status:** research + design only. No code changed. Grounded in live web research
(2026-08-04) since Cowork is a real Anthropic product released after this session's own
knowledge cutoff - not something I had prior grounding on. Sources at the bottom.

## What Claude Cowork actually is (confirmed, not assumed)

- A general-computing agent distinct from Claude Code - built primarily for
  **nontechnical users**, automating multistep workflows across a person's own files,
  folders, and applications with less constant prompting than Claude Code needs.
- Timeline: public betas on macOS (Jan 2026) and Windows (Feb 2026), GA April 9 2026.
  Expanded to web (claude.ai) and mobile (iOS/Android) in beta starting July 7 2026, for
  Max plan subscribers.
- The new web/mobile surfaces run on a **materially different execution model**: remote
  sessions hosted on Anthropic's own servers (not the user's device), which can keep
  working - including running scheduled tasks - with no device online at all, and can
  proactively seek a decision from the user via phone.
- **Cowork fully supports MCP** (the Model Context Protocol) for connecting to external
  tools/data - the same mechanism this Jasper install's own Claude Code sessions already
  reach real MCP servers through (visible in this environment's own tool list). Cowork's
  skill model (`~/.claude/skills/`, SKILL.md format, auto-triggering) is also the same
  shape as Jasper's own `skills_registry.py` - not a coincidence, both are the Claude
  Skills format.

## Where this actually connects to Jasper

Jasper already exposes almost everything Cowork would need through its own REST API
(`server_lean.py`) - issues, pending suggestions, the "run any skill" dispatch task #112
just generalized, the stakeholder-compose action from task #35. The real integration
point is **not** replacing any of Jasper's own workers or architecture - it's giving
Marc's *personal* Cowork instance a way to see and act on Jasper's state.

**Proposed shape:** a small new MCP server module inside Jasper (`workgraph_mcp_server.py`
or similar, using Anthropic's official Python MCP SDK) that wraps the existing REST API,
not a parallel implementation:

- **Resources** (read-only): open issues, pending suggestions, gated items, the "your
  next move" ranking - straight passthroughs to functions that already exist
  (`workgraph_nba`, `workgraph_projects`, etc.), not new logic.
- **Tools** (callable): dismiss/snooze an issue, run a registered skill on an issue
  (task #112's exact mechanism - `skills_registry.list_all()` + the same
  `/api/cockpit/actions` dispatch), compose to selected stakeholders (task #35). Every
  one of these already exists as a real, tested function; the MCP server is a thin
  adapter, not new business logic.

Marc would add this MCP server to his own Cowork configuration the same way any MCP
server gets added to Claude today - at that point Cowork can be asked things like "did
anything new land in Jasper that needs my attention" or "run the contract review skill
on the Acme SOW" from wherever Cowork itself runs, in Marc's own words, without opening
the cockpit UI.

## The one real decision this needs from Marc before any build

Jasper's server (`server_lean.py`) runs on `127.0.0.1:8700` - not reachable from
anywhere but this machine. That's fine for local Cowork (the desktop app, running on
this same box) but **not** for the web/mobile remote-hosted Cowork sessions, which
execute on Anthropic's own servers and would need a real network path to reach an MCP
server sitting on Marc's laptop.

Two honest options, not a recommendation to pick between them myself - this is a real
exposure decision:
1. **Local-only scope**: the MCP server binds to localhost, usable only when Cowork
   itself runs on this same machine (the desktop app). Zero new exposure, works today,
   but doesn't reach Cowork's own most distinctive new capability (mobile, work-while-
   offline, phone-based decisions).
2. **Remote-reachable scope**: expose the MCP server (e.g. via a tunnel or a real
   deployment) so Cowork's mobile/remote sessions can reach it too - unlocks the mobile
   decision-seeking use case, but is a real new network-exposure surface for a server
   that currently has none, and needs its own authentication story (today's server has
   none - it's never been reachable from outside this box).

**Recommendation:** build option 1 first (zero new risk, real value - Marc's own Cowork
desktop app gets to see and act on Jasper's state) and treat option 2 as a separate,
explicit decision for later, not bundled into the same build.

## What this does NOT propose

- Replacing any of Jasper's own workers (bridge/curator/relay/etc.) with Cowork - they
  do fundamentally different jobs (Jasper's workers are narrow, routine-driven, and
  operate directly on `workgraph.db`; Cowork is a personal general-purpose assistant).
- Using Cowork as the executor for task #50's contract-review SME panel members - that
  design's "pluggable executor" note was speculative, and Cowork's consumer/personal-
  automation positioning doesn't obviously fit a headless, programmatic, scoped-clause
  reasoning workload the way a worker's own Agent-tool sub-agent call already does.
  Nothing found in this research changes that earlier conclusion.

## Build order (once approved - not started)

1. `workgraph_mcp_server.py` - thin MCP adapter over existing functions, local-only
   (option 1 above). No new business logic, no new exposure.
2. Marc configures his own Cowork client to use it; verify the resources/tools round-trip
   with real data.
3. Revisit option 2 (remote reachability) as its own explicit decision, only if Marc
   wants the mobile use case specifically.

## Sources

- [Anthropic Launches Mobile Access for Claude Cowork - PYMNTS](https://www.pymnts.com/news/artificial-intelligence/2026/anthropic-launches-mobile-access-for-claude-cowork/)
- [Claude Cowork expands to mobile and web - TechCrunch](https://techcrunch.com/2026/07/07/the-coding-agent-wars-are-spilling-into-the-rest-of-the-office-claude-cowork/)
- [Anthropic brings Claude Cowork to mobile and web - VentureBeat](https://venturebeat.com/technology/anthropic-brings-claude-cowork-to-mobile-and-web-as-usage-data-shows-most-users-arent-coding)
- [Claude Cowork | Claude by Anthropic](https://claude.com/product/cowork)
- [Building agents that reach production systems with MCP - Claude by Anthropic](https://claude.com/blog/building-agents-that-reach-production-systems-with-mcp)
- [Build a Custom Connector for Claude Cowork with the MCP TypeScript SDK](https://rebeccamdeprey.com/blog/build-custom-connector-claude-cowork)
