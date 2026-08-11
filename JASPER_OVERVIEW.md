# What Jasper Is

## The core idea

Jasper is not a chatbot with a good memory prompt. It's a **workgraph** — a
persistent, structured record of an employee's business relationships and
communications, built and kept current automatically, that any AI (or
person) can query, act through, or extend.

The problem it solves isn't specific to procurement, or to any one role.
Almost anyone doing real work at a company like Lilly is juggling a high
volume of requests, conversations, and open items scattered across email,
Teams, meetings, and whatever line-of-business systems their role touches.
The actual work is rarely reading any single message — it's remembering
everything relevant *around* it: who already asked for what, what's already
been promised, what's still open, what happened on this relationship or
project months or years ago. Reconstructing that by hand, or re-reading a
thread every time, doesn't scale to any busy role, and none of it survives
someone leaving or handing work off.

Jasper's answer is to do that assembly work continuously, once, in the
background, and store the result as a real, durable, structured object: not
a cache of messages, but a graph of **Projects → Issues → Claims**, where
every claim (an ask, a decision, a commitment, a date) is tied back to the
exact conversation it came from.

## Why conversational context is the piece other systems can't give you

Most line-of-business systems (an ERP, a contracting platform, a ticketing
tool) know the *state* of something — a PO is approved, a contract is
signed — but they don't know the *texture* of how it got there: the back-
and-forth, the informal commitment made on a call, the caveat someone
mentioned in passing in Teams. That texture lives almost entirely in email,
Teams messages, and meetings — increasingly including what Copilot's own
meeting notes capture. It's real signal, and it's the signal a system-of-
record alone never has.

That's the case for Jasper being more than one more integration: it can be
the layer that sits underneath and connects the systems-of-record to the
actual conversations that explain them — the backbone that makes both kinds
of signal usable together, not two separate places someone has to check.

## How this is different from just asking an AI to look things up live

An AI assistant can already search a mailbox or Teams history on the fly
when asked, and stitch together an answer in the moment. That's genuinely
useful, but it's re-done from scratch every time, at whatever quality that
particular pass through the data happens to produce, and it never persists
anywhere for the next question, the next person, or a skill to build on.

Jasper does the equivalent work differently, and in advance:

1. **Deterministic ingestion and extraction** — every new email, chat, or
   meeting note is pulled in and the real content extracted (asks,
   decisions, dates, commitments) by rule, not by chance. It's designed so
   nothing silently falls through unnoticed the way a message can get lost
   in a long thread or a full inbox.
2. **LLM-judged grouping and validation** — the system uses a model to
   decide which conversations belong to the same real project or
   relationship, and to fill in details the deterministic pass can't infer
   on its own — but always checked against what's actually confirmed, not
   just guessed.
3. **Synthesis into one current picture** — the result is a standing,
   continuously updated summary of each relationship or project, so
   "what's the state of this" is a lookup, not a research task.

Because the system already knows the context, the history, and what's being
asked, it's also positioned to reason about the *right response* — including
recognizing when a registered skill applies (review a contract, build a
pro-forma or cash flow, draft an executive summary, put together a deck) and
running it, with the output waiting for the user to review rather than
requiring them to notice the need, remember the tool exists, and go run it
themselves. That's the shift from an assistant you have to operate to a
system that does real administrative work in the background — while keeping
a human in the loop exactly where that matters (nothing gets sent, filed, or
finalized without review).

## What it can actually do today

This is grounded in what's built and verified, not aspirational:

**Ingestion (automated, ~5x/day on a scheduled cycle)**
Pulls inbound mail, Teams chats, Calendar, and SharePoint content
automatically. Sent mail is ingested through the same path (currently gated
behind a config toggle pending validation), so a user's own replies count as
real signal too, not just what arrives.

**Grouping**
Every new communication is automatically matched to an existing project or
used to start a new one — grouped by real shared identity (participants,
reference numbers, thread history), judged by an LLM read of both sides when
the automatic signal isn't clear-cut.

