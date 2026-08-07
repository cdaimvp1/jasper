# Design: Personalized data-point discovery (setup-time vocabulary, not a hardcoded schema)

**Status:** built 2026-08-06 (workgraph_discovery.py + retrofit into
workgraph_projects._matched_data_points/workgraph_pipeline2.process_new_item,
tasks #213-217, all tests green). Written after a real, live investigation of a
grouping bug (Kinaxis Maestro contract fragmented across 12+ projects, `docs/design/`
sibling docs and this session's own transcript have the full forensics) surfaced a
deeper architectural problem than the bug itself: Jasper's extraction logic
(`workgraph_signals.py`, `workgraph_projects.py`) hardcodes procurement-specific
fields (Ariba requester/descriptor, PR/PO reference format, DocuSign/AdobeSign
sender domains) directly in Python. That's fine for one procurement user. It
actively breaks for anyone else, because the *categories themselves* - not just how
they show up in mail - are procurement's, not universal. This document designs the
fix at the root, not another hardcoded field.

Directly extends the generalization gap already identified in this project's
standing notes (gap #1: "Domain logic hardcodes procurement-specific signal types
... rather than as swappable config/data" / "an automated discovery pass... producing
a per-installation config, with a human-review step before it goes live"). This
document is that gap's real, worked-out design.

## 1. The mistake this corrects (stated plainly, because it happened live)

Mid-design, the first draft of this document proposed a "stable, canonical
data-point schema" - supplier, stakeholder, product/service, amount, reference,
document identity, subject/topic - with only the *extraction mechanics* varying per
installation. **That is wrong, and Marc corrected it directly.** Those seven
categories are themselves artifacts of procurement work. A marketing/sales user's
real, meaningful data points might be campaign name, customer segment, deal stage.
Manufacturing: batch/lot number, equipment ID, shift. Research: protocol ID, study
ID, compound name. Leadership: initiative name, budget line. None of that is
knowable in advance, and choosing any fixed list - even one that looks
"comprehensive" from inside procurement - makes Jasper work for procurement and
nobody else.

**The corrected principle:** there is no universal category list. Setup discovers
*both* what the categories are *and* how to extract each one, from that specific
person's own real mail. The only universal thing is the *mechanism* of discovery,
never its output.

## 2. What setup actually does

1. **Mass ingestion + pattern analysis over the new installation's own real mail**
   (a real window - locked in 2026-08-06: **90 days**, confirming the generalization
   notes' original figure. An earlier draft of this doc mistakenly recorded 30 days
   as a revision; that was wrong, corrected here. The window is a parameter, not
   load-bearing to the design, but 90 days is the real default). No categories
   assumed going in.
2. **Optional role/org self-report as a soft prior, never a hard commitment.**
   **Locked in (2026-08-06): freeform text, not a rigid picklist** - with a set of
   clickable suggestions shown alongside the input (Procurement, Marketing & Sales,
   Manufacturing, Research, Leadership, Finance, IT, Research and Development, Human
   Resources, Learning and Development, ...) so a common case is one click, but
   nothing forces a real answer like "supply chain analyst embedded with
   manufacturing" into the nearest bucket. Whatever the user types or picks seeds
   the discovery pass with a starting set of *hypotheses* to specifically test
   against this person's real mail - a suggestion has a pre-built hypothesis set
   ready to go (e.g. "Procurement" -> check for Ariba-style approval notifications,
   DocuSign/AdobeSign requests, common ERP vendor domains; "Marketing" -> check for
   campaign-platform notifications, agency names, creative-review threads); genuine
   freeform text asks the discovery LLM to derive analogous hypotheses from the text
   itself. **Every hypothesis is tested against real, recurring evidence in this
   person's own mail before it becomes a candidate** - the role never forces a
   pattern into existence that isn't actually there, and a Procurement user with no
   DocuSign mail simply gets no DocuSign candidate. The role hint's only job is to
   make discovery faster and more sample-efficient (useful especially with a short
   window and little history yet), not to pre-determine the answer.
3. **LLM-proposed candidate data points, each grounded in real recurrence.** For
   each candidate: a name, a description, the real evidence that surfaced it
   (example senders/subjects/labeled-field text actually seen), and a proposed
   extraction rule (deterministic pattern where one exists - a regex, a labeled-
   field name; otherwise "no reliable deterministic rule found - Haiku-only field").
4. **Human review before anything is trusted.** Same discipline already
   established elsewhere in this codebase (Aristotle's `detect_candidate_rules()` -
   propose, never auto-activate; `workgraph_lessons`' trust-score-needs-repeated-
   confirmation) - abstaining/proposing is the safe default, never silently guessing.
   The user picks which candidates matter, can rename/merge/discard any, and that
   confirmed set becomes this installation's real, active vocabulary.

**Output of setup:** a per-installation vocabulary - not code, not a schema
migration, a real data table (see §4) - of confirmed data-point definitions this
one person actually cares about, each with whatever extraction rule was found for
it (deterministic pattern, Haiku-only, or both).

## 3. Ongoing refinement, not a one-shot scan

The vocabulary isn't frozen after setup, and this has to handle more than slow
drift - Marc's own explicit reason for insisting on this: **people change jobs.**
A vocabulary discovered from someone's mail in one role can go genuinely, mostly
stale within months, not just gradually decay. Three distinct mechanisms, covering
gradual drift, active staleness, and real discontinuity respectively:

- **Confidence grows/shrinks with repeated confirmation/reversal** - the exact
  trust-score arithmetic `workgraph_lessons.py` already uses (bump on repeat-
  confirm, penalty on reversal, never a hard cliff). A data point that keeps
  proving useful for real grouping/matching gets more trusted over time (may
  graduate from "Haiku-only" to "deterministic," if a reliable pattern emerges from
  enough real examples). This handles gradual drift within a stable role.
- **Staleness needs its own explicit signal, not just silence.** A confirmed data
  point that hasn't actually matched anything real in a long stretch (a real,
  measurable "last matched" timestamp per `data_point_definitions` row) shouldn't
  just quietly keep existing at whatever trust score it last had - it should
  eventually surface as "this hasn't come up in N months, still relevant?" A person
  who changes roles doesn't need to remember to tell Jasper; the absence of the old
  patterns in their new mail is itself the signal.
- **New candidates surface continuously, not just at setup.** The same discovery
  pass that ran once over the setup window can run periodically (or be triggered by
  drift - e.g. a new automated sender domain appearing repeatedly with no matching
  known data point) over new mail, proposing additions the same way - never
  auto-activated, always a suggestion the user confirms. This is what actually
  catches a job change in progress: new, unfamiliar recurring patterns showing up
  is exactly what a fresh discovery pass is built to notice.

**Locked in (2026-08-06), a real two-tier mechanism, not a single trigger:**

1. **Continuous, cheap, deterministic tracking - no LLM cost.** Every new item
   gets checked against a `candidate_pattern_observations` table (pattern
   signature - sender domain / labeled-field name / structural signature -
   count, distinct-thread count, first_seen_ts, last_seen_ts). Pure counting,
   always on. When a pattern crosses the real significance bar - **5
   occurrences, across at least 2 genuinely distinct threads/senders (not 5
   copies of the same forwarded email), within a rolling 60-day window** (Marc's
   own number, more conservative than this document's first draft of 3 -
   fewer false-positive proposals cluttering review, at the accepted cost of
   being slower to catch a real but lower-frequency pattern) - THAT crossing is
   what triggers a real LLM call: characterize the pattern from the accumulated
   real examples, draft a proposal, present for confirmation. Never auto-added.
2. **A monthly full sweep, not instead of (1) - a genuine complement.** (1)
   catches clear-cut new recurring patterns cheaply; a periodic full
   LLM-driven re-analysis catches subtler things pure frequency-counting
   would miss (patterns that look different on the surface but are actually
   related), and is the natural place to handle REMOVAL/staleness too -
   checking the WHOLE existing confirmed vocabulary at once for "hasn't
   matched anything real in months, still relevant?" (§3's job-change case) in
   a way (1) isn't built to do at all. Same rule as everywhere else in this
   design: proposes, never auto-commits - additions and removals alike.

Same real, practical trigger for the monthly sweep either way: run it on a
fixed cadence (monthly), not only reactively when something visibly breaks.

**Vocabulary size cap - locked in 2026-08-06: 20 confirmed data points max per
installation.** Marc's own number, sweet spot "somewhere between 10 and 20."
Applies only to the discovered/human-confirmed vocabulary this system produces
(the "Supplier name" / "Campaign ID" / "Batch number"-style fields) - NOT to
sender identity/affiliation, attachments, or subject, which already exist as
separate, always-on infrastructure (`parties` + `workgraph_parties.
classify_affiliation()`, the `attachments` table, `raw_items.subject`) and were
never proposed as `data_point_definitions` rows in the first place. When a new
candidate would push a confirmed count past 20, the human-review surface (§214)
must force a choice (retire/merge an existing one, or decline the new one) rather
than silently growing past the cap - keeps the review UI and the matching
signal set from drowning in low-value fields, the same failure mode a totally
unbounded schema would eventually hit anyway.

## 4. Data model (sketch, not final)

```
data_point_definitions
  id                  TEXT PK
  name                TEXT            -- user-facing, e.g. "Supplier name", "Campaign ID"
  description         TEXT            -- what this represents, for the review UI
  point_type          TEXT            -- structural role: 'entity' | 'reference' | 'amount' | 'person'
                                       -- | 'date' | 'freetext' (6 types, locked in 2026-08-06 after
                                       -- checking the real live corpus - see §7.4). A small, genuinely
                                       -- universal STRUCTURAL taxonomy - not a content taxonomy; used
                                       -- only to decide how a value participates in matching/scoring:
                                       -- "reference"-typed values can auto-merge alone, "entity"/"person"-
                                       -- typed contribute to the 2+-point candidate gate, "date"-typed
                                       -- get real temporal comparison/staleness handling (workgraph_
                                       -- deadlines.py's existing hard/soft real-timestamp treatment,
                                       -- generalized instead of hardcoded to issues.due/dates_mentioned),
                                       -- "freetext" never auto-merges (product/service names, subject-
                                       -- derived topics - real content, but not identity-backed the way
                                       -- a tracked party or a unique reference number is)
  deterministic_rule  TEXT NULL       -- regex / labeled-field name, when one was found
  status              TEXT            -- 'proposed' | 'confirmed' | 'rejected'
  trust_score         REAL            -- same bump/penalty arithmetic as workgraph_lessons
  discovered_from      TEXT           -- audit: which real raw_items/examples surfaced this
  created_ts, confirmed_ts, confirmed_by

data_point_values
  id                  INTEGER PK
  definition_id       TEXT FK -> data_point_definitions
  work_object_id      TEXT FK -> work_objects
  value               TEXT
  extraction_source    TEXT           -- 'deterministic' | 'llm_backfill' | 'llm_judgment'
  extracted_ts         REAL

candidate_pattern_observations       -- §3's continuous cheap tracker, pre-proposal
  id                  INTEGER PK
  pattern_signature   TEXT            -- normalized sender domain / labeled-field name / structural key
  occurrence_count    INTEGER
  distinct_thread_count INTEGER
  first_seen_ts       REAL
  last_seen_ts        REAL
  promoted_to_definition_id TEXT NULL -- set once this crosses the bar and a real proposal is drafted
```

**Two different refinement speeds, not one (Marc, 2026-08-06):** the discovered
data points themselves (rows in `data_point_definitions`) are meant to keep
changing continuously - trust-score drift, staleness, the monthly sweep. The
6-type `point_type` taxonomy above is a slower, deliberate layer instead - a real
schema decision (a migration, like the one `date` just required), made by a human
looking at real evidence, not something inferred/auto-added by the discovery LLM.
Unlimited auto-growth at the type level would recreate the same sprawl problem
the 20-item vocabulary cap (§3) exists to prevent, one layer up.

This replaces today's hardcoded shape (`compute_work_object_signature`'s
`positive_vocabulary` dict with named fields `ariba_requester`/`ariba_descriptor`/
`value_amount`/`system_party`, `workgraph_projects._matched_data_points`'s fixed
point-type list `reference/supplier/stakeholder/product_service/amount/document/
subject_entity`) with a **generic, per-installation-defined** set of the same
shape. `_matched_data_points`'s actual point-COUNTING logic (2+ real points from
DIFFERENT definitions = candidate; a `point_type='reference'` match alone can
auto-merge) is structurally sound and carries over unchanged - only the fixed list
of WHAT the points can be becomes data instead of code.

