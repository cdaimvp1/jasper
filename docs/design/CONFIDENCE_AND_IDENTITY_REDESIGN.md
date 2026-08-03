# Design: confidence/ambiguity spine + identity de-fragmentation, redesigned for Jasper

**Status:** design only (this doc). No live code changed. Verified against the
real current codebase on 2026-08-03 by three independent read-only audits (see
"Verification" section) before any redesign decision was made. Build tracking
is deliberately not yet split into tasks — see "Sequencing" for why, and what
each stage should become as a task once approved.

**Inputs to this redesign** (all in Downloads, read in full):
`Jasper_Ambient_Work_Orchestration_Remediation_Spec (1).md`, `JASPER_ACE_FOR_JASPER.md`,
`JASPER_UNIFIED_BLUEPRINT.md`. A fourth referenced file, `GitHub Access Request.html`,
was opened and confirmed to be an unrelated saved chat transcript — not used.
Governance-apparatus material from the wider corpus these came from was already
excluded by the Unified Blueprint itself and stays excluded here; nothing below
imports registries, threat models, or multi-tenant machinery a single-operator
tool doesn't need.

## 0. What this doc changes vs. the Unified Blueprint

The Unified Blueprint is good raw material — its own reconciliation section
(0.1) and phase design are sound in principle, and the verification pass below
confirms its defect list (D1-D17) is accurate, not stale. But three things in
its *plan*, specifically, are wrong for Jasper as it actually exists today, and
this doc corrects them:

1. **It couples the confidence/ambiguity spine to Identity v2.** The spine's
   `provenance_reliability` signal is specified to read from `anchor_strength`
   in a table (`identity_anchors`) that doesn't exist until Phase 2 ships. That
   means Marc gets zero confidence-calibration benefit until the biggest,
   riskiest phase of the program lands. There's no real reason for that
   coupling — Jasper's *current* code already has an implicit strength
   ordering (reference match > party/company match > category-only match).
   **Redesign: ship the spine first, against today's signals, upgrade its
   inputs later.** See Section 2.

2. **It frames Identity v2 as new construction.** The anchor audit below found
   that most of what Phase 2 proposes to build — PR/PO scoring and veto,
   party/company relationship-only linking, thread-container exclusivity,
   even the Jasper `Ref:` tag — already exists and already works, informally.
   The real gap is narrower and cheaper than the blueprint's schema section
   implies. **Redesign: Identity v2 is a formalization-and-backfill of
   existing signals plus a short, specific list of real gaps — not a rebuild.**
   See Section 3.

3. **It doesn't say what stays out of the UI.** You told me directly: a lot of
   new signal is coming, and Jasper should use it to *decide better*, not
   necessarily *show more*. Neither the Blueprint nor ACE-for-Jasper draws that
   line explicitly — both are about the engine, correctly, but a design that
   goes on to touch `cockpit.html` needs the line drawn on purpose or it drifts
   into a dashboard of scores nobody asked for. **Redesign: an explicit
   signal-rich/surface-light rule with concrete per-surface calls.** See
   Section 1.

Everything else — the additive-overlay discipline, shadow-before-dual-write-
before-read-switch, the three-bucket confidence contract (Automatic / One-touch
/ Refuse), reversible merge/split, typed speech acts, delivered-vs-verified —
is already right and is kept as-is.

## 1. The governing rule: signal-rich, surface-light

Every new signal this design adds (context coverage, freshness, provenance
reliability, referential resolution, ambiguity, volatility, anchor strength) is
**engine input by default, UI-visible only by exception.** Concretely:

| Signal use | Stays internal | Surfaces to Marc |
|---|---|---|
| Grouping decision (auto-attach vs. suggest vs. new work item) | The raw component scores | The *behavior* already there today: it just attaches, or it asks once, or it starts a new item. No score is shown — the bucket outcome is the whole interface, same as now. |
| NBA ranking / nag suppression | `context_accuracy`, `volatility_indicator` | Nothing new. Marc just sees fewer wrong nags and a next-move list that's already right more often. If he asks "why is this ranked here," that's Socrates' job (see below), not a permanent badge. |
| Socrates answers | Component confidence signals | The hedge in the *sentence* ("I'm fairly sure, based on two matching threads" vs. a flat assertion) — not a numeric confidence readout next to it. |
| Deadline attribution (#57, already decided) | — | **Deliberate exception.** "Third-party deadline — affects you" is a targeted, one-purpose badge solving one observed confusion (marc-281). It's the model for how a signal *earns* UI space: a specific failure mode, a specific fix, not "we now compute this so let's show it." |
| Identity confidence (drill-down only) | Anchor strength, session boundaries | If Marc clicks into a merge/split/confirm action, showing *why* ("matched on PR-88213, strong") is reasonable — that's explaining a decision he's already interacting with, not a new always-on surface. |

The test before adding any new visible element: **is this explaining a decision
Marc is already being asked to make, or a decision Jasper already made better
silently?** The first earns UI. The second doesn't.

## 2. Confidence/ambiguity spine — decoupled, shipped first

### 2.1 What ACE-for-Jasper and the Blueprint got right

Keep in full: deterministic, signal-only, no LLM calls, no embeddings/NLI in
the shipped version, `context_accuracy = mean(coverage, freshness, provenance,
referential_resolution)` as a **multiplier** (not a parallel axis) on match/
commitment/NBA scores, and the explicit park of the ML-heavy ambiguity models
(polysemy, dispersion, deterministic NLI) until arithmetic proves too coarse.
ACE-for-Jasper's own correction of an earlier draft — restoring ambiguity as a
real second axis instead of collapsing it into confidence — is right and is
kept: **ambiguity is a ceiling** (how many readings are plausible), **context
accuracy is a floor/multiplier** (how much you trust what you have). They're
different questions and both matter for Jasper specifically because procurement
threads carry real referential ambiguity (which "the contract" is being
discussed) independent of how *fresh or well-provenanced* the context is.

### 2.2 What changes: input sourcing, not the math

Ship `workgraph_confidence.py` now, against today's real signal vocabulary,
using a small compatibility layer instead of the not-yet-built anchors table:

```python
# workgraph_confidence.py (deterministic, signal-only, no writes)

def provenance_reliability(match_kind: str) -> float:
    """Compatibility shim for pre-Identity-v2 signal vocabulary.
    Maps today's real match categories (workgraph_projects.py's own
    ordering: reference > party/company > category-only) onto the same
    [0,1] scale the Blueprint's ANCHOR_POLICIES table will eventually
    replace this with. Same call signature either way — Section 2.3."""
    return {
        "reference": 1.0,        # PR/PO base match, veto-checked
        "jasper_ref_tag": 1.0,   # explicit Ref: JW-... tag (task #36)
        "party_company": 0.4,    # relationship-only signal today
        "category_only": 0.15,   # weak-signal proximity, no corroboration
    }.get(match_kind, 0.2)
```

`context_coverage`, `freshness`, and `referential_resolution` need no anchors
table at all — they're computable from `raw_items`/`evidence` timestamps and
the existing reference/party extraction today. Only `provenance_reliability`
needed the shim above, and it's a four-line map, not a blocker.

**Where it plugs in immediately, with zero schema changes:**
- `workgraph_projects.scored_grouping_decision` — multiply `effective =
  raw_score x context_accuracy` before the auto-merge/suggest/reject decision.
  This is *also* the fix for D4 in practice: a party-alone match's raw score
  can stay whatever it is, but `context_accuracy` on thin/stale context pulls
  `effective` below the auto-merge floor without a separate branch.
- `workgraph_nba.py` — damp `action_score` by `context_accuracy`; gate nags on
  `volatility_indicator`. `volatility_indicator` needs >=3 scoring cycles of
  history per Section 4.2(e) of the Blueprint — that data already accumulates
  every time `score_issue` runs, no new storage needed to start collecting it.
- `workgraph_socrates.answer()` — this is also where D14 (the generic "grounded
  evidence found" sentence) gets fixed for real: once there's an actual
  `context_accuracy` number, the hedge language can be genuinely conditioned
  on it instead of being a fixed template.

### 2.3 The upgrade path (Section 3 makes this real)

`provenance_reliability(match_kind)` above and `provenance_reliability
(anchor_strength)` from a future `identity_anchors` row are **the same
function signature with a better input**. When Section 3 ships, swap the
shim's lookup for a real anchor-strength read. No caller of
`workgraph_confidence.py` changes. This is the concrete mechanism that makes
"ship the spine first" actually safe rather than a throwaway prototype.

### 2.4 Acceptance (unchanged from ACE-for-Jasper/Blueprint)

Identical inputs reproduce identical output; every value in range; removing a
reference match lowers `provenance_reliability`; ageing evidence lowers
`freshness`; no embedding/NLI import in the shipped module.

## 3. Identity: formalize what exists, then close the real gaps

### 3.1 What's already real (found by direct code audit, not assumed)

| Blueprint's proposed anchor | Status in real code today |
|---|---|
| Jasper `Ref: JW-...` tag | **Fully built.** `workgraph_signals.JASPER_REF_RE`/`jasper_ref_issue_id()`, stored on `raw_items`, consumed in `workgraph_classify.cluster_and_link` (task #36). |
| Email ConversationID (thread container) | **Fully built.** `stable_key` in `ingest/normalize.py`, `thread_map` in `workgraph_store.py`. |
| Teams chat ID (thread container) | **Fully built,** container-level only (see gap below). |
| PR/PO base (work-item anchor) | **Fully built, and already does exactly what Phase 2 wants:** `workgraph_signals.REFERENCE_ID_RE`/`reference_base()`, scored and veto-checked in `workgraph_projects.py` (`_shared_reference_id`, `_vetoed_by_reference_mismatch`, `scored_grouping_decision`). This is Jasper's strongest signal today and needs no new extraction logic. |
| Supplier/company/domain, person/contact (relationship anchors) | **Fully built, already relationship-only, never sole-merge** — `workgraph_parties._company_from_domain`, `workgraph_projects._shared_external_company`/`_shared_external_party`. This already matches the Blueprint's own "weak/relationship, never auto-merge alone" rule (task #13 narrowed the live path to exactly this back in July). |

**Net finding: the anchor *detection* logic the Blueprint's Phase 2 schema
section proposes building mostly already exists.** What doesn't exist is a
typed table that names these as first-class, queryable "anchors" with a
strength value — today they're scattered function calls with an implicit,
undocumented strength ordering. That's a real gap, but it's a **formalization
job** (read the existing signals, materialize them into `identity_anchors`
rows in a backfill), not new signal engineering.

### 3.2 The real gaps (genuinely new work, found by the same audit)

| Gap | Evidence | Fix scope |
|---|---|---|
| **No Teams sub-session boundaries — built 2026-08-03 (v0)** | Teams anchors only at the chat-container level; no `replyToId`-based or gap-based session splitting exists. `replyToId` itself is still not captured by ingest (confirmed absent from real payloads) — stays a named, real gap for whenever Teams ingest is revisited. | `workgraph_sessionize.py`: reference-anchor continuity (sticky across ref-less messages) + 8h/72h gap thresholds, deliberately without the Blueprint's full rare-entity/artifact/topic-token corroboration model (nothing to reuse for those). Wired into the identity backfill as `source_sessions`, additive/observe-only — not yet consulted by live classify/grouping. Ran against the live DB: 16 Teams containers, 3 genuinely split into multiple sessions — including **marc-362**, the real multi-topic chat flagged during the merge review, which split into exactly 3 sessions (an initial burst, a `reference_mismatch` boundary when a different PR appeared, and a `gap_exceeds_72h` boundary ~2 weeks later) — confirming the sessionizer does what it was built for on the real case that motivated it. |
| **Calendar `seriesMasterId` rarely populated** | `ingest/normalize.py`'s own comment: the real Graph field is "never populated in practice," so it falls through to a synthetic organizer+time+subject key almost always. | Not a Jasper bug — a Graph API data-availability limit. Keep treating the synthetic key as a weak/diagnostic-only container, exactly as it's already treated. No new work; just don't "fix" what isn't broken on our end. |
| **Contract/order/document ID beyond PR/PO not extracted — investigated 2026-08-03, premise did NOT hold** | Checked against real subject/body text (this module's own required discipline before adding a pattern): SOW/MSA/Order-Form/Work-Order are referenced by free-text title ("Order Form 003", "SOW redlines", "Workday Early Renewal Order Form"), not a stable structured code the way Ariba's PR/PO numbering is. The one candidate found — ContractPodAI's `no-reply@contractpodai.com` "Important Dates/Key Obligations updates - 100099 -" template — carries a numeric ID in only 1 of 2 real matching emails, far too sparse to trust. | **Not built.** A regex here would either match plain English (false positives) or miss the real cases (too sparse). PR/PO's clean numbering is a property of Ariba's requisition system, not a general pattern other contract types share. Revisit only if a new automated CLM sender template appears with a consistently-populated ID — don't force it now. |
| **No attachment content identity** | Only hash in the repo is a message-level dedupe key, not a content/attachment SHA-256. | New, and lower priority — nothing today depends on it; it's a nice-to-have relationship signal, not a fix for a confirmed defect. |
| **D18 (new, found in this review): SharePoint container/item identity is backwards** | `ingest/normalize.py`'s `_process_sharepoint` sets `thread_key` to the **parent folder URL** — which is exactly what the Blueprint's own §7.2 says is *not* artifact identity ("SharePoint folder URL is not artifact identity"). This likely explains part of D17's 40-raw-items-to-1-evidence-row attrition: folder-level keying over-collapses distinct documents into one container. | Real bug, cheap fix: key on `drive_id:item_id` (already extracted, per the audit, just not used as the thread key) instead of the folder. Do this alongside the existing D17 investigation task, not as a separate Phase-2 schema exercise — it's a one-function fix, not new infrastructure. |

### 3.3 Redesigned Phase 2: backfill-first, not build-first

Rather than the Blueprint's build-the-schema-then-migrate order, do it in the
order that matches what's actually new vs. actually existing:

1. Create `identity_anchors` (as specified in Blueprint §7.4 — the schema
   itself is fine, keep it) and `source_containers`/`source_sessions`
   (§7.2) — additive, empty tables, no behavior change yet.
2. **Backfill anchors from existing signals** — a script that reads current
   `pr_number`/`jasper_ref_issue_id`/`thread_map`/party-match results and
   materializes them as typed rows with the strength values from Section 2.2's
   compatibility map. This is mechanical, not judgment-laden, because the
   underlying detection already works and is already tested in production
   (it's live right now).
3. Close the real gaps from 3.2 in priority order: D18 (SharePoint, cheap,
   fixes a confirmed defect) → Teams sessionization (real gap, moderate
   effort) → wider contract-ID regex (cheap, extends existing machinery) →
   attachment hashing (demand-driven, no confirmed defect pushing it).
4. Swap `workgraph_confidence.provenance_reliability`'s input from the
   Section 2.2 shim to real `anchor_strength` reads — same function
   signature, per 2.3.
5. Only then consider enabling the shadow scored model (`scored_model_enabled`)
   for candidate generation, per the Blueprint's own §7.7 gate (backtest,
   confirm the false-positive class is empty, party weight added,
   never-fires-alone preserved).

This ordering means **steps 1-2 alone** (formalize + backfill, no new
detection logic) already gets Jasper a real, typed, queryable identity layer
that Section 2's confidence spine can use — before any of the genuinely new
engineering in step 3 has to be committed to.

### 3.4 Domain vocabulary — fold into the same work, don't duplicate it

The audit found the automated-sender domain allowlist
(`_MACHINE_SIGNAL_DOMAINS`: ariba.com, adobesign.com, docusign.net,
concursolutions.com, an "ironclad" substring, etc.) duplicated in both
`workgraph_signals.py` and `workgraph_classify.py`, plus procurement-specific
`TOPIC_RULES` baked into classify code. Task #38 (already in the tracker,
parked as "future phase") is exactly this problem. **Do it as part of Section
3's `ANCHOR_POLICIES` formalization, not as a separate future-phase project** —
an anchor policy table and a domain-vocabulary config are the same underlying
need (named, versioned, overridable signal definitions instead of scattered
constants), and this redesign is already touching every file that currently
hardcodes them. Doing it twice, later, would mean re-touching the same
functions a second time.

## 4. What's genuinely demand-driven (parked, with real trigger conditions)

Keep the Blueprint's Phase 3 (claims/commitment ledger) and Phase 4 (NBA v2)
parked, but with concrete triggers instead of "pull in when it hurts":

- **Commitment ledger (Phase 3):** pull in when Sections 2-3 above are live
  *and* Marc still finds himself hand-tracking who-owes-what because the
  existing `checklist`/asks-and-decisions model can't express actor + due +
  delivered-vs-verified cleanly. Not before — the current model may turn out
  to be good enough once grouping stops fragmenting and confidence stops
  lying; test that before rebuilding.
- **NBA v2, ranking actions not issues (Phase 4):** pull in only after Phase 3,
  per the Blueprint's own dependency logic (NBA v2 is only as good as the
  identity and commitments beneath it) — this hasn't changed.
- **ACE's ML models (polysemy/dispersion/NLI):** pull in only if arithmetic
  `context_accuracy` demonstrably misjudges real cases Marc points out — i.e.
  reactive, not scheduled.

## 5. Migration safety

Keep the Blueprint's discipline in full: additive overlay only (never drop
`issues`/`projects` in the same release that stops reading them); shadow →
dual-write → compare → read-switch per engine; `legacy_entity_type/id` aliases
so `marc-NNN`/`proj-NNN` survive; rollback means flipping a read flag back, not
deleting v2 data; preflight snapshot + `PRAGMA integrity_check` before any
migration; a reconciliation report with hard zero-loss gates before any
cutover.

One correction to the "why keep this" reasoning: the Blueprint frames the
Phase 1 revision/audit/migration-runner discipline as scaled-down governance
kept "at lightweight scale" for a single-user tool. That undersells it —
**Jasper genuinely has real concurrency** (the `relay`/`curator`/`bridge`/`tia`
worker cohort writes to the same store from separate processes right now).
Optimistic-concurrency (`expected_revision` + 409) and an audit trail aren't
governance theater here; they're the correct fix for a real multi-writer
system. Keep Phase 1 fully, not as a scaled-down nicety.

## 6. Verification (why the above can be trusted)

Before writing this redesign, three independent audits ran against the live
code and DB (not against the specs' own claims):

- **Defect audit (D1-D17):** all 17 confirmed still real except D7, which is
  half-fixed (`ACTION_BRIDGE_ROUTINE.md`'s success path already uses
  `worker_action`; only the failure path still specifies the CHECK-rejected
  `action_note`). None of tonight's earlier fixes (#54/#55 confidence-tier
  persistence, #53 dedup, #62 dismiss, #20 project completion, the two N+1
  fixes) touch this defect list at all — they landed in unrelated code paths,
  so there's no double-fix risk.
- **Anchor audit:** table in Section 3.1/3.2 above — confirmed by direct
  reads of `workgraph_signals.py`, `workgraph_classify.py`,
  `workgraph_projects.py`, `workgraph_parties.py`, `ingest/normalize.py`.
- **Phase 0 status audit:** of the Blueprint's 5.1-5.7, only 5.2 shows real
  prior work (task #13 narrowed the *live* grouping path away from
  party-alone auto-merge in July) — but the *shadow-only* scored model
  (`scored_grouping_decision`) still carries the exact D4 hazard verbatim and
  still needs the fix before it's safe to ever enable. 5.1, 5.3, 5.4, 5.5,
  5.6 (failure path only), 5.7 are all still fully open, exactly as described.
- **Live DB baseline** (sourced via `symphony_env.sh`): issues=356,
  singletons=207 (58.1%), projects=42, pending merge suggestions=2,074
  (741 rejected, 9 confirmed), link suggestions=70 — matches the pasted
  analysis and the Blueprint's stated baseline almost exactly.

## 7. Sequencing (design order — full build, per Marc's 2026-08-03 direction)

Marc's instruction (2026-08-03): stop treating Phases 3/4 as strictly
demand-driven; prioritize completing this doc's full build and sequence
the rest. This section is the resulting order. **Done so far (1-5):**

1. ~~**Phase 0, corrected scope**~~ — done, committed, live.
2. ~~**Confidence spine v0**~~ — done, wired into NBA/Socrates for real,
   grouping observe-only. Committed, live.
3. ~~**Identity formalization backfill**~~ — done. 356 containers, 558
   anchors, live on the real DB.
4. ~~**Close real identity gaps**~~ — D18 fixed; Teams sessions built and
   run (16 containers, 3 real splits found, incl. marc-362); wider
   reference regex investigated and correctly NOT built (no real pattern
   to extract); attachment hashing still parked (still no confirmed
   defect behind it — revisit opportunistically during step 9, not before).
5. ~~**Confidence spine v1**~~ — real anchor_strength wired into NBA and
   the grouping model's observe-only fields. Done, live.
   Also done outside the original sequence, because Marc reviewed and
   approved it directly: the 13 real reference-conflict issue merges
   (`merge_issue_into`, built and applied).

**Remaining, in the order they should be built:**

6. **Measure the post-fix singleton rate.** Cheap, read-only, five minutes -
   and it's the actual trigger condition for step 9 (Section 8.3) and the
   real acceptance check for steps 3-5 above (target from Section 6:
   below 35%, down from 58%). Do this before anything else below so every
   later decision is made against a real number, not the pre-fix one.
7. **Evidence Assembly** (Section 8.1) — next lowest-risk step: reuses the
   confidence spine as its ranking function, no new signal type, "near-
   zero extra design risk" per its own section. Build this before Phase 3
   (step 10) and Project Deep-Dive (step 11), both of which want it as
   their retrieval layer — building it once now avoids two later re-builds.
8. **Wire Teams session boundaries into live classify/grouping.** The one
   piece of step 4 that's still observe-only. Shadow-compare first (what
   would session-aware grouping do differently on the real corpus vs.
   today's flat-per-chat model), review the diff, then cut over — same
   discipline as every structural change in this doc, because this one
   *does* change which issue a new Teams message lands in.
9. **Safely enable the scored grouping model** (Blueprint §7.7's gate,
   finally actionable now that D4 is fixed and real anchors exist):
   `backtest_scored_model()`, review the false-positive class, and only if
   it's clean, flip `scored_model_enabled` AND let the confidence spine's
   observe-only grouping fields become real dampers on the verdict. This
   is the step that actually moves D1's singleton rate at scale — the
   core of the original stated goal ("one work item per real thread of
   work"). Opportunistically fold in attachment hashing here if a real
   need surfaces while touching this code, not before.
10. **Semantic identity signal** (Section 8.3) — build only if step 6's
    (re-measured after steps 8-9) singleton number still shows a large
    ambiguous-but-humanly-obvious middle. If steps 8-9 already close most
    of the gap deterministically, this may turn out not to be needed at
    all - check before building it.
11. **Phase 3: claims + commitment/decision ledger** (Section 8, the
    original Blueprint §8) — no longer gated on "wait for the pain," per
    Marc's direction. Delivers the second pillar of the original goal
    ("all the tasks, deduped, actor-attributed, honest completion").
    Fold in the `contradicts`/`supports`/`derived_from` edge types here
    (Section 8.2) rather than as a separate step - claims/commitments are
    the first real consumer that needs them.
12. **Project Deep-Dive** (Section 8.4) — slot in during or right after
    step 11; gated only by Evidence Assembly (step 7, already done by
    this point) and a basic delivered-vs-verified distinction (step 11).
    Build as the sequential background sweep Marc specified, not a
    manual per-click action.
13. **Phase 4: NBA v2** (Section 9) — the capstone, last on purpose: it's
    only as good as identity (steps 6-10) and commitments (step 11)
    underneath it.

**One deliberate scope call, stated plainly:** this order does NOT build
the Blueprint's formal `work_objects` table (Section 7.1's canonical
entity unifying issues/projects under `wo-<uuid>` ids). `merge_issue_into`
(step 5's companion build) already delivers reversible, non-destructive
issue consolidation directly on `issues.id`, which is the actual behavior
work_objects existed to enable - introducing a second identity layer on
top of a model that already works would be complexity without new
capability. Revisit only if a real need for the abstraction itself (not
just the behavior) shows up.

Every step above keeps this doc's own discipline: additive overlay,
shadow-before-cutover for anything touching live classify/grouping
behavior, tested before committed, backed up before any real-DB
migration. Steps 6-7 have no live-behavior risk at all; steps 8-9 are the
first ones that do and get the full shadow-compare treatment; steps 11-13
are genuinely large builds and will each get their own design-review pass
before code, the same way this whole doc got one before Phase 0 started.

---

## 8. Second-pass addendum: bounded evidence assembly, a semantic identity signal, and project deep-dives

Marc surfaced a second pass over the wider corpus (Volumes 7-8, Appendices
F-U — material not seen in the first pass) plus his own real-world observation
that Claude + the M365 connector already does an excellent job when he
personally seeds it with a project name and asks for a timeline. Both are
evaluated here on their own merits against Jasper's real code, not accepted
because a secondhand source or a good demo said so.

**Naming note before anything else:** that corpus overloads "MCP" to mean a
governance review cycle, unrelated to Microsoft's Model Context Protocol —
which is what Jasper's own ingest code already means by MCP (the M365
connector `relay` uses). None of that corpus's acronyms (MCP-the-governance-
cycle, BGI, P-ASL, RPEE, GIL, or the Φ_chunk symbol) are used anywhere below;
everything is renamed into Jasper's own vocabulary or just described plainly.