**Extraction**
Each communication is read for the real content buried in it — asks,
decisions, commitments, dates — stored as individual, evidence-linked
claims, not just a subject line. Every claim traces back to the exact
message it came from.

**Synthesis**
Each project/issue carries a running, continuously updated summary — the
state of the relationship in plain language, not something re-derived by
hand every time someone wants to know where things stand.

**Prioritization**
A scoring pass ranks what actually needs attention — whose move it is,
what's gone stale, what has real value attached, what's overdue — so the
next thing to act on is a ranked answer, not a manual triage of everything
open.

**Real actions**
On request, Jasper can open an email, draft a reply or forward (addressed
correctly, with real drafted content, in the user's actual mailbox —
reviewed before sending, never auto-sent), request a specialist review from
a worker, or compose a new message to multiple stakeholders at once.

**Proactive background prep**
For recognized patterns (a status-update request, a signature request),
Jasper can prepare the response ahead of time — queued for review, not
dispatched automatically. This runs independent of whether any UI is even
open.

**Workers**
A small cohort of named workers (e.g. a contract-review specialist) can be
messaged directly or dispatched to a task, and report their own status back.

## How skills plug in

A "skill" (contract review, a pro-forma build, an executive summary, a deck)
is registered once — what it does, and what kind of request or deliverable
it applies to. After that, Jasper doesn't need to be told to use it: when a
new issue or request matches the pattern, the same next-best-action ranking
that already surfaces "draft a reply" or "this is overdue" can surface "run
this skill" as the recommended action instead. With pre-emptive execution
turned on, it can go a step further and actually run the skill in the
background the moment the pattern is recognized, so the output is simply
sitting there, ready for review, by the time the user gets to it — rather
than the user having to notice the need, remember the right tool, invoke it,
and wait.

That plug-in model is what makes Jasper extensible without a redesign: new
skills, new deliverable types, and new registered tools all hook into the
same recognize → recommend → (optionally) run → hold-for-review pipeline
that already exists.

## Two different kinds of "autonomous," and why they shouldn't be treated the same

Not all automation carries the same risk, and the design should keep that
distinction explicit rather than let it blur as capability grows. One
important scoping choice up front: Jasper's own conversational surface is
**entirely internal** — it never talks directly to a supplier, customer, or
anyone outside the company. That removes the highest-stakes failure mode
(something wrong reaching an outside party) from the picture entirely. What
remains is a smaller, internal-hygiene question: *which* colleague is
asking, and what are they actually authorized to trigger or see.

- **Triggering the work vs. disclosing the result.** These are two separate
  moments, and only one of them needs a human gate. A colleague asking
  Jasper to kick off a contract review while you're in a meeting has
  essentially no downside in the triggering itself — nothing is committed,
  nothing leaves the company, nothing represents a position to anyone.
  Handing over the *result* — the reviewed document, a confirmed answer —
  is the moment that actually matters, so that's where the gate belongs:
  hold the output for your review before anyone (even an internal
  colleague) is told the outcome or handed the document. This maps
  directly onto a mechanism that already exists — completed skill/worker
  output already lands in a review queue before anything happens with it;
  extending that to "trigger immediately, but the answer waits for you" is
  a natural fit, not a new concept.
- **Knowing who's actually asking.** Jasper's data already distinguishes
  internal colleagues from external parties (it's what drives some
  existing stakeholder-view behavior today), which is a real foundation —
  but that's domain-based ("this address is @lilly.com"), not identity- or
  role-verified. Getting to genuine access control — can *this specific*
  person request a review, should they be allowed to see *this*
  document — means integrating with Lilly's real identity and permission
  systems (Entra ID, RBAC), not something the graph does on its own today.

Because the whole exchange stays internal, the practical design principle
is simpler than an external-facing system would need: let the trigger run
freely once the requester is a recognized, authorized colleague, and keep
the human gate on the moment a result would actually reach someone —
internal or not.

## Roughly how much time this could save someone