## 5. The tiered extraction pipeline, reading from the discovered vocabulary

Same three tiers already partially built this session, now schema-driven instead
of hand-coded per field:

1. **Deterministic (free).** For every `data_point_definitions` row with a
   `deterministic_rule`, apply it directly to raw_item text + attachment
   `extracted_text` (the attachment-scanning capability already built tonight,
   `workgraph_projects.reference_base_ids_for_issue`, generalizes to this - it
   becomes "run every confirmed deterministic rule over every text source," not a
   PR/PO-specific function).
2. **Haiku (cheap).** For whichever of THIS installation's confirmed data points
   came up empty on a given item, one cheap-model pass reads the same text once and
   tries to fill in whichever are missing - genuinely plural, not the single-field
   version built (and reverted from) earlier tonight. Given the real participant
   emails/known structured fields as INPUT (not asked to invent them from prose -
   the concrete bug found tonight: Haiku correctly said "none" when asked for an
   email address that was never in the text at all, because the real sender address
   isn't part of `resolve_item_text`'s output). Writes results tagged
   `extraction_source='llm_backfill'`, auditable/reversible, never indistinguishable
   from a confirmed deterministic hit.
3. **Heavier-model judgment (already built, unchanged).** `workgraph_pipeline2.
   judge_candidate` - the real full-text read for whether two 2+-point candidates
   are actually the same deal. Unaffected by this design; it already treats
   "matched signals" as an opaque list of names, so it doesn't care whether that
   list came from hardcoded functions or the discovered vocabulary.