### 8.1 Evidence Assembly — the one genuinely new piece, correctly scoped

**Correcting the claim it was pitched on.** The pasted analysis asserts
curator does unbounded "raw dumps of a thread." That's not quite right —
`ingest/SYNTHESIS_ROUTINE.md` already does delta-based reading: it diffs
against a stored evidence marker and reads only evidence *new* since last
synthesis, not the whole thread every time. So the actual gap is narrower than
claimed: within one delta, there's no bound, no relevance ranking, and no
cross-work-item view — a work item that receives an unusually large new batch
(a big attachment thread, a reopened negotiation) gets all of it, unranked,
with no signal about which parts matter most or whether any of it conflicts.
That's a real gap, just a smaller one than advertised.

**The design.** A deterministic `assemble_evidence(work_item_id, mode,
token_budget)` that selects the most load-bearing evidence under a budget,
using signals Jasper already has by this point in the design:

```
score(evidence_row) = recency_weight x freshness            # Section 2.2
                     + reference_weight x provenance_reliability
                     + relevance_weight x referential_resolution
```

This isn't a new signal set — it's **Section 2's confidence spine reused as a
ranking function**, not a second thing to build. Same module, second consumer.
Output is deterministic and replayable (same work item + same budget + same
evidence state -> same selection, every time), and it flags when two selected
rows disagree (see 8.2) instead of silently picking one.