This splits into two categories with very different ceilings:

**Doing the same work faster.** Skipping a from-scratch reread of a thread
to get status, skipping manual compilation of a "here's where things stand"
update, starting a reply from a real prepared draft instead of a blank
page. This is a straightforward, per-task compression, bounded by how much
routine work a person does themselves — a reasonable estimate here is
**3–8 hours saved per person per week**.

**Work that stops requiring the person at all.** Running the contract
review the moment an urgent request lands instead of whenever it's next
checked, rather than making the requester wait until you're free. Answering
a colleague's status question directly, in context, without them waiting on
your calendar. A document arriving and routing itself into the right system
with nobody touching it. This isn't compression — it's parallelization, and
its ceiling isn't minutes-per-task, it's how much of a person's inbound
interruption load never needs them personally. For a role fielding a high
volume of requests, this category plausibly exceeds the first one — but it
also depends on capability that's mostly not built yet (see above), so it's
a real, larger opportunity to validate as that capability matures, not a
number to bank on today.

## How Jasper is designed to keep learning

A system like this is only as good as the judgment behind its grouping and
its recommendations, and that judgment isn't meant to be static:

- **Discovering what matters.** Rather than starting from a fixed,
  hardcoded schema, Jasper periodically scans the real ingested corpus for
  recurring structured signals — a field that shows up repeatedly with the
  same shape (a reference number, a contract value, a renewal date) — and
  proposes them as candidate data points. A human confirms which ones
  actually matter before they become part of the working vocabulary, so the
  vocabulary is discovered from real usage, not guessed in advance, and a
  different role naturally ends up tracking different things.
- **Validating what it's learned.** Every grouping or matching judgment the
  system makes is either confirmed or corrected, and that outcome is stored
  as a lesson that biases future judgment calls toward patterns that have
  actually proven right, and away from ones that were wrong — so the same
  mistake shouldn't keep recurring, and accuracy is expected to improve with
  real use rather than staying fixed at launch quality.
- **Being taught directly.** A user can state a rule in an ordinary
  sentence during a normal conversation — "X can't happen until Y is
  signed" — and have it become a real, applied constraint from then on,
  without a formal admin screen or a developer involved.
- **Personalizing over time.** The same backbone can learn a specific
  user's own patterns — the tone and phrasing they'd actually use in a
  reply, the kinds of things they tend to prioritize — so what it prepares
  looks like something that user would have written, not a generic
  default. And because the data-point vocabulary is discovered per corpus
  rather than fixed, a sourcing role naturally ends up tracking PRs and
  contract values while a sales role ends up tracking deal stages and close
  dates, on the exact same underlying system.

## Why each person's graph should stay their own

An important design question this raises: should work graphs be shared —
across a team, or across the company — or should each person's stay
personal? The answer here is deliberately personal by default, and not
just as a simplification:

- **A work inbox is never purely work.** Personal errands, HR matters,
  health or family mentions, job-search activity — all of it routinely
  passes through a work account. A shared, company-visible graph built
  from that turns incidental personal content into a queryable record,
  which is a real privacy problem on its own, and likely a bigger barrier
  to people actually trusting and using this than any technical concern.
  A personal graph — today, literally one local database per install —
  sidesteps that entirely: there's no cross-person content to filter in
  the first place.
- **It also avoids a much harder version of the grouping problem.** Within
  one person's mailbox, grouping has a strong natural anchor — the same
  person is a party to every thread, so shared participants and continuity
  do most of the work. Across a whole company, that anchor disappears:
  matching two separate threads, sent to two different people, with no
  shared thread and often different internal shorthand for the same
  underlying negotiation, is a far fuzzier inference — and getting it
  wrong is worse in both directions, from missing a real connection to
  wrongly blending two unrelated conversations or exposing one person's
  communications to another. Personal scope keeps the matching problem
  bounded to something tractable.