## 6. Downstream impact - this reshapes more than grouping (Marc's own catch)

The discovered vocabulary isn't only a grouping signal. Two other already-planned
pieces were scoped with the same procurement-hardcoding mistake and need the same
fix:

- **Type-aware NBA / suggested actions** (roadmap item #2): "Ariba-approval-needed
  → show a deep link; DocuSign-signature-needed → offer contract_review" is itself
  a signal-type → action mapping that's procurement-specific. Important correction
  here too (Marc's own): this is NOT "a manufacturing person has no Ariba signals" -
  that's just as wrong as assuming a universal schema, the exact mistake this whole
  document exists to avoid. Whether Ariba/DocuSign signals exist for a given person
  depends entirely on what THAT PERSON'S actual job touches, not their department
  label - a manufacturing employee who also manages suppliers may have real,
  frequent Ariba mail. A role/org hint (§2.2) may add "check for Ariba" to the
  search hypotheses; it must never be used to ASSUME OR RULE OUT a signal type
  independent of what's actually, genuinely in that person's own mail. The
  signal-type → suggested-action mapping needs to be part of the same discovered,
  per-installation, per-PERSON, human-confirmed configuration - never hardcoded,
  never inferred from a job title alone.
- **Background-task triggers / skills** (roadmap item #3): which skills are even
  offered ("run contract_review") is downstream of the same installation-specific
  reality.
- **Multi-host card presentation** (roadmap item #4): less directly affected, but
  renders whatever the above two produce, so it inherits the fix for free once
  they're right.

## 7. Open questions for Marc

All four resolved (2026-08-06):

1. ~~Setup window length~~ - **90 days.** (An earlier draft of this doc had
   mistakenly flipped this to 30; corrected in §2 above.)
2. ~~Role/org self-report~~ - **freeform text with clickable suggestions**
   (Procurement, Marketing & Sales, Manufacturing, Research, Leadership, Finance,
   IT, Research and Development, Human Resources, Learning and Development, ...),
   not a rigid picklist. See §2.2.
3. ~~Migration for the existing live corpus~~ - **fast-track**: Marc's own
   already-proven procurement fields become this installation's initial confirmed
   vocabulary directly (task #217), not a re-discovery pass. Re-discovering what's
   already known and working would be pure cost with no signal.
4. ~~point_type taxonomy granularity~~ - **6 types: entity, reference, amount,
   person, date, freetext.** Checked against this installation's real, live data
   before finalizing (not just designed in the abstract, per Marc's own request):
   the existing hardcoded content categories (`reference`/`supplier`/`stakeholder`/
   `product_service`/`amount`/`document`/`subject_entity`) map cleanly onto 5 of the
   6 - supplier->entity, stakeholder->person, product_service/subject_entity->
   freetext (neither is identity-backed the way a tracked party or a reference
   number is, so both should behave as soft/non-auto-merging), amount->amount,
   reference->reference - except `dates_mentioned` (a field EVERY real extraction
   row already carries, already given real hard/soft temporal classification and
   comparison by `workgraph_deadlines.py`), which had no home in the original
   5-type sketch and would have silently lost that real, already-built comparison/
   staleness behavior if forced into `freetext`. Added `date` as the 6th type for
   exactly that reason. `document` (shared attachment-hash lineage) deliberately
   does NOT get a 7th type - it isn't a discovered "value" at all, it's a
   structural fact already tracked separately via `artifact_lineages`, and forcing
   it into this taxonomy would recreate the original hardcoding mistake in a new
   place. Vocabulary SIZE (as opposed to type granularity) is capped separately
   at 20 confirmed data points per installation - see §3.

## 8. Explicitly not designed here

- The actual setup UI/wizard screens.
- Migration tooling for existing installations beyond the one open question above.
- Any change to `judge_candidate`'s own judgment logic (§5, tier 3) - it already
  generalizes without modification.
- The actual build step for a confirmed system-table proposal (§9) - by design, that
  stays a human/dev pass, not an automated one.

## 9. Generalizing to whole systems, not just fields (task #266, 2026-08-07)

contractpodai_requests/ariba_requisitions (built by hand, tasks #265/#267) exist because
Marc's own correction was that a field like "Request ID" means something different
depending on which SYSTEM produced it - a real, typed, per-system table beats folding it
into the generic `data_point_definitions` vocabulary above. `workgraph_discovery.
check_and_propose_system_table` generalizes the RECOGNITION half of that judgment: a
sender_domain that already crosses §3's ordinary significance bar AND carries 3+ genuinely
co-occurring structured labeled fields (`_labels_cooccurring_with_domain`) looks like a
whole automated system's notification format, not one more isolated field. It drafts a real
proposal into `proposed_system_tables` - system name, suggested columns with sample values,
source examples - for human review (`GET`/`POST /api/discovery/system-table-proposals`).

Deliberately does NOT generalize the BUILD half. A confirmed proposal is a real go-ahead
decision, never a trigger that executes DDL or writes a Python extraction function -
building the actual table stays a human/dev pass, exactly how ContractPodAI/Ariba
themselves came to exist. Auto-generating and auto-applying schema/extraction code from an
LLM's own characterization is a materially riskier class of automation than anything else
this design produces (every other output is a row in an already-generic, safe table); this
mechanism stops at the proposal on purpose.

Known, accepted gap: a domain that crosses the system-table bar can also still get
individual per-field proposals if any of its labels independently cross their OWN
significance bar - no dedup between the two paths yet. A genuinely useful first cut, not a
claim this fully resolves review-queue tidiness.