This is the plumbing both 8.3 and 8.4 below actually need — it's not adopted
for its own sake.

### 8.2 Two small, cheap graph additions (only relevant once Section 3.3's tables exist)

- **Add `contradicts`, `supports`, `derived_from` to the `work_object_edges`
  edge-type CHECK** (Section 3.3, step 1's schema) alongside the Blueprint's
  existing `contains`/`related_to`/`blocks`/etc. This wires the ambiguity axis
  into the graph as a real, queryable relationship instead of only a score —
  useful the moment Evidence Assembly needs to say *why* it's surfacing a
  conflict instead of picking a side.
- **Treat synthesis output as an expandable digest, not just prose.** Jasper's
  synthesis already cites sources; the one addition worth making is the same
  rule already governing the identity resolver's veto
  (`_vetoed_by_reference_mismatch`) and Section 3's whole identity contract:
  *a digest only collapses evidence that doesn't contradict; if it does, the
  digest says so instead of quietly picking a version.* Not a new principle —
  extending the one principle already proven correct elsewhere in this design
  to synthesis compression too.

Compute-budget triage (only run expensive synthesis/semantic-read passes on
issues that are actually active, skip dormant `waiting`/archived ones more
aggressively) is worth checking against what `scheduled_refresh.py` actually
does today before building anything new here — I didn't find explicit
state-based skipping in it during this review, so this may already be free
(if NBA rescoring and synthesis already scope to active issues) or a cheap win
(if they don't). Not confirmed either way; check before treating it as a gap.

### 8.3 A semantic identity signal — yes, Jasper can read meaning like Marc does, with one correction

Marc's real point stands: the deterministic anchors leave a genuine middle
bucket — no exact reference, no exclusive anchor, but a human skim resolves it
instantly ("same order form, same thread from Thursday"). There's no technical
reason an LLM given the same evidence can't do that read. The right design
isn't to wall reasoning out of identity resolution; it's to make sure a
semantic read is held to the *same* trust discipline as every other signal
already in this doc — never a bypass.

**The one correction to make before building this:** the natural instinct is
to gate on the model's *own stated confidence* ("act when confident, ask when
not"). Don't. Self-reported LLM confidence is not well-calibrated — it's
exactly the overconfident-and-wrong failure mode I3 (never false certainty)
exists to guard against, and trusting it would quietly reopen the same door
this whole design has been closing. **Gate on what the read cites, not what it
claims to feel:** score its strength by how many independently-checkable
facts it names and whether they verify against the raw evidence Evidence
Assembly (8.1) fed it — not by a confidence number it reports about itself.

Concretely, this becomes one more candidate-generation source in the resolver
(Section 3's Step 3), scored like everything else:
- Its `provenance_reliability` is capped below "exact/exclusive," always — a
  semantic read is never treated as strong as a real PR/PO match.
- It **never** solo-clears the merge-of-two-established-work-items bar (I2's
  highest bar, unchanged) — same as a bare party match today.
- It **can** help attach a new, ambiguous single session to an existing item,
  but only combined with >=1 other independent signal — the identical pattern
  already used to fix D4 (party-alone can't auto-merge alone; a semantic read
  alone can't either).
- Its citations are stored and shown only on click-through, per Section 1's
  rule — this is a case where surfacing earns its keep, because it's
  explaining a decision Marc can already inspect (a suggestion, a merge), not
  a new standing readout.

This stays fully demand-driven (Section 4): build it only after Section 3's
anchor formalization is live and the post-fix singleton rate is actually
measured. If a large ambiguous-but-humanly-obvious middle remains, that's the
trigger — not before.

### 8.4 Project Deep-Dive — Marc's own idea, and it's concretely buildable today

This is the sharpest idea in what was pasted, and it's Marc's, not the
corpus's: the connector fan-out he's already used personally ("find everything
on the Workday renewal, tell me where we are") is retrieval-from-a-known-seed,
which is a fundamentally easier problem than the cold discovery problem the
grouping engine solves. It doesn't replace grouping; it's a complement, seeded
*by* it.

**Why this is low-risk to build, concretely:** `scheduled_refresh.py`'s own
comment confirms Jasper already has the exact infrastructure pattern this
needs — M365 MCP tools "only work inside a live Claude Code session," so
`relay`'s Teams/Calendar/SharePoint pulls already run as a scoped, one-shot
headless `claude -p` invocation, not a persistent unsupervised agent. A
"chase down project X and synthesize" pass is the same pattern, pointed at
search instead of ingest — not a new capability paradigm for this codebase,
just a new prompt for an already-proven harness.

**The design.** Marc's own correction, direct: on-demand-by-click is the wrong
default — "I don't work that way, one thing at a time." He doesn't want to be
the trigger; he wants it already done by the time he looks. So the primary
mode is **a slow, sequential, budget-capped background sweep**: pick one
project (oldest-deep-dived first, or highest-signal-change-since-last-pass
first), run its deep-dive to completion, move to the next, one at a time,
respecting a per-run budget — never all projects at once, never a blind
full-graph pass (that's the unscalable discovery problem again, just paid for
at LLM-call prices). A manual "do this one now" affordance can still exist as
a secondary way to jump the queue, but it's not the interaction Marc should
have to rely on for this to happen.
- Seeded by the project's own name/known anchors — the exact seed Marc types
  by hand today.
- Uses Evidence Assembly (8.1) plus live M365 search to pull in threads the
  deterministic matcher couldn't connect (no shared reference, no shared
  thread ID) — recovering exactly the orphaned singletons D1/D2 leave behind.
- Any newly-found candidate thread goes through the **existing** suggestion/
  confirm queue (One-touch bucket) — never silently attached, no matter how
  confident the read. Same rule as 8.3, same reason.
- Two caveats to carry forward, both correctly named in what was pasted: (a)
  recall is bounded by what the connector can retrieve — it's a strong reader,
  not an omniscient one; (b) a synthesis is only as honest as the completion/
  commitment tracking underneath it (Section 4's Phase 3 trigger) — a
  confident "still waiting on X" that's actually already resolved is worse
  than no synthesis at all. Don't ship this ahead of at least a basic
  delivered-vs-verified distinction existing somewhere, even a crude one.

### 8.5 Confirmed skip, in Jasper's own terms

Everything else in that corpus stays cut, and here's why in this codebase's
own vocabulary rather than "it's governance":

- Anything with adaptive/self-tuning weights conflicts directly with the
  determinism/replay-stability rule this whole design (and ACE itself) is
  built on — a scoring function whose weights change itself is exactly what
  "identical inputs -> identical outputs" forbids.
- Anything modeling multi-party harm/trust/dignity or institutional conscience
  is solving a problem this tool doesn't have — one operator, one mailbox, no
  multi-party trust surface to govern.
- Capability lifecycles, adversarial screening, distributed topology, formal
  verification — platform/multi-tenant scale apparatus, same verdict as the
  first pass, nothing changed that.
- A predictive-convergence forecaster has no real analog here — Jasper's
  suggestion backlog isn't an iterative-convergence loop, it's a queue with a
  budget (Section 3's suggestion caps already bound it directly).

### 8.6 Where this slots into the sequencing (Section 7)

- **Evidence Assembly (8.1)** follows directly after confidence spine v0/v1 —
  same module, second consumer, near-zero extra design risk.
- **Semantic identity signal (8.3)** is demand-driven off Section 3's results,
  as stated above — not before.
- **Project Deep-Dive (8.4)** is the least coupled of the three and could ship
  any time once Evidence Assembly exists — gated only by the completion-
  tracking caveat, not by Sections 3 or 8.3 at all.

### 8.7 D17 resolved: SharePoint 40→1 attrition is legitimate, not a bug

Investigated directly against the live DB (2026-08-03), per Blueprint 5.8's
own instruction not to "fix" before the root cause is known. Findings: all
40 `sharepoint` raw_items are safely stored (zero data loss - this is not a
D5-shaped defect); 39 classify as `FYI-EVIDENCE`, 1 as `WAITING-ON-OTHERS`;
only that 1 (`air_submissions_21Jan.xlsx`, linked to marc-122) ever gets an
`issue_id`, and the `evidence` table requires a non-null `issue_id` by
schema - so the other 39 structurally can't produce an evidence row, not
because anything failed. Reading the actual 39: general corporate SharePoint
noise unrelated to any procurement issue (IT test-tracking spreadsheets,
architecture exports, unrelated file shares) - correctly left unclustered,
the same "legitimate noise-filtering" shape as calendar's 126/1123. **1/40
is legitimate. No fix needed or built.**

### 8.11 Step 9 done: sender demoted, damping activated live, gate passes, model enabled

Applied Marc's approved fix: `SCORE_WEIGHTS["sender"]` 0.30 → 0.20 (party
already got this treatment in D4; sender needed the same - see the weight
table's own comment for the exact arithmetic). Also activated what v0/v1
had deliberately left observe-only: `scored_grouping_decision`'s verdict
now decides on `effective_score` (context_accuracy-damped), not the raw
ordered score - the backtest-and-review gate this was waiting for is what
this section documents.

Re-ran the backtest after the sender fix alone: different-project false
positives dropped from 80 to 36 (raw score). Then checked what the REAL,
damped verdict is for every issue behind those 36 pairs (calling
`scored_grouping_decision` directly, not the backtest's raw-score-only
check) - **34 of 36 now correctly land on "suggest," not "auto_merge"**;
thin/no-anchor context damps them below threshold live, exactly the
mechanism this was built for. The remaining 2 (`marc-357`/`marc-356`,
mutual pair, 5 matching signals, effective_score 0.92) are **not a false
positive at all** - both are the identical recurring "LEAH CLM - Send a
Contract for Signature" training-webinar calendar invite, fragmented
into two issues. Correctly belongs merged.

**Gate passes.** `config('grouping','scored_model_enabled')` set to
`true` on the live install. Note for later: `backtest_scored_model()`
itself still only checks the raw, undamped score (a conservative upper
bound, never an under-count) - upgrading it to be damping-aware would
need pre-fetching evidence/anchors once per issue alongside the existing
signal-snapshot pass to stay O(n) DB-reads; not done here, verified
instead with a direct per-issue `scored_grouping_decision` check against
the flagged pairs. Fine as a one-time verification; worth doing properly
if this gate needs re-running after a future change.

### 8.13 Step 10 checked, not built: the remaining singletons don't show the trigger condition

Section 8.3's own rule: build the semantic identity signal only if a real
sample of the no-signal-at-all singletons shows a large "ambiguous but a
human would instantly spot it" middle. Sampled the 205 remaining
singletons (132 of which are the batch run's genuine `no_match` cases)
directly rather than assuming either way. **They don't show that
pattern.** The overwhelming majority are exactly what a real one-off
procurement requisition looks like: a unique PR/PO number, a distinct
requester, a distinct subject (a specific tool, a specific one-time
purchase) - e.g. `marc-148` (PR416079-V33), `marc-297` (PR1188348),
`marc-292` (PR1182019), each a different requester approving a different,
specific, one-time thing. There is no visible pile of "these are
obviously the same deal" pairs sitting in this set the way the earlier
17 reference-conflicts or marc-362/marc-360 were.

**This is a real, worth-stating correction to the original framing, not
just a "nothing to build" note:** D1's 58% singleton figure was treated
throughout this doc (and the source Blueprint) as *the* headline problem
to fix. Looking directly at what's actually left in that number, a large
share of it looks like **correctly standalone work** - one requisition,
one approval, one done, with no second thread to belong to - not
fragmented pieces of a larger deal. The identity work in steps 3-9 was
still real and necessary (D1-D18 were confirmed, specific bugs, each
independently verified against real data, not assumed from the raw
percentage) - but the raw singleton percentage was never the right
number to chase toward zero, and shouldn't be read as "how much
identity work is left to do." **Step 10 (semantic identity signal) is
not built - the evidence doesn't support it right now.** Revisit only if
a future sample of the *then-current* singleton set shows a real cluster
of humanly-obvious matches deterministic signals keep missing.

### 8.12 Step 9's real-world result: mostly a suggestion queue, not mass auto-merge (as intended)

Ran `workgraph_projects.run()` against all 207 singletons with the model
enabled: `{"processed": 207, "auto_merged": 1, "suggested": 73,
"no_match": 132, "already_grouped": 1, "deferred_reconciliation": 0}`.

Singleton rate moved **58.1% → 57.6% (207 → 205)** from the single real
auto-merge. That's a small number on purpose, not a shortfall: with real
damping active, auto-merge now correctly requires a real anchor (Section
3.3's own bucket rule), and most genuine singletons don't share one with
anything else - that's *why* the reference-aware live model never merged
them either. The real contribution here is the **69 new, corroborated
merge suggestions** now sitting in the one-touch queue for Marc to
confirm - each one backed by an actual combined signal, not the old
2,004-row flood's single-category guesses. Confirming those by hand is
what actually moves the singleton number meaningfully; that's a review
task for Marc, not something to auto-resolve. Zero `deferred_
reconciliation` (no risky established-project collisions), `integrity_
check` clean, DB backed up beforehand.

### 8.10 Step 8 done, and a second real blocker found for step 9

Wired: `workgraph_classify._effective_thread_key()` now computes a
session-scoped grouping key for `teams_chat` items (bare thread_key for
every other source, unchanged). A session boundary inside one physical
Teams chat now behaves exactly like a different Outlook ConversationID
already does — falls through to the existing new-issue/hold-aside path
(task #54/#55) instead of force-attaching to whatever issue the flat
container used to point at. Forward-looking only, by design: this changes
where a *future* message lands, not any existing raw_item's current
issue_id — no retroactive resplit was built or run.

That last point matters for step 9. Re-checking the backtest's false-
positive class after this shipped: it's **unchanged** (still the same 80
pairs) — expected, since nothing retroactive happened and none of
marc-362's *existing* raw_items moved. But looking closer at the second
repeat offender from that class, **marc-360, found something session-
wiring can't fix**: it's not a Teams chat at all — it's a single calendar
event ("Lilly / SAP Legal Discussion: S/4HANA Cloud") that matches many
unrelated issues via `party`+`sender` alone, because a common internal
attendee (a manager/legal/coordinator-shaped contact) is invited across
many genuinely unrelated real deals. **This is the same shape D4 already
fixed for `party` alone — but `sender` (shared internal party, weight
0.30) still combines with `party`/`company` to cross the auto-merge
threshold without any structural anchor at all.** Session-splitting a
Teams container doesn't touch this; it needs a decision about the
`sender` signal itself (e.g. demote it to non-combining-alone, the same
treatment `party` already got in Phase 0/D4) before the scored model's
backtest can be considered clean. **Step 9 stays blocked on this, not
just on step 8** — flagged for Marc rather than reweighted unilaterally,
since it changes how much a shared internal contact counts as evidence
at all, and that's a real product call, not a mechanical fix.

### 8.9 Step 6/9 findings (2026-08-03): singleton rate unchanged, scored-model backtest fails its own gate

Measured against the live DB after Phase 0 + identity formalization +
the 13 merges: **singleton rate is unchanged at 58.1% (207/356)**. Not a
bug — every merge so far involved issues that were already in a project
(that's why the backfill flagged them at all), so none of this work could
have moved the singleton number; nothing yet has actually tried to attach
an ungrouped issue to anything. This is the real reason step 9 exists.

Ran `backtest_scored_model()` (read-only, no live change) as the required
pre-check before ever enabling it: **80 different-project pairs score
at/above the auto-merge threshold — the gate is not clean.** Most of them
share one root cause: `marc-362` (the confirmed multi-topic Teams chat
from the merge review) matches many unrelated issues via a shared
internal sender/party across topics that have nothing to do with each
other — the scored model can't tell those apart because it currently
reads the WHOLE chat's blended signal, not the per-session signal Teams
sessionization (step 8, already built) already computes correctly. A
smaller number of pairs cluster around a second contact (`marc-360`) with
the same shape. **Confirms the sequencing call in Section 7: step 8
(wire session boundaries into live grouping) must land before step 9
(enable the scored model) is retried** — re-run this same backtest after
step 8 ships before deciding anything about the flag.

### 8.8 First real backfill run (2026-08-03) — findings

`workgraph_identity.backfill_identity_anchors()` ran against the live DB
(356 issues): 356 `source_containers`, 558 `identity_anchors` written, and
17 real exclusive-anchor conflicts surfaced — the same PR/PO reference base
already active on two different issues. Checked all of them: **all 14
underlying pairs are already co-located in the same project** (e.g.
marc-271/marc-279 both in proj-031). This is reassuring, not alarming — it
means D1's 58%-singleton number is almost entirely genuinely-ungrouped
issues, not cross-project reference mismatches; the live grouping model is
already doing its project-level job correctly here. Whether each pair
should also collapse to one ISSUE (not just one project) is a real
judgment call the backfill correctly did NOT make automatically — some may
be legitimate separate amendments/resubmissions under the same PR
(marc-271 and marc-279 carry different dollar amounts: $1.94M vs $3.88M),
not true duplicates. Left for a human (or the semantic-read signal, Section
8.3, once built) to review via the existing, already-tested
`merge_issues()` — never auto-merged here.

---

## 9. Phase 3: claims + commitment/decision ledger — design (step 11)

Authorized directly by Marc (2026-08-03): "yes you can design this now",
then "after the design is done, build it" — design and build in the same
pass, no separate approval gate in between. This section is written before
any of it is built, against the real current code (read in full, not
assumed), the same discipline every prior section used.

### 9.0 What this actually fixes (resolves Marc's framing question)

Marc's real question: "one work item per thread" doesn't match how email
actually works — a single thread can carry many distinct asks, decisions,
and commitments. That's correct, and it means **"work item" was always the
wrong noun for two different things this doc had been conflating:**

- An **issue** (Section 3) is a *container identity* — "this thread of
  conversation is one continuous piece of work." One thread, one issue
  (mostly — see 8.9's marc-362 exception, which is exactly a container
  holding more than it should).
- A **claim** (this section, new) is the actual granular thing Marc means
  by "task" — one ask, one decision, one commitment, one deadline. Many
  claims live inside one issue. This was never built as a first-class
  thing; it has existed only as an unstructured, un-deduped, un-attributed
  JSON blob per message (9.1 below).

Phase 3 doesn't change issue identity at all. It builds the claims layer
underneath it — the thing Marc actually meant by "work item" in the
original goal ("all the tasks, deduped, actor-attributed, honest
completion").

### 9.1 Current state (read directly, not assumed)

- `raw_item_extractions` (one row per `raw_item`, PK on `raw_item_id`):
  a single unversioned JSON blob — `asks`, `decisions`, `dates_mentioned`
  (`{"text","kind":"hard"|"soft"}`), `commitments`, `key_facts`,
  `repeat_signals` (`{"ask_text","days_since_first_ask","escalated",
  "escalation_note"}`). `ingest/SYNTHESIS_ROUTINE.md`'s own rule: extract
  once per raw_item, **never re-extract** — deliberate, and the direct
  root cause of D9/D10 below.
- Three reader modules (`workgraph_commitments.py`,
  `workgraph_asks_decisions.py`, `workgraph_repeat_signals.py`) are
  **reflect-only** — they read the blob fields back out for display, with
  no dedup, no actor attribution, no lifecycle (open/done/superseded).
- `repeat_signals` is the one field that's already *real, curator-judged
  dedup* — populated only when curator judges a genuine restatement of an
  existing ask — but it is currently only ever displayed
  (`workgraph_repeat_signals.py`), never consumed to actually link or
  collapse anything. This is the load-bearing fact behind 9.3.
- `workgraph_commitments.py`'s own docstring already found the actor
  problem and refused to guess: "only 5 of 79 real extracted commitments
  even mention Marc by name... a keyword filter would be exactly the kind
  of unreliable guess this design already showed the cost of." This is
  binding on 9.4 below.
- `text_extract.resolve_item_text(item)` resolves a raw_item's full body
  from a **local file** (`raw_ref` → `body_text`/`body_html`, quote-
  stripped), never a live Outlook/Graph call — safe and cheap to run over
  the entire historical corpus, including for 9.6's backfill.
- `synthesis.synthesized_from_marker` is a string,
  `"count:N|max_ts:T"` — this is D9/D10's exact shape: a late-arriving
  historical item (e.g. a backfilled attachment with an old timestamp)
  changes `count` but not `max_ts`, so a marker comparison keyed on either
  alone can miss it. No revision/change-event infrastructure exists to
  fix this properly (the Blueprint's Phase 1 was never built — a
  deliberate right-sizing call this section narrows rather than reopens).

### 9.2 The `claims` table — materializing what curator already extracts

One new table, additive, pointed at `issues.id` directly (same scope
call as `merge_issue_into` — no `work_objects` layer):

```sql
CREATE TABLE claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id        TEXT NOT NULL REFERENCES issues(id),
    raw_item_id     INTEGER NOT NULL REFERENCES raw_items(id),
    claim_type      TEXT NOT NULL CHECK (claim_type IN
                       ('ask','decision','commitment','date')),
    text            TEXT NOT NULL,
    author          TEXT NOT NULL CHECK (author IN
                       ('marc','counterparty','unknown')),
    author_basis    TEXT NOT NULL CHECK (author_basis IN
                       ('direction','unresolved')),
    owner           TEXT CHECK (owner IN ('marc','counterparty','unknown')),
    date_kind       TEXT CHECK (date_kind IN ('hard','soft')),   -- date only
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN
                       ('open','done','superseded','dismissed')),
    superseded_by   INTEGER REFERENCES claims(id),
    escalated       INTEGER NOT NULL DEFAULT 0,
    escalation_note TEXT,
    first_seen_ts   REAL NOT NULL,
    last_seen_ts    REAL NOT NULL
)
```

This is a straight materialization of fields Jasper already extracts —
not a new extraction pass, not a new LLM call. `ask`/`decision`/
`commitment` rows come from the matching `extracted_json` list;
`date` rows come from `dates_mentioned`. Built once per raw_item, at the
same point the blob is already written — same "compute once, never
re-extract" discipline `SYNTHESIS_ROUTINE.md` already enforces, just with
a consumer that actually keeps state instead of re-reading the blob
every time.

### 9.3 Dedup and supersession — consume `repeat_signals`, don't rebuild it

The dedup signal already exists and is already curator-judged; the only
gap is that nothing reads it for anything but display. Materialization
rule for `ask` claims: if the raw_item's `repeat_signals` names an
`ask_text` matching an existing **open** claim on the same issue, don't
insert a new row — update that claim's `last_seen_ts`, and if
`escalated` is set, carry `escalated`/`escalation_note` onto it. Only
insert a new claim when curator's own judgment says it's genuinely new.

**Real gap, stated plainly:** `repeat_signals` today only covers asks.
Commitments and decisions get no equivalent judgment yet — a repeated
commitment restated in a later message would materialize as a second
open claim rather than updating the first. Fix is a small,
additive contract change (9.7), not a new mechanism: widen the *scope*
of the judgment curator already makes, don't invent a second one.

### 9.4 Actor resolution — deterministic, never a keyword guess

Binding constraint from 9.1: `workgraph_commitments.py` already proved
keyword/name matching doesn't work (5/79). The fix is to stop trying to
find Marc's name in the text at all, and use the one signal Jasper
already computes deterministically for every raw_item: `direction`
(`inbound`/`outbound`/`internal`).

- `author` = who wrote the raw_item this claim came from. `outbound` →
  `marc`; `inbound` → `counterparty`; `internal` or missing → `unknown`
  (never guessed — matches I3, never false certainty).
- `owner` (who the resulting obligation actually falls on) is **derived
  from `author` + `claim_type`**, not a second judgment:
  - `commitment`: owner = author (the person who said "I'll do X" owns
    doing X).
  - `ask`: owner = **the other side** from author (an ask made by Marc
    puts the obligation on the counterparty to respond, and vice versa).
  - `decision`: owner = NULL — a decision is a joint fact, not an
    obligation on one side.
  - `date`: owner is NOT derived from direction (see 9.7 — a date's
    "whose deadline" can't be inferred from who happened to write the
    message that mentioned it).

This resolves actor attribution for asks and commitments with zero new
curator judgment and zero keyword matching — it was sitting in a column
Jasper already fills in at classify time.

### 9.5 D9/D10 fix — a revision counter replaces the marker string

Root cause, precisely (checked directly in `workgraph_synthesis.py`, not
assumed): the top-level staleness check
(`list_stale_entities`'s `existing.get("synthesized_from_marker") ==
marker`) actually catches a late item fine — it's a full string compare
and `count` changing is enough to flip it. The real bug is one level
down, in `_new_raw_items_since`, which computes its delta as
`occurred_ts > since` where `since` is parsed out of the *old* marker's
`max_ts`. A late-arriving, old-timestamped item (`occurred_ts` older than
`since`) is correctly flagged stale at the top level but then invisible
to `_new_raw_items_since` — so `_is_material` sees an empty delta, and
`touch_synthesis_marker` silently advances the marker **without ever
actually re-synthesizing the late content**. That's D9/D10: not a
missed staleness flag, a missed delta.

Fix, right-sized to this section rather than reopening the Blueprint's
full audit/change-event system: add
`issues.claims_revision INTEGER NOT NULL DEFAULT 0`, bumped in the same
transaction that inserts or updates any claim for that issue — bumped at
**ingestion/materialization time**, so it's monotonic in the order Jasper
*learned about* an item, never in the item's own `occurred_ts`. That's
the property `_new_raw_items_since` actually needs and `occurred_ts`
structurally can't provide. Project-level revision is **derived**, not
stored: `MAX(claims_revision)` across member issues at synthesis-check
time — one fewer counter to keep in sync.

`synthesized_from_marker` becomes `"rev:N"`. Both the top-level equality
check and `_new_raw_items_since`'s delta switch to comparing against the
stored revision instead of parsing `max_ts` — "new since last synthesis"
now means "materialized after revision N," which is correct regardless
of the underlying evidence's own timestamp order. Old
`"count:...|max_ts:..."` markers are left as-is on already-synthesized
rows; the comparison treats any marker not in `rev:` form as
unconditionally stale, which forces exactly one harmless re-synthesis per
existing entity after migration — cheaper than special-casing the old
format.

### 9.6 FTS evidence index

`text_extract.resolve_item_text()` reads local files (9.1) — safe to run
over the full historical corpus once. New FTS5 virtual table:

```sql
CREATE VIRTUAL TABLE evidence_fts USING fts5(
    body, raw_item_id UNINDEXED, issue_id UNINDEXED,
    tokenize='porter unicode61'
)
```

Backfilled once over every existing raw_item, then kept current
incrementally (insert on ingest, idempotent on `raw_item_id`). Consumer:
Evidence Assembly (8.1) gets a real full-text candidate path in addition
to its existing linked-evidence ranking, and Project Deep-Dive (8.4) gets
a cheap in-corpus search to run before falling back to live M365 search.
Not a new capability class — the missing half of retrieval Evidence
Assembly was already scoped to need.

### 9.7 Task #57 folded in — `whose` on date claims

Marc's recorded decision on #57: a third-party deadline with real
consequences to Marc should be **elevated**, not downgraded, just because
it's nominally the counterparty's date to hit. `direction` can't answer
"whose deadline is this" the way it answers ask/commitment ownership — a
counterparty can write about *Marc's* deadline, or about *their own*, and
only the sentence's actual content distinguishes them. This is the one
place in this section that needs a real (small) extraction contract
change, because it needs the same kind of read curator already does for
hard/soft:

- `SYNTHESIS_ROUTINE.md` change: add `"whose": "marc"|"counterparty"|
  "shared"|"unclear"` to each `dates_mentioned` entry, judged the same
  way `kind` already is. Additive — existing hard/soft judgment,
  never-re-extract rule, and delta-based reading are all unchanged.
- `claims.owner` for `date` claims is set from this field directly
  (`unclear` → `unknown`).
- The actual "elevate, don't downgrade" behavior is a **consumption**
  rule, not a schema rule — it belongs to NBA v2 (step 13, Section 7),
  which is the first consumer that ranks by urgency. Recording it here
  now (owner captured at extraction time) means step 13 doesn't need its
  own extraction-contract change later — the data will already exist by
  the time it's needed. Historical items simply have `owner = unknown`
  for date claims until naturally superseded; no forced backfill of old
  judgment.

### 9.8 Consumers — retire the reflect-only readers, don't wrap them

`workgraph_commitments.py` / `workgraph_asks_decisions.py` /
`workgraph_repeat_signals.py` become thin views over `claims`
(`list_open_claims_for_issue(s, claim_type=...)`) rather than a second
API surface kept in permanent sync with a third. Check real callers
before deleting anything — likely just the issue-detail panel and NBA's
existing read paths — and swap them onto the new table directly rather
than leaving a compatibility shim (this codebase's own stated preference:
no backwards-compat wrappers when the call sites can just be updated).

### 9.9 Build order (this step — executed immediately, per Marc's direction)

1. Migration: `claims` table, `issues.claims_revision`, `evidence_fts` —
   additive. Back up the live DB first, same as every prior real-data
   step in this doc.
2. `workgraph_claims.py`: `materialize_claims_for_raw_item(raw_item_id)`
   (idempotent — safe to call once per raw_item, mirrors the existing
   never-re-extract discipline), the `author`/`owner` derivation (9.4),
   the `repeat_signals` dedup rule (9.3), `list_open_claims_for_issue(s)`.
3. Backfill: run materialization over every existing
   `raw_item_extractions` row (extracted_json only — no live calls).
4. FTS backfill (9.6) over every existing raw_item via
   `resolve_item_text()`.
5. Marker migration (9.5): switch to `rev:N`, wire the revision bump into
   the same transaction as claim materialization.
6. `SYNTHESIS_ROUTINE.md` contract changes: `whose` on `dates_mentioned`
   (9.7); widen `repeat_signals` guidance to commitments/decisions (9.3).
7. Point the three reader modules at `claims` (9.8); update their
   callers.
8. Tests: materialization idempotency, the full `author`/`owner`
   derivation matrix, `repeat_signals`-driven dedup, the revision-counter
   staleness fix (a synthetic D9/D10 repro), FTS backfill correctness.
9. Run against the live DB (backed up first), verify `integrity_check`,
   commit and push incrementally — same discipline as every step from
   Phase 0 through step 10.

### 9.10 Step 11 done: built and run against the live DB (2026-08-03)

Backed up first (`backup.create_labeled_snapshot`, labeled
`pre_phase3_claims_backfill`). Migration applied cleanly (`claims`,
`issues.claims_revision`, `evidence_fts` all present). Backfill results
against the real corpus (356 issues, 511 extractions, 2,524 raw_items):

- **Claims:** 423 materialized from the 511 existing extractions —
  258 asks, 89 commitments, 61 decisions, 15 dates. Author split:
  336 counterparty / 79 marc / 8 unknown (no direction recorded).
  Owner split: 230 marc / 110 counterparty / 22 unknown / 61 null
  (decisions, correctly no owner). Spot-checked several by hand — e.g.
  an ask authored by a counterparty ("Category-manager approval
  requested for PR1193376...") correctly resolves `owner: marc`, the
  exact "who actually owes the next move" read the old commitments
  module could never make from text alone.
- **Evidence FTS:** 2,105 of 2,524 raw_items indexed (419 skipped —
  genuinely empty resolved text, e.g. bare calendar entries with no
  body).
- **`integrity_check`: ok.**
- **The D9/D10 fix is confirmed live, not just in tests:** all 328
  existing `synthesis` rows still carry the pre-migration
  `"count:...|max_ts:..."` marker, so `list_stale_entities()` now
  correctly flags 248 of them for a one-time re-synthesis (documented,
  expected cost of the marker-format change, Section 9.5) — the exact
  before/after this section predicted, observed on the real corpus
  rather than assumed.
- Wired live (not just backfilled once): `POST /api/workgraph/raw_items/
  {id}/extraction` now calls `materialize_claims_for_raw_item` and
  `index_evidence_fts` inline, so every future extraction curator writes
  keeps `claims`/`evidence_fts`/`claims_revision` current without a
  second backfill ever being needed again.
- `workgraph_commitments.py`/`workgraph_asks_decisions.py`/
  `workgraph_repeat_signals.py` rewritten to read `claims` — same public
  functions, same return shapes, every existing caller unaffected. Full
  test suite green throughout (~930 tests).

**Step 11 (Phase 3) is done.** Next per Section 7: step 12 (Project
Deep-Dive) or step 13 (Phase 4/NBA v2), both now unblocked by this.

---

## 10. Project Deep-Dive: design (step 12)

Authorized by Marc's "proceed" (2026-08-03), continuing the same "design
then build" pass used for step 11. Grounded against the real code again
before writing anything, per this doc's own discipline — and that reading
found a real structural correction to Section 8.4's own framing, below.

### 10.1 The correction: this reuses relay's ingestion path, not a new queue

Section 8.4 said a Deep-Dive find "goes through the existing suggestion/
confirm queue... never silently attached." Reading `pending_project_
suggestions`'s real schema shows that's not quite right as stated:
that table is pairwise — `issue_id_a`/`issue_id_b`, both **already-existing**
issues the deterministic matcher is unsure about. A live M365 search result
is the opposite case: content Jasper has **never seen at all** — there is no
`issue_id_b` yet for an external Teams message or SharePoint doc that was
never ingested.

The real, corrected design: a Deep-Dive find doesn't need a new decision
mechanism — it needs to enter Jasper through the **same front door** every
other item already does. `ingest/GRAPH_INGEST_ROUTINE.md`'s drop-file ->
`normalize.py` -> classify -> link pipeline already handles "is this new,
and if so what does it match" correctly and safely (raw_item insertion is
already idempotent on `stable_key` — "first write wins," confirmed in
`insert_raw_item`'s own docstring, so a Deep-Dive find that turns out to
already exist is a safe no-op, not a duplicate). Once a found item is
written through that same path, the deterministic matcher does exactly
what it already does for a relay-sourced item: auto-link if there's a real
anchor, or land in the existing suggestion/hold-aside queues (task #54) if
it's ambiguous. **Deep-Dive is a scoped, seeded variant of relay's own
ingestion routine** — same drop-file mechanics, same downstream pipeline,
different search seeds (one project's own identity, not a generic top-N
recency sweep) and a different trigger (a sequential background schedule,
not "whatever's newest").

### 10.2 A real risk, inherited and named plainly: M365 auth in a headless run

`GRAPH_INGEST_ROUTINE.md`'s own line 5-6: "the Teams/Calendar/SharePoint MCP
tools only work from inside a live Claude Code session (the OAuth token
isn't portable to a standalone process, confirmed this session)."
`run_relay_oneshot()`'s own docstring documents a REAL prior failure: a
headless run reported a confident, detailed, entirely fabricated success
("5 chats, 81 messages, 25 events") while the connector's auth silently
didn't carry over and nothing was actually pulled. Relay's mitigation is a
code-verifiable proxy (did the calendar cursor actually advance) — Deep-Dive
inherits the exact same underlying risk (it needs the same MCP connector,
headless, to do a live search) but **has no equally clean proxy**: "found
nothing new" is BOTH the honest common-case outcome (recall is bounded,
most sweeps legitimately find nothing) AND indistinguishable, by outcome
alone, from a silently-failed tool call.

**Mitigation, honestly imperfect, stated as such rather than papered over:**
the routine gets the same explicit honesty requirement `RELAY_PROMPT`
already proved out ("if a tool is missing/fails/auth doesn't carry, STOP and
say so plainly — never fabricate a search that didn't happen"), and every
run — found something or not — writes a real, inspectable note (10.4) of
which searches it actually attempted and what came back, so a pattern of
silent failure is at least visible on review, even though no single run has
a hard code-verifiable guarantee the way relay's cursor check does. This is
a real, load-bearing risk to the piece Marc's own idea depends on — worth
watching the first several real runs before trusting the sweep unattended.

### 10.3 The design: sequential, budget-capped, one project per wake

Matches Marc's own correction from earlier this session ("the on-demand
synthesis is crap, I don't work that way, one thing at a time") — Deep-Dive
was never going to be a manual per-click action, and this makes it literal:
**exactly one project per scheduled_refresh wake**, same "small, bounded,
never-unattended" shape as `run_synthesis_oneshot`/`run_relay_oneshot`.

- **Scope call, stated plainly:** Projects only, not standalone issues.
  Matches Marc's own example ("find everything on the Workday renewal") and
  Section 8.4's text — a standalone issue with no group to chase down yet
  is arguably lower-value for this specific mechanism; revisit if that
  turns out wrong once real runs are reviewed.
- **Picker:** never-deep-dived projects first, then oldest-`last_deep_
  dive_ts` first (identical anti-starvation shape to `list_stale_entities`'
  ranking) — scoped to `active`/`waiting` projects only (chasing evidence
  for something already `done`/`archived`/`dismissed` has no payoff).
- **Seeds:** the project's own `name` plus every `identity_anchors` value
  across its member issues (reference numbers, company names) — the exact
  seed Marc already types by hand, derived instead of retyped.
- Uses evidence_fts (9.6) as a **free, zero-risk first pass** before ever
  touching live search — Jasper's own corpus may already hold the answer
  (an item ingested but never linked to this project) without any API call
  at all.
- Any genuinely new find is written through relay's own drop-file envelope
  + `normalize.py` (10.1) — not a new ingestion path.
- The two caveats Section 8.4 already named stay true and are now
  concretely satisfied: (a) recall is bounded by what the connector can
  retrieve, stated honestly in every run's note (10.4); (b) the "at least a
  basic delivered-vs-verified distinction" gate is satisfied by Phase 3's
  `claims.status` (open/done/superseded/dismissed) — real, if basic, now
  built and live.

### 10.4 Completion tracking — a real, code-verifiable act, not an LLM claim

`projects.last_deep_dive_ts` (new column) is set by a deterministic POST
the routine calls when it's done for this wake —
`POST /api/workgraph/projects/{id}/deep_dive_complete {note}` — never
inferred from the model's own prose, same discipline as every other write
in this doc (`synthesized_from_marker`, `claims_revision`: the code, not the
model's self-report, is what future runs trust). `note` is a short, honest,
freeform account of what was actually searched and found (or why it
stopped early) — stored as `last_deep_dive_note`, inspectable on the
project's own detail view, the same "surface it where Marc's already
looking, not as a new standing readout" rule as everywhere else in this
doc (Section 1).

### 10.5 Build order

1. Migration: `projects.last_deep_dive_ts REAL`, `projects.last_deep_dive_
   note TEXT` — additive.
2. `workgraph_deepdive.py`: `list_deepdive_candidates(limit)` (the picker,
   10.3), `derive_seeds_for_project(project_id)` (name + identity_anchors
   rollup).
3. Store + route: `mark_project_deep_dived(project_id, note)` and
   `POST /api/workgraph/projects/{id}/deep_dive_complete`.
4. `ingest/PROJECT_DEEPDIVE_ROUTINE.md`: seeds -> evidence_fts check first
   -> live M365 search (Teams/Calendar/SharePoint/mail) -> any new find
   through relay's existing drop-file/normalize.py path -> call the
   completion route with an honest note. Same honesty requirement as
   `RELAY_PROMPT` (10.2), stated explicitly.
5. `run_deepdive_oneshot()` in `scheduled_refresh.py` — same one-shot
   headless pattern as synthesis/relay, skipped entirely when
   `list_deepdive_candidates` is empty, capped to one project.
6. Tests: the picker's ranking/scope filtering, seed derivation, the
   completion-marking store function/route — the same split this doc has
   used throughout (deterministic scaffolding is tested; the LLM judgment
   inside the routine doc is not, same as `SYNTHESIS_ROUTINE.md`).
7. Wire into `scheduled_refresh.py`'s cycle. No live-DB backfill needed
   here (nothing retroactive — this only ever affects the NEXT project it
   picks) — but the first several real runs are worth watching directly
   given 10.2's named risk, before trusting the sweep fully unattended.

### 10.6 Step 12 done: built, migrated, wired (2026-08-03)

Backed up first (labeled `pre_step12_deepdive_migration`). Migration
applied cleanly (`projects.last_deep_dive_ts`/`last_deep_dive_note`
present, `integrity_check: ok`). `list_deepdive_candidates()` checked
directly against the live project list — real, active projects, all
correctly `never-deep-dived` on this first pass (e.g. `proj-043`,
`proj-024`, `proj-042`...). `search_evidence_fts` (9.6/10.5 step 2)
checked directly too — real hits, including one with `issue_id: None`
(indexed but never linked to any issue), exactly the "found but not yet
connected" case this step exists to catch. Wired into
`scheduled_refresh.py`'s cycle after synthesis, gated to skip entirely
when no candidate exists, same never-block-the-rest-of-the-cycle
discipline as every other daily/per-cycle step there. Full test suite
green (~940 tests) — deterministic scaffolding only (picker, seed
derivation, completion route); the routine's own live-search judgment is
untested by design, same split as `SYNTHESIS_ROUTINE.md`.

**Not yet observed: a real headless run actually exercising live M365
search.** That's the one part of this step no test or direct DB check can
verify — per 10.2, worth watching the first several real
`run_deepdive_oneshot()` firings (next scheduled_refresh cycles) directly,
checking `projects.last_deep_dive_note` for an honest account each time,
before trusting the sweep unattended.

**Step 12 is done, pending that first real-world observation.** Next per
Section 7: step 13 (Phase 4/NBA v2), the capstone.