- **It makes personalization better, not just safer.** A graph trying to
  stay useful across many people has to average across everyone's habits
  and vocabulary to stay generally applicable — personalization only gets
  sharp when it's learning one person's own patterns with no cross-user
  noise diluting it. The generic part (a role's base vocabulary — what a
  sourcing job tracks vs. a sales job) is the only piece that could ever
  usefully generalize; everything that makes Jasper feel tailored to a
  specific person depends on staying scoped to that person.
- **Role transitions don't require shared or continuous access to solve.**
  The claims that make up a project — asks, decisions, commitments,
  evidence — are already a structurally separate layer from personal
  response-pattern learning (how a specific person tends to phrase
  things). That separation is what makes a deliberate, one-time
  **extraction** possible: hand a successor the real business state of
  every project they're inheriting, without carrying over anything
  personal or behavioral, the same way a mailbox gets handed off during
  an offboarding process today — a triggered, auditable event, not
  standing access to someone else's graph.
- **Where cross-person value is real, it doesn't require merging graphs.**
  Two people dealing with the same supplier and stepping on each other is
  a genuine coordination cost. Solving it only needs a thin, metadata-only
  layer — a directory of which named relationship is tracked by whom —
  not shared access to the underlying claims or conversations themselves.
- **Storage and access can ride on infrastructure that already exists.**
  A personal database, kept on that person's own company storage (their
  own OneDrive, say) and governed the same way that storage already is,
  isn't a new access-control system to design and get approved — it's the
  same per-user permission boundary Lilly's tenant already enforces. And
  "not freely accessible by anyone else" should mean the same boundary a
  mailbox already has today — not casually browsable, but still reachable
  by IT or legal under a real process (a legal hold, an investigation) —
  rather than an ungoverned blind spot.

## Why it's worth continuing to build

Beyond the day-to-day time savings, a few reasons this is worth investing
in as real infrastructure rather than a one-off tool:

- **One consistent answer, not a new reconstruction every time.** Anyone
  (or any AI surface) asking about the same relationship gets the same
  current state, instead of each person or each tool re-deriving its own
  slightly different read of an ambiguous thread.
- **An auditable trail.** Every claim traces back to a real source message
  — a meaningfully more defensible record than an AI's live, unlogged
  summary, which matters in a regulated environment.
- **Compounding value.** The graph gets more useful the longer it runs —
  more history, more confirmed lessons, more personalization — unlike a
  tool whose value is flat every time it's used.
- **Shared infrastructure, not a personal hack.** If this proves out for
  one role, the same backbone — with each role's own discovered vocabulary
  — could become common infrastructure across many teams, rather than
  everyone independently developing their own ad hoc AI habits.
- **Lower cognitive tax.** Keeping every open thread straight in your own
  head is a real, quiet source of dropped follow-ups and stress; offloading
  that tracking is a real form of relief, not just a convenience.

## Growing it by connecting more systems

Today Jasper draws from mail, Teams, Calendar, and SharePoint, and has
started building real understanding of specific tools like Ariba and
ContractPodAI. Each additional connected system — an ERP, a signature
platform, a CRM, a ticketing tool, whatever a given role actually runs
through — adds more structured, ground-truth signal that strengthens
grouping accuracy and gives the synthesized picture more to work with. This
is designed to scale horizontally: adding a new system extends what Jasper
knows without requiring a redesign of the core.

---

## The interfaces (not the core of what Jasper is)

**Cockpit (web app):** the full-featured internal dashboard — project
lists, detail panels, settings, worker status, telemetry.

**Outlook add-in:** a lightweight taskpane surfaced directly inside Outlook
— a "project drawer" view of whatever email or supplier is being looked at,
a chat interface, and one-click real actions, without leaving the mail
client. Currently under active iteration; an open question has been whether
this custom UI is worth maintaining versus exposing the same capabilities
as tools any Claude client (Desktop, Code, potentially others) can call
directly, trading a custom interface for Claude's own native chat and
rendering.

Both are presentation layers over the same graph — neither one *is* Jasper.
The graph, and the pipeline that keeps it correct and current, is.
