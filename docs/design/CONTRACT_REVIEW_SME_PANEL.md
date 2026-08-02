# Design: AI-native SME panel + synthesis layer for contract_review

**Status:** design only (task #45). No live code changed. The build is
tracked separately as task #50, gated behind Marc reviewing this.

## A correction before the design: what actually exists today

Earlier framing of this idea assumed `contract_review` was a single flat
pass that could be "broken out into 15-20 different reviewers." Reading
the skill's own real reference material first (`references/pass-artifacts.md`,
`playbook.md`, `sme-matrix.md`, `risk-scoring.md`, and the rest of the
folder) shows the skill is already far more structured than that:

- It's a **mandatory 4-pass pipeline**, each pass gated on the previous
  one producing a named artifact (`PASS_1_STRUCTURE` → `PASS_2_COVERAGE`
  → `PASS_3_ANALYSIS` → `PASS_4_PREP`), with explicit "anti-collapse"
  signals for when a run skips a pass and produces shallow output.
- It already names **12 individual SMEs** (Tax, Insurance, Audit Rights,
  AI/Privacy, Adverse Events, Trade Sanctions, InfoSec, HSE, Payment
  Terms, Records Retention, Brand/Publicity, Anti-Bribery), each with
  real trigger keywords, scope, common issues, and turnaround time, plus
  a 13th generic escalation route ("Contract Request and Consultation
  Tool") for anything not covered by a named SME.
- It already has a **documented protocol for conflicting/parallel SME
  triggers** (sme-matrix.md's "Multiple SME Escalation Handling": create
  separate escalation comments, note that other SMEs are also reviewing,
  wait for all responses, escalate to the Consultation Tool if positions
  conflict).
- It already has a **locked, versioned output structure**
  (`dashboard-canonical.md` v3.2: 3 panels, fixed sub-tabs, fixed color
  tokens, position cards with 5 persona variants) that must render
  identically regardless of what analysis produced the content.
- It already has a **mechanical risk-scoring formula**
  (`risk-scoring.md`'s Protection Score) that cross-references every
  finding's protection category against `PASS_2_COVERAGE` before scoring
  it - explicitly designed to prevent one specific failure mode (treating
  a well-governed finding the same as an unprotected standalone one).
- There's a real gap worth flagging separately, unrelated to this design:
  `skills_registry.json`'s `skill_dir` for this skill points to
  `documents/reference/skills/lilly-contract-review-1c344a` under
  `TEAM_DATA_DIR`, but that directory is currently **empty** in the real
  app data - every reference file cited in this doc was only found in
  old session scratchpad copies, not in Jasper's own live skill storage.
  If those scratchpad copies are ever cleared, this skill's real grounding
  material goes with them. Worth a look independent of this design doc.

Given all that already exists, the right scope for "AI-native SME panel"
is narrow and additive: **parallelize the one pass where a single model
juggling 12+ SME domains' worth of trigger phrases and thresholds is
most likely to miss something a domain-specific pass wouldn't** - and
change nothing else. Not a new architecture; a refinement to one pass
inside the architecture that already exists.

## Where the panel actually fits: Pass 3 only

Pass 3 (`PASS_3_ANALYSIS`) is where findings get generated - commercial
analysis, vendor tactics scan, pharma requirements check, and (implicitly,
since the playbook's escalation column names an SME for nearly every
section) SME-relevant issue-spotting across 12+ domains at once. This is
the pass most exposed to a single generalist model's attention being
spread thin across domains it context-switches between per clause, and
it's the pass whose OUTPUT (`PASS_3_ANALYSIS`) already has a well-defined
shape Pass 4 consumes unchanged - so panelizing it doesn't require
touching Pass 1, Pass 2, Pass 4, the risk-scoring formula, or the locked
dashboard structure at all.

Pass 1 (structural scan) and Pass 2 (coverage/definition tracing) stay
single-pass, sequential, exactly as designed - they're read-and-classify
work with no natural per-SME decomposition (there's one document
structure, one coverage matrix, not twelve). Pass 4 (QA/negotiation prep)
stays single-pass too - it's explicitly a reconciliation step that needs
one consistent view across all of Pass 3's findings at once, not
something to further parallelize.

## Proposed design

**Panel members:** one sub-agent per SME row in `sme-matrix.md` (12
members) plus one generalist "everything else" member covering the
Contract Request and Consultation Tool's catch-all list (indemnification
structure, liability cap, choice of law, termination-for-convenience,
force majeure, IP ownership, and "any provision not covered above") -
13 members total, not an arbitrary 15-20. Each member's prompt is
grounded in exactly:

- Its own `sme-matrix.md` entry (triggers, scope, common issues,
  escalation threshold) - this is what makes it a specific lens rather
  than a generic "look for legal issues" pass.
- The specific `playbook.md` section(s) its domain owns (e.g., the Tax
  member gets §8 and HS-3; the AI/Privacy member gets §9-10, §19, and
  HS-5).
- The already-produced `PASS_1_STRUCTURE` and `PASS_2_COVERAGE`
  artifacts (read-only) - a panel member must never re-derive coverage
  status itself or flag something Pass 2 already resolved; it only adds
  NEW findings within its own domain.
- The document text itself (or the relevant excerpt - see "scoping the
  input" below).

Each member's job is narrow and mechanical: scan for its own trigger
keywords/clause types, and for each real hit, produce one finding in
`PASS_3_ANALYSIS`'s existing shape (severity tier, document reference,
playbook/regulatory citation, VERIFIED/ASSUMED flag, impact, recommended
action) plus one extra field this design adds: `owning_sme` (name/email,
straight from `sme-matrix.md`) and `escalation_threshold` (also straight
from `sme-matrix.md`) so the synthesis step and, later, the actual
escalation-comment generation, don't have to re-look this up.

A member that finds nothing in its domain returns an empty findings list
- never a fabricated "no issues found, all clear" narrative line. Silence
is the correct output when a document genuinely has no tax provisions,
no AI/ML involvement, etc.

**Scoping the input (real cost consideration):** running 13 sub-agents
against a full contract's text each is wasteful when most SMEs' trigger
keywords won't appear anywhere in a given document. Two-stage scoping:

1. A cheap, deterministic keyword pre-filter (regex over each SME's own
   `Triggers:` list from `sme-matrix.md` - already plain comma-separated
   keyword lists, no NLU needed) run once against the full document text.
2. Only SMEs whose trigger keywords actually appear get a real sub-agent
   call, with the input trimmed to the matching clause(s) plus
   surrounding context (a paragraph or two, not the whole document,
   unless the SME's scope is inherently document-wide like Trade
   Sanctions or Anti-Bribery).

This keeps the panel's cost proportional to how many domains a document
actually touches, not a flat 13x multiplier on every review, and it's a
real, mechanical filter (same "grounded, never guessed" discipline as
this session's own `workgraph_signals.py` keyword rules), not a judgment
call.

**Synthesis:** a single reconciliation step (not a 13-way vote) merges
all triggered members' findings into one `PASS_3_ANALYSIS` list:

- Straight concatenation for findings in different domains touching
  different clauses - no conflict, no reconciliation needed, this is the
  common case.
- When two members flag the **same clause** (sme-matrix.md's own worked
  example: an AI data-processing provision triggers both AI/Privacy and
  InfoSec), follow the matrix's own already-documented protocol exactly:
  keep both findings, but link them (each finding gets a `related_findings`
  reference to the other), and flag the pair for the **existing**
  "Multiple SME Escalation Handling" step rather than inventing a new
  arbitration mechanism - that protocol already says to escalate
  conflicting SME positions to the Contract Request and Consultation
  Tool, which is a human-facing routing decision, not something this
  design should try to resolve computationally.
- Severity ties or overlapping citations to the SAME Hard Stop are
  deduped (one HS-1 finding, not twelve copies of it from every member
  whose scan happened to touch §25) - keyed on the Hard Stop id
  (`HS-1`...`HS-6`) or the playbook section number, not on fuzzy text
  similarity.

The reconciled list is exactly what `PASS_3_ANALYSIS` already expects,
so Pass 4's gate checks, the risk-scoring formula, position-card
generation, and the locked dashboard structure need **zero changes**.

## What this does NOT change

- The 4-pass gate structure and artifact names.
- The Protection Score formula or its anti-drift calibration.
- The canonical dashboard structure, sub-tabs, color tokens, or persona
  variants.
- The actual SME escalation comment format or the "wait for all SMEs,
  escalate conflicts to the Consultation Tool" human workflow -
  panelizing Pass 3 makes the FINDING that something needs an SME's eyes
  more reliable; it does not change what happens once a human SME is
  actually looped in.
- Pass 1, Pass 2, and Pass 4 - single-pass, unchanged.

## Executor is pluggable, not fixed

Per the earlier conversation about Claude Cowork potentially becoming a
better executor for exactly this kind of "read a document, reason
carefully about one narrow domain" work: nothing about this design
requires the 13 panel members to be run by any specific agent runtime.
The interface each member needs is narrow (a domain brief in, a findings
list out, in `PASS_3_ANALYSIS`'s shape) - whatever runs Jasper's skills
today can run them; if a better-suited executor exists later, only the
"how each member gets invoked" plumbing changes, not this design's shape
or Pass 4's consumption of the result.

## Edge cases to design around when building this (task #50)

- **A document with almost no SME-relevant content** (e.g., a simple
  NDA touching none of the 12 named domains' triggers): the keyword
  pre-filter should trigger zero or one panel members (likely just the
  catch-all generalist), and the panel should cost about the same as
  today's single Pass 3 - not a fixed 13x overhead regardless of content.
- **A trigger keyword appearing in an unrelated context** (e.g., "audit"
  appearing in a definitions section, not an actual audit-rights clause):
  each member's prompt must include its `sme-matrix.md` "Scope" text, not
  just the trigger keyword, precisely so it can recognize a keyword hit
  that isn't actually its domain and correctly return no finding rather
  than force one.
- **Hard Stop double-counting**: since several Hard Stops are also named
  in a specific SME's trigger list (HS-1/Sanctions→Curti, HS-3/Tax→Shields,
  HS-4/AE→Chu, HS-5/AI→Legal AIPC), the dedup-by-Hard-Stop-id step above
  is load-bearing - without it, the Protection Score's "Hard Stops are
  never reduced, always -15" rule could get applied multiple times for
  the same real Hard Stop.
- **Cost/latency budget**: even with pre-filtering, a document that
  genuinely touches many domains (a real MSA renewal, say) could still
  trigger most of the 13 members - this should run as a `parallel()` fan-
  out (not sequential), since members are fully independent given their
  scoped input; wall-clock should track the slowest single member, not
  the sum.

## Test plan for the build (task #50)

- Keyword pre-filter: given a document's text, correctly identifies which
  SMEs' triggers appear (unit-testable against `sme-matrix.md`'s own
  trigger lists directly, no LLM call needed for this part).
- Each panel member, given a clause that matches its domain, produces a
  finding with the correct `owning_sme`/`escalation_threshold` carried
  through from `sme-matrix.md`.
- A member given a clause OUTSIDE its domain (keyword hit, wrong context)
  returns no finding.
- Synthesis: two members flagging the same clause produces two linked
  findings, not a silently dropped one.
- Synthesis: two members' findings both citing the same Hard Stop id
  dedupe to one Hard Stop finding in `PASS_3_ANALYSIS`.
- End-to-end: the reconciled `PASS_3_ANALYSIS` from a panelized run
  passes every existing Pass 4 gate check unmodified.
