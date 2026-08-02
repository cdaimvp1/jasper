# Design: leadership-facing usage/telemetry + time-saved page

**Status:** design only (task #48), per Marc's own explicit instruction
("do not build this now... queue this after the ui port is complete and
stable"). No live code changed. The build is tracked separately as task
#51, gated behind Marc reviewing this.

## The one finding that reshapes this whole design

Before designing anything, I queried every table this doc proposes to
read from, live, against the real production database
(`C:\Users\lane_marc@lilly.com\Symphony\body\data\workgraph.db`):

```
raw_items total: 0          issues total: 0          issues open: 0
pending_actions total: 0    nba_choice_log total: 0  issue_state_history total: 0
```

**Every relevant table is currently empty.** This installation has no
real ingested mail, no real issues, no real logged actions yet - which
means there is no real usage history to chart today, and any numbers a
mockup shows right now would be fabricated, not illustrative. This
doesn't block the design (the schema is real and sufficient - see
below), but it does mean:

- This page's actual value only materializes after real usage
  accumulates - consistent with the "stabilize via weeks of real use,
  then generalize" plan already discussed separately.
- The mockup accompanying this doc uses numbers explicitly labeled as
  hypothetical/illustrative, not a snapshot of Marc's real activity -
  same honesty rule this whole port has followed everywhere else (never
  show a number you'd have to fake).
- Nothing here should be read as "the data already proves this" - it's
  "here's what the data WILL prove once it exists."

## Data audit: what's real and already logged (no new instrumentation)

| Table | What it actually captures | Relevant to |
|---|---|---|
| `pending_actions` | Every skill/action Marc has triggered - `action_kind`, `worker`, `status` (requested→in_progress→done/failed), `requested_ts`, `updated_ts` | Workload volume, time-to-complete per action, success rate |
| `nba_choice_log` | Every ranked recommendation set Jasper ever offered on an issue, whether Marc chose one, ignored it, or it expired, which one, and when | Recommendation acceptance rate - "is the guidance actually useful" |
| `issue_state_history` | Every state transition per issue (from→to, timestamp) plus `actor` (who/what caused it - "marc" for a manual click vs. automated recompute) | Issue lifecycle timing, automation rate (how much state management happens without a manual click) |
| `issues` | Full issue rows - category, priority, value_found (via `workgraph_nba.value_amount_for_issue`), opened_at/updated_at | Volume by category, dollar value tracked, aging |
| `raw_items` | Every ingested mail/Teams/calendar item, classified or not, with `item_class` (NOISE/ACTIONABLE-ASK/WAITING-ON-OTHERS/FYI-EVIDENCE) | Raw ingestion volume - the "how much would you have had to triage by hand" number |
| `audit_log` | Field-level corrections (entity/field/old/new/reason) | Secondary: "Jasper gets corrected, and the correction sticks" - a trust signal, not a time-saved one |

`bus.db` (the cohort message bus - `id/ts/source/kind/actor/target/payload`)
was also named in the task brief, but on inspection it's the Symphony
cohort's general worker-coordination bus, not Jasper-domain usage data -
team-room posts and cross-worker messages, not "actions Marc took in the
cockpit." Not a source for this page; noted so a future session doesn't
re-investigate it for this purpose.

**Conclusion: no new instrumentation is needed for the metrics below.**
Every number this design proposes showing is a read (aggregate, filtered,
grouped) against tables that already exist and are already written to by
real code paths, not something requiring a new logging table.

## What's measurable now vs. what's inherently an assumption

This distinction has to stay visible on the page itself, not just in this
doc, or the page becomes exactly the kind of "trust me" claim Jasper's
own design philosophy elsewhere refuses to make.

**Directly observable (once data exists), no assumption required:**
- Count of raw_items ingested, by source and by item_class.
- Count of pending_actions completed, by action_kind, with real
  `updated_ts - requested_ts` duration per completed action.
- NBA acceptance rate: `chosen` / (`chosen` + `ignored` + `expired`) from
  `nba_choice_log`, overall and by `source_surface` (nba/evidence_row/
  synthesis) - this is a real quality signal on its own, independent of
  any time-saved conversion.
- Issue volume/category/value_found distribution, and what fraction of
  state transitions have `actor` = an automated value vs. a manual one.

**Requires a stated assumption (a per-unit time estimate), never
presented as observed:**
- "Time saved" for any of the above, because there is no logged baseline
  anywhere of "how long would this have taken Marc to do by hand" - that
  number does not exist in any system, real or hypothetical, and can't be
  derived from Jasper's own data. It has to be an assumption, applied to
  a real count, and both halves must be shown separately (assumption ×
  real count = estimate), never collapsed into a single unexplained
  figure.

## Time-saved methodology (conservative, itemized, assumption-labeled)

Same conservative framing already discussed: account for Marc's real
role (a procurement rep reviewing roughly 10+ contracts/week, per his own
estimate), the fact that most of Jasper's work runs in the background and
is delivered back to him rather than requiring active operation, and two
real caveats that must stay on the page, not buried in a footnote:

1. **Irreducible human review floor.** High-stakes items (anything past a
   Hard Stop, anything with real dollar value) still need Marc's own
   judgment - Jasper reduces the TIME TO a decision point, not the need
   for the decision itself. Time-saved estimates should be framed as
   "time to first-draft/first-read," not "time to close."
2. **Saved time may become more assigned volume, not free time.** If
   Jasper's real effect is that Marc gets to touch more items per week at
   the same quality bar, that's a genuine productivity story, but a
   different one than "this gives Marc N hours back" - the page should
   let both readings coexist rather than assert one.

Proposed itemized mechanism list, each with its own explicit multiplier
(illustrative starting values - Marc should adjust these, they are the
one input this design can't derive from data):

| Mechanism | Real count (from) | Assumed time per unit | Basis for the assumption |
|---|---|---|---|
| Triage/routing | raw_items ingested & classified | ~30-60 sec/item | Time to open, skim, and decide "does this need me" for an email that never required that because it was auto-classified |
| Drafting (reply/forward/compose) | pending_actions, action_kind in (draft_reply, draft_forward, compose_new) | ~3-5 min/action | Time to open a blank reply and write a first pass, vs. reviewing/editing a draft already addressed and quoted |
| Skill execution (contract_review, summarize, etc.) | pending_actions, other action_kinds, status='done' | per-skill, NOT one flat number - contract_review's own first-pass reading time is materially different from a thread summary | Needs a per-`action_kind` table, not a single constant - see open question below |
| Tracking/no dropped follow-up | issues with `has_unmet_prerequisite` transitions or repeat_signals present | ~2-3 min/instance | Time to notice a stalled thread needs a nudge, which Jasper already surfaced instead |

**Open question this design deliberately leaves for Marc, not guesses at:**
the "skill execution" row needs a per-skill time estimate (a `contract_review`
run's real prior manual-review time is a very different number from a
`summarize` run's), and Marc's own stated ~10 contracts/week volume is
the one hard, real anchor available - the design should let him plug in
his own honest number for "how long does a full manual contract read
actually take me" rather than this doc inventing one.

## Proposed page structure

Three sections, in order of how defensible the number is - most-observed
first, most-assumption-dependent last, so the reader sees the real data
before the extrapolation:

1. **Usage** (fully observed, zero assumptions): raw_items ingested over
   time, issues tracked, actions run by kind, NBA acceptance rate. This
   section alone already tells the "is this thing actually being used"
   story leadership needs, with no estimate required at all.
2. **Time saved** (observed counts × stated assumptions, both shown):
   the itemized mechanism table above, rendered as real-count ×
   assumption = estimate, with the assumption values visibly editable/
   labeled as inputs, not hidden constants. A running total, clearly
   subtitled "based on the assumptions above, adjustable."
3. **What this doesn't claim** (the caveats): the irreducible-review-
   floor and volume-vs-free-time points from above, stated plainly, not
   as a legal disclaimer buried at the bottom - this is what makes the
   first two sections credible rather than a sales pitch.

## Mockup

A static, non-wired mockup accompanies this doc (published separately,
same pattern as the Detail Panel mockups earlier this session) - it
illustrates the layout and the assumption/estimate distinction above
using clearly-labeled hypothetical numbers, not real data (there is none
yet, per the finding at the top of this doc). It is not built into
`cockpit.html` and does not query any real table - consistent with
Marc's explicit "do not build this now."

## What this design does NOT do

- Add any new database table or logging call. Every metric reads
  existing, already-written tables.
- Present any number as real that isn't backed by an actual row count
  against the live database at render time.
- Collapse an assumption and a real count into one unlabeled figure.
- Decide the per-unit time assumptions itself - those are flagged as
  Marc's own inputs to set (or defaults he can override), not this
  design's guess presented as fact.

## Test plan for the build (task #51)

- Each "Usage" metric: a query test against a seeded `ws_db` fixture
  with known rows, asserting the exact count/aggregation returned.
- NBA acceptance rate: seeded `nba_choice_log` rows in each status,
  assert the rate calculation matches by hand.
- Time-saved: given a real count and a given assumption value, the
  estimate is `count * assumption` exactly - no hidden rounding/fudging
  - and the page must render both the count and the assumption next to
    the estimate, not just the product.
- Empty-state: with zero rows in every source table (today's actual
  state), the page must render an honest "not enough usage yet" state,
  never a zero dressed up as a real result or a silently blank section.
