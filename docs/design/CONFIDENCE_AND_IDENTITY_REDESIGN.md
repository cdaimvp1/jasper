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

### 10.7 First real headless run, live M365 search, observed (2026-08-03)

Triggered `run_deepdive_oneshot()` directly rather than waiting for the
next scheduled cycle. **First attempt failed for a mundane, unrelated
reason, not the M365-auth risk 10.2 named:** the live server hadn't been
restarted since this session's commits, so `/api/workgraph/deep-dive/next`
didn't exist on the running process yet. Curator's own honesty requirement
worked exactly as designed — it detected the 404, refused to fabricate a
search, refused to restart the server unilaterally (per
`conductor_runbook.md`'s "propose to the manager first" rule), and reported
plainly what happened. Restarted the server directly (`POST
/api/server/restart`, confirmed all three new routes respond correctly),
then re-ran it for a genuine second attempt.

**Second attempt: real success, with two honestly-reported real limits.**
Seeded on `proj-043` ("Pwc — contract"). Curator correctly scoped its own
75 returned anchors down to the real distinguishing identity (most were
internal Lilly staff shared across many projects, useless as a search
seed) before ever calling a live tool. Net result: **11 new raw_items
ingested, 0 duplicates** — 1 real Teams message plus 10 real SharePoint
documents (executed PSAs, RFIs, amendments dating back to 2011) that had
never been indexed before. Cross-checked `marc-033`'s existing PwC mention
against the new evidence and correctly confirmed it was already linked to
the right project, not a matcher miss.

Two real, honestly-reported limits, not fabricated around:
- **A real gap in this routine's own instructions, found and fixed
  immediately:** `outlook_email_search` found genuine new content (a
  2026-08-03 rate-card thread), but the routine told curator to write it
  through the same `outlook_mail` drop-file envelope Teams/SharePoint
  finds use — that source doesn't exist in `normalize.py`'s processors.
  Mail ingestion is `outlook_com_ingest.py`, a fully independent,
  comprehensive scan of Marc's real mailbox that already runs every
  `scheduled_refresh.py` cycle regardless of Deep-Dive — a real mail hit
  needs no manual handling at all, it surfaces on its own next mail scan.
  Fixed `PROJECT_DEEPDIVE_ROUTINE.md` to say so directly instead of
  pointing at a path that never existed.
- **A live connector inconsistency, exactly the risk class 10.2
  anticipated, just a different symptom:** `chat_message_search` returned
  a real hit (a specific Teams meeting chat), but `read_resource` on that
  exact `chatId`/`messageId` the search itself returned came back
  `NOT_FOUND` — the content couldn't be retrieved even though the search
  succeeded. Not fixable from this side; noted honestly in the run's own
  `last_deep_dive_note` rather than silently dropped or guessed around.

**Step 12 is confirmed working end-to-end on real data**, both search
paths that can currently ingest (Teams, SharePoint) proven live, the
mail-path gap found and closed, and the honesty discipline validated
under a real failure (twice, in two different ways) rather than only in
tests. Watching a few more real scheduled-cycle firings is still
worthwhile, but the core mechanism is no longer unobserved.

---

## 11. Phase 4: NBA v2 — rank actions, not issues (step 13, the capstone)

Authorized by the same "proceed" as step 12, continuing the same design-
then-build pass. Last on purpose (Section 7): this is "only as good as the
identity and commitments beneath it" — both are now real, live, and load-
bearing here for the first time.

### 11.1 What v1 actually does today (read directly, not assumed)

`workgraph_nba.py` today has two, not one, existing mechanisms, and the gap
is narrower than "no action ranking exists at all":

- **`score_issue`**: one score per **issue** — `is_your_step` (state-based) +
  `staleness_urgency` + `due_urgency` (from `issues.due`, rarely set) +
  `value_urgency` (regex-extracted dollar figure), confidence-damped
  (Section 2), with a Total Recall precedent boost. Written to
  `issues.priority_score` by `recompute_all` — this is what sorts the
  Inbox today.
- **`candidate_actions`**: already produces a ranked, deduped list of
  candidate *actions* — but scoped to **one issue at a time** (called from
  the issue-detail view), merging three surfaces (the issue's own
  `nba_reason` template, per-evidence-row recommendations, curator's
  synthesis `suggested_actions`).

**The real gap "rank actions, not issues" closes:** there is no *global*
ranked action list across the whole inbox. Two issues each carrying one
middling action can't be compared against one issue carrying three
genuinely urgent ones; a single, sharp deadline buried in an otherwise-
quiet issue can't outrank a issue whose overall `priority_score` just
happens to be higher. `candidate_actions` already proved the "list of
real actions, not a bare issue verdict" idea works — this just makes it
global instead of per-issue, using a unit `candidate_actions` never had
access to: a real, individually-scoreable **claim**.

### 11.2 The claims-shaped action unit — what Phase 3 makes possible for the first time

Ranking unit: an open **`ask`/`commitment` claim with `owner == 'marc'`**
(Section 9.4) — the concrete, individually-attributable thing Phase 3
built and this doc's original goal actually meant by "work item"
(Section 9.0). `decision` claims are excluded from the ranked list itself
(`owner` is `None` by design — a decision is a joint fact, not an
obligation Marc owes) but still surface as context on the issue.

This fixes something v1 structurally could not: `workgraph_commitments.py`'s
own finding — only 5/79 commitments mentioned Marc by name — is exactly why
v1 never attempted a "my actions only" filter. `claims.owner` (deterministic,
from `direction`, Section 9.4) makes that filter real for the first time,
without a keyword guess.

### 11.3 Scoring — reusing v1's real signals, extended with what claims add

```
score = w1 * staleness_urgency(claim.first_seen_ts)
      + w2 * due_urgency_from_date_claims(issue)
      + w3 * value_urgency(issue)          # unchanged from v1, _extract_value_amount
      + w4 * escalation_bonus(claim)       # new - Section 9.3's real repeat/escalation signal
```

- **`staleness_urgency`** — same shape as v1's `_staleness_urgency`, keyed
  off the CLAIM's own `first_seen_ts` (how long has THIS specific ask sat
  open), not the issue's `updated_at` — a genuinely different, more precise
  clock than v1 had access to.
- **`due_urgency_from_date_claims`** — real, stated scope limit: `date`
  claims store curator-judged `kind` (hard/soft) and `whose`, never a
  parsed calendar date (Section 9.7 never built free-text date parsing,
  deliberately) — so this is a **tiered** urgency (hard > soft > none), not
  a decaying days-until-due curve like v1's `_due_urgency`. **Task #57's
  fix, made concrete:** a `hard` date claim contributes its full urgency
  tier regardless of `owner` — a counterparty's own deadline that still
  affects Marc is never downweighted for being `owner: counterparty`, per
  Marc's explicit standing decision. Real free-text date parsing (an
  actual days-until-due number) is a stated non-goal here, same as it was
  in 9.7 — revisit only if the tiered version proves too coarse in practice.
- **`value_urgency`** — unchanged, reused verbatim from v1
  (`_extract_value_amount`/`_value_urgency`) at the issue level.
- **`escalation_bonus`** — new, real signal Phase 3 unlocked: an escalated
  claim (`claims.escalated`, Section 9.3 — curator judged this as a
  restated ask from a different/more senior sender) is a genuinely
  stronger urgency signal than plain staleness alone; v1 had no equivalent
  because the underlying repeat/escalation data existed but was never
  consumed for anything until Phase 3.
- Confidence damping (Section 2) applies exactly as it does in
  `score_issue` today — same `context_accuracy` call, reused per-issue
  (every claim on the same issue shares that issue's own damping factor,
  no per-claim recomputation needed).

### 11.4 Output shape and dedup

`rank_actions(limit)` returns claims across every open issue, ranked
globally, each entry carrying enough to render without a second
round-trip: `claim_id`, `issue_id`, `project_id` (if grouped), `text`,
`claim_type`, `score`, `reason` (a short, human string built the same
"arithmetic, not prose" way `score_issue`'s `nba_reason` already is), and
`raw_item_id` (a real deep link, same as `candidate_actions`' evidence-row
entries already provide). **Capped per issue** (a genuinely chatty single
thread with 5 open asks shouldn't fill the whole list) — a real, stated
design call, not an oversight.

### 11.5 Scope call: additive and observe-only, NOT a cutover — stated plainly

Every structural change in this doc that touches what Marc actually sees
got a shadow-before-cutover pass first (Section 7's own discipline: steps
8-9 were the ones that changed live grouping/classify behavior, and both
got a shadow-compare before flipping anything). This is the first step in
the WHOLE build that changes the PRIMARY surface Marc looks at every day —
higher stakes than any backend ledger or migration in this doc. So:

- `rank_actions` ships as a new, additive function and a new, additive
  read-only API route (`GET /api/workgraph/actions/ranked`) — **nothing
  about the existing Inbox sort, `issues.priority_score`, or
  `candidate_actions` changes.** Both mechanisms keep running exactly as
  they do today.
- This is genuinely, deliberately **not a full cutover** — the doc's own
  "full build" instruction is satisfied by shipping the real, working
  mechanism; wiring it into the primary Inbox view (replacing or merging
  with today's issue-level sort) is a real product decision about how
  Marc wants to see his own worklist, and belongs to Marc's own review of
  this new list against real data first, the same way the scored grouping
  model's backtest results got reviewed before the flag flipped.

### 11.6 Build order

1. `score_claim`(claim, issue, ctx, now) in `workgraph_nba.py` — the
   per-claim scorer (11.3), reusing `_staleness_urgency`/`_value_urgency`/
   confidence damping verbatim, adding the tiered date-claim term and the
   escalation bonus.
2. `rank_actions(limit)` — pulls every open `ask`/`commitment` claim with
   `owner == 'marc'` across open issues, batches issue/context lookups
   (same N+1-avoidance discipline as every other batched reader in this
   doc), scores, dedupes per-issue, sorts, caps.
3. `GET /api/workgraph/actions/ranked` — new, additive, read-only route.
4. Tests: the scorer's own weight math (staleness/date-tier/value/
   escalation in isolation and combined), the owner filter (decisions and
   counterparty-owned claims correctly excluded from the ranked list
   itself), per-issue dedup/cap, confidence damping parity with
   `score_issue`.
5. No live-DB migration needed (pure read, no new columns) — but worth a
   direct check against the real corpus (does the real top-N list look
   sane) before calling this genuinely done, same as every other step's
   real-data verification.

### 11.7 Step 13 done: built, tested, checked against the real corpus (2026-08-03)

Full test suite green (~960 tests) — including a real, incidental fix found
along the way: `workgraph_nba._value_cache` is process-global, and its
existing cache-clearing fixture was scoped only to `test_workgraph_nba.py`,
not the whole suite. This test file's own small, sequential raw_item ids
(no dollar figures) primed empty cache entries that a later file
(`test_workgraph_suppliers.py`, alphabetically after) silently inherited
for colliding ids, corrupting its own real dollar-figure assertions.
Promoted to a global autouse fixture in `conftest.py` — the correct, general
fix for a process-global cache shared across files, not a one-off patch.

Checked `rank_actions(limit=10)` directly against the live DB (no migration
needed - pure read): the real top 10 are exactly the shape this step was
built for — unresolved "approve requisition PR..." asks Marc genuinely owes
a response to (`marc-014`, `marc-310`, `marc-267`, `marc-185`...), correctly
excluding decisions and counterparty-owed asks. The per-issue cap (2)
visibly fired on `marc-271` (two distinct raw_items both restating the same
PR854779-V4 approval ask, not caught as an exact-text repeat — a real,
minor, expected limitation of exact-match dedup, not a bug in this step).

**Step 13 is done — additive and observe-only, per 11.5.** The existing
Inbox sort, `issues.priority_score`, and `candidate_actions` are all
unchanged; `GET /api/workgraph/actions/ranked` is a new surface for Marc to
review against his own real worklist before any decision about wiring it
into the primary view.

**This completes the design doc's full build (Section 7, steps 1-13).**
The three explicit non-builds remain correctly parked, not forgotten:
attachment hashing (no confirmed defect behind it), the semantic identity
signal (Step 10 found no real trigger condition in the actual singleton
set). **Correction, 2026-08-03 (Marc's direct instruction, see Section
12): the `work_objects` scope call above was never actually Marc's own
choice between the smaller and fuller design — it was this doc's own
engineering judgment, presented as part of a package. Re-opened and
being built for real, full scope, per Section 12 below** (the one
deliberate exception: tenant/multi-user scope, deferred until a second
real user exists, per Marc's own generalization roadmap).

---

## 12. Ambient Work Orchestration v2 — building it right, full scope (reopened 2026-08-03)

Marc's direct correction: the scope-downs throughout this doc (no
`work_objects`, no `EvidenceUnit` layer, no `PreparedAction`, claims as a
smaller stand-in for a full claim store) were never something he
specifically weighed and chose — they were this doc's own engineering
judgment, packaged into a design he was asked to approve as a whole. His
instruction now: **build it at full scope on his real desktop, unless a
specific piece genuinely cannot run there** (not "would take more
engineering effort" — that's not a desktop limitation, that's just the
size of the work). The one confirmed exception, by his own direction:
**tenant/multi-user scope, deferred until a second real user exists** —
that one doesn't just cost more to build, it has no way to be exercised
or tested with a single operator, and his own generalization roadmap
already said to hold it until the stabilization period is declared over.

Grounded against the real current schema (not the abstract Blueprint
description) before designing anything below — `issues`/`projects` are
separate tables today with a one-way `issues.project_id` FK (two tiers
only, no nesting above project); `evidence.issue_id` is a single
mandatory FK (one raw_item's evidence can only ever belong to ONE issue,
confirmed directly in the schema — the exact constraint EvidenceUnit
exists to remove); `project_links` is built and firing (70 real pending
`link` suggestions in the queue right now, zero yet confirmed — a real
backlog, not a dead mechanism) but carries no hierarchy/nesting
semantics, only symmetric relatedness.

### 12.1 Typed `work_objects` — replaces the issues/projects split, migrated not wrapped

Marc's explicit choice: migrate the real data into one model, not wrap
the old tables with a second parallel identity system to keep in sync
forever.

```sql
CREATE TABLE work_objects (
    id               TEXT PRIMARY KEY,
    object_type      TEXT NOT NULL CHECK (object_type IN
                        ('relationship','program','project','engagement',
                         'case','request','recurring_responsibility')),
    parent_id        TEXT REFERENCES work_objects(id),
    name             TEXT NOT NULL,
    category         TEXT,
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                        ('active','waiting','done','archived','dismissed')),
    priority_score   REAL,
    nba_action_kind  TEXT,
    nba_reason       TEXT,
    owner            TEXT NOT NULL DEFAULT 'marc',
    due              TEXT,
    confidence_tier  TEXT,
    claims_revision  INTEGER NOT NULL DEFAULT 0,
    last_deep_dive_ts   REAL,
    last_deep_dive_note TEXT,
    opened_at        REAL NOT NULL,
    updated_at       REAL NOT NULL
)
```

**Migration, real and direct, not a shim:**
- Every existing `issue` -> a `work_object` with `object_type='request'`
  (an email/Teams thread is structurally "someone asking for something,"
  the Blueprint's own closest type), same `id` reused (no new id scheme —
  every existing FK across `evidence`/`claims`/`identity_anchors`/
  `source_containers`/`issue_parties`/`issue_state_history` keeps working
  unchanged), `parent_id` = the work_object created from its old
  `project_id`.
- Every existing `project` -> a `work_object` with `object_type='project'`,
  `parent_id = NULL` (no `relationship`/`program`/`engagement` tier is
  auto-inferred — `project_links`' symmetric relatedness data doesn't
  carry real parent/child semantics, so fabricating a tier from it would
  be a guess; a human or a future scored mechanism promotes a real
  `relationship`/`program` above existing projects going forward, not
  this migration).
- `issues`/`projects` tables kept, renamed to `issues_pre_workobjects`/
  `projects_pre_workobjects` (same detect-and-rename pattern this file
  already uses for every CHECK-widening migration) — never dropped,
  reversible if anything is missed.
- **Correction, made while building rather than assumed at design time:**
  the paragraph above originally said every caller across the codebase
  would need repointing at `work_objects` directly — checked against
  what SQLite actually supports before writing that code, and it's
  wrong. `issues`/`projects` become real SQLite VIEWS over
  `work_objects` (filtered by `object_type`), with `INSTEAD OF`
  triggers making them fully read/write, not just read-only
  projections. Confirmed empirically before wiring this in for real: a
  partial-column `INSERT`/`UPDATE` against the view (the exact pattern
  `create_issue()`/`update_issue()`'s own dynamic SQL already uses)
  behaves identically to the real table — unspecified columns correctly
  resolve to `NULL`/their prior value, not an error. **Zero callers
  anywhere in the codebase needed to change** — `workgraph_classify.py`/
  `workgraph_projects.py`/`workgraph_nba.py`/`workgraph_claims.py`/
  `workgraph_deepdive.py`/`server_lean.py` all keep calling
  `ws.list_issues`/`ws.get_issue`/`ws.list_projects`/`ws.get_project`
  exactly as before. `work_objects` is still the one real underlying
  table (satisfying "migrate, don't wrap") — the views are how every
  existing raw SQL statement in the file keeps working against that one
  real table without being individually rewritten.

**v2.1 done, built and migrated on the live DB (2026-08-03).** Two real
bugs found and fixed while building, both against the FULL existing test
suite (~960 tests), not assumed correct from the design:

1. `CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(...)` (and two
   siblings) — safe on a real table, but SQLite raises "views may not be
   indexed" on the second-and-later `init_workgraph()` call, once `issues`
   is a view. Unlike `CREATE TABLE IF NOT EXISTS` (confirmed to silently
   no-op against an existing view of the same name) and the existing
   `ALTER TABLE` calls (already `try`/`except sqlite3.OperationalError`-
   wrapped, which happens to catch this too), a bare `CREATE INDEX` was
   the one statement shape in this file with no existing safety net.
   Wrapped in the same `try`/`except` pattern.
2. The `issues`/`projects` `INSTEAD OF UPDATE` triggers omitted
   `opened_at` from their `SET` clause — any `UPDATE issues SET
   opened_at = ...` (used by tests simulating a specific issue age, and
   by anything that ever needs to correct it) silently had no effect,
   confirmed live via `workgraph_suppliers`' `days_to_close` computing
   `0.0` instead of a real value. Added `opened_at = NEW.opened_at` to
   both triggers.

Migrated the real live DB (backed up first, labeled
`pre_work_objects_migration`): 408 total `work_objects` (360 `request` +
48 `project`), exactly matching `issues_pre_workobjects`/
`projects_pre_workobjects`'s row counts — lossless. `integrity_check: ok`.
Restarted the live server and confirmed the real HTTP API
(`/api/workgraph/issues`, `/api/workgraph/projects`,
`/api/workgraph/actions/ranked`) all return correct data through the new
view layer, plus every Phase 3/12/13 function built earlier this session
(`rank_actions`, `list_deepdive_candidates`) working unchanged against
real production data.

### 12.2 `EvidenceUnit` — evidence can belong to more than one work_object

Removes the exact constraint just confirmed in the schema
(`evidence.issue_id` mandatory, one row = one work_object only):

```sql
CREATE TABLE evidence_units (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id   INTEGER NOT NULL REFERENCES raw_items(id),
    type          TEXT NOT NULL CHECK (type IN
                     ('email','teams','calendar','sharepoint','worker_action')),
    summary       TEXT NOT NULL,
    ts            REAL NOT NULL
);
CREATE TABLE evidence_unit_links (
    evidence_unit_id INTEGER NOT NULL REFERENCES evidence_units(id),
    work_object_id   TEXT NOT NULL REFERENCES work_objects(id),
    PRIMARY KEY (evidence_unit_id, work_object_id)
);
```

One `evidence_unit` per raw_item (not per raw_item-per-issue like today's
`evidence`) — `evidence_unit_links` is the many-to-many join that lets
"attached is the SAP redline; separately, did you see the Workday invoice
issue?" attach evidence of the SAME message to TWO real work_objects, the
exact case the current schema structurally can't represent. Migration:
one `evidence_unit` per distinct `raw_item_id` already in `evidence`, one
`evidence_unit_links` row per existing `evidence` row (mechanical,
lossless). The old `evidence` table's role as "the append-only link from
an issue to its source" is now `evidence_unit_links`; `list_evidence`/
`list_evidence_for_issues` become thin views over the join.

### 12.3 Claims extensions — edges, work-state taxonomy, and a real reconciler

Builds on Phase 3's `claims` table (Section 9) — this was already a real,
smaller version of "the claim store," not a wrong direction, just not
finished to the v2 refinements' full spec:

- **Edge table** (Section 8.2 already named this, never built):
  ```sql
  CREATE TABLE claim_edges (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      from_claim_id INTEGER NOT NULL REFERENCES claims(id),
      to_claim_id   INTEGER NOT NULL REFERENCES claims(id),
      edge_type     TEXT NOT NULL CHECK (edge_type IN
                       ('contradicts','supports','derived_from','supersedes')),
      created_ts    REAL NOT NULL,
      created_by    TEXT NOT NULL
  );
  ```
  `supersedes` folds in what `claims.superseded_by` already does today as
  a real edge instead of a bare column - the column stays for the common
  fast-path query, the edge table is the general case.
- **Work-state event taxonomy + reconciler**, replacing "materialize once,
  never revisit" with a real event log: `REQUEST_WORK`, `COMMIT_WORK`,
  `ASSIGN_WORK`, `DELIVER_OUTPUT`, `REQUEST_DECISION`, `RECORD_DECISION`,
  `DECLARE_BLOCKER`, `CREATE_DEPENDENCY`, `CHANGE_DEADLINE`,
  `CANCEL_WORK`, `CLAIM_COMPLETION`, `ACKNOWLEDGE`, `REOPEN_WORK`,
  `REPORT_STATUS` — each written by `workgraph_claims.py` at
  materialization time (a new extraction on an EXISTING open claim
  produces an event against it, not always a bare dedup-touch), each
  routed through a reconciler function that maps
  (current claim status, event type) -> new status
  (open/done/superseded/dismissed/blocked), so "can you review this?" ->
  "just following up" -> "we need it tomorrow instead" becomes one
  claim with a `CHANGE_DEADLINE` event and a nudge, not three claims.
- **Completion contracts, explicit and predicate-based** (not a weighted
  average): a `completion_contract` JSON column on `claims`, set at
  creation time for claim_type='commitment' where the extraction can
  state one (e.g. "return the redline": `{"requires": ["outbound_message_after_request", "artifact_attached"]}`)
  - checked by the reconciler before accepting a `CLAIM_COMPLETION`
  event, never inferred from silence/a deep-link click/calendar passage
  alone (v2 refinement #14 - "ignored twice" is not completion evidence).
  Statuses widen to include `stale_unverified`/`completed_inferred`/
  `completed_confirmed`, not a binary done/not-done.
- **Actor resolution correction** (v2 refinement #10): the reconciler
  never defaults ownership to the raw_item's author (today's `direction`-
  based rule) when the extraction can tell the speaker is describing a
  THIRD party's commitment ("Marc is going to send Legal the redline,"
  said BY Sarah) - `author` (who wrote this raw_item, still direction-
  based, unchanged) and `owner` (who the obligation resolves to) stay
  the two separate fields Section 9.4 already split them into; this adds
  a third, extraction-time-judged override when the text names a
  different responsible party than direction alone would derive,
  producing `owner: unassigned` rather than a wrong guess when it can't
  tell.

### 12.4 `PreparedAction` — between a commitment and executing it

```sql
CREATE TABLE prepared_actions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id           INTEGER REFERENCES claims(id),
    action_type        TEXT NOT NULL,
    proposed_parameters TEXT NOT NULL,   -- JSON
    evidence_refs       TEXT NOT NULL,   -- JSON list of evidence_unit ids
    rationale           TEXT NOT NULL,
    risk_class          TEXT NOT NULL CHECK (risk_class IN ('low','medium','high')),
    required_approval    INTEGER NOT NULL DEFAULT 1,
    policy_result        TEXT,
    state                TEXT NOT NULL DEFAULT 'proposed' CHECK (state IN
                            ('proposed','ready_for_approval','approved',
                             'executing','succeeded','failed','uncertain',
                             'rejected','expired','cancelled')),
    idempotency_key      TEXT NOT NULL UNIQUE,
    created_ts           REAL NOT NULL,
    resolved_ts          REAL
);
```

Prevents conflating "the skill/action completed" with "the obligation was
fulfilled" (v2 refinement #16) — today's `pending_actions`/
`nba_choice_log` tables are close cousins of this but don't carry a real
state machine or idempotency key; `PreparedAction` sits between a
`rank_actions` candidate (Section 11) and the actual `pending_actions`
dispatch, so an uncertain external outcome (an email send that might have
gone through before a worker crashed) is a real, queryable `uncertain`
state rather than a silent retry risk (v2 refinement #17's write-ahead
concern) - `idempotency_key` blocks a retry from double-sending.

### 12.5 `ArtifactLineage`/`ArtifactVersion` — the real answer to attachment hashing's open question

Directly resolves the attachment-hashing consumer question from this
same conversation: `sha256` was already computed on every attachment
(385/385, confirmed) but nothing read it. The 39 real duplicate-hash
groups found (all legitimate cross-work_object reuse of the same file,
zero same-work_object waste) are lineage instances, not noise:

```sql
CREATE TABLE artifact_lineages (
    id            TEXT PRIMARY KEY,
    work_object_id TEXT REFERENCES work_objects(id),
    title         TEXT NOT NULL,
    created_ts    REAL NOT NULL
);
CREATE TABLE artifact_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lineage_id      TEXT NOT NULL REFERENCES artifact_lineages(id),
    attachment_id   INTEGER NOT NULL REFERENCES attachments(id),
    document_role   TEXT NOT NULL CHECK (document_role IN
                       ('original','redline','counter_redline','clean_copy',
                        'executed_copy','exhibit','other')),
    derived_from_id INTEGER REFERENCES artifact_versions(id),
    sha256          TEXT NOT NULL,
    created_ts      REAL NOT NULL
);
```

Backfill: group existing `attachments` by `sha256`; each real duplicate
group becomes one `artifact_lineages` row with N `artifact_versions`
(one per attachment row referencing it) — surfaces exactly the "this
document also appears on N other threads" signal identified as
enhancement idea #2/#6 (task #110), now with a real mechanism instead of
a flat hash comparison. `document_role`/`derived_from_id` are set going
forward by whatever extracts/uploads a new version (curator's synthesis
routine, the claudeskills redline output if #112 is ever built) - not
backfillable from historical data with any honesty, since nothing today
records "this PDF is a redline of that one" - left NULL/`other` for
existing rows rather than guessed.

### 12.6 Richer constraint model

Extends `pending_project_suggestions`' existing pairwise
`must_link`/`cannot_link`-shaped mechanism (today expressed only as
merge/link suggestion rows) into a real, typed constraint table:

```sql
CREATE TABLE identity_constraints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    constraint_type TEXT NOT NULL CHECK (constraint_type IN
                       ('must_link','cannot_link','cannot_merge',
                        'confirm_anchor','downweight_anchor',
                        'mark_artifact_generic','override_container_class',
                        'confirm_person_alias','prevent_person_merge',
                        'confirm_work_object_parent')),
    subject_a       TEXT NOT NULL,
    subject_b       TEXT,
    reason          TEXT NOT NULL,
    created_ts      REAL NOT NULL,
    created_by      TEXT NOT NULL
);
```

`cannot_merge(work_object, work_object)` is the one this doc's own
`merge_issue_into`/`merge_issues_txn` should check before ever merging -
a real, durable "these two were confirmed separate, never re-propose it"
memory the suggestion-expiry sweep (D2) currently has no equivalent of
(today a rejected suggestion just expires and can resurface later from
fresh evidence). `mark_artifact_generic` feeds Evidence Assembly/
scoring directly: a template file (e.g. a blank order-form template
attached to many unrelated threads) downweights globally instead of only
ever being handled per-thread.

### 12.7 `ProjectSignature`-based scoring

Replaces `scored_grouping_decision`'s flat weighted-sum
(company/topic/sender/category) with a real per-work_object signature,
checked as a whole rather than summed pairwise:

```sql
CREATE TABLE work_object_signatures (
    work_object_id      TEXT PRIMARY KEY REFERENCES work_objects(id),
    definitive_ids       TEXT NOT NULL,  -- JSON: exact reference numbers
    accepted_lineages     TEXT NOT NULL, -- JSON: artifact_lineage ids
    containers            TEXT NOT NULL, -- JSON: source_container ids
    external_orgs         TEXT NOT NULL, -- JSON
    participant_roles     TEXT NOT NULL, -- JSON
    active_period_start    REAL,
    active_period_end      REAL,
    positive_vocabulary     TEXT,        -- JSON
    negative_vocabulary     TEXT,        -- JSON
    cannot_link_ids         TEXT NOT NULL -- JSON, mirrors identity_constraints
);
```

A new candidate is scored against the WHOLE signature (a hard conflict on
`definitive_ids` or a `cannot_link` entry vetoes outright, same veto
discipline D4 already established for `party` alone), not a sum of
independent per-signal weights - this is where v2 refinement #6's anchor-
decay-by-type point lands too: `definitive_ids` barely decay,
`participant_roles` decay fast, `positive_vocabulary` is a tie-breaker
only, matching `workgraph_confidence.py`'s existing `freshness`/
`provenance_reliability` split rather than inventing a second decay
model. Computed/updated incrementally as claims/evidence attach to a
work_object, not recomputed from scratch each time.

### 12.8 Provisional vs confirmed membership + exposure state

```sql
ALTER TABLE work_objects ADD COLUMN membership_state TEXT NOT NULL DEFAULT 'provisional'
    CHECK (membership_state IN ('provisional','confirmed'));
ALTER TABLE work_objects ADD COLUMN exposure_state TEXT NOT NULL DEFAULT 'not_exposed'
    CHECK (exposure_state IN ('not_exposed','shown_in_project','used_in_summary','used_for_action'));
```

Rule (v2 refinement #7): a `provisional` assignment (an auto-merge/auto-
link the deterministic matcher made with no human confirmation yet) can
be silently revised before `exposure_state` ever advances past
`not_exposed` - once Marc has actually seen it (in the Inbox, in a
synthesis, in an NBA action), it becomes `confirmed`-eligible-only,
never silently moved again. This is what gives real stability without
freezing every early machine guess forever - the "identity events never
rewritten" invariant (Section 0's original monotonicity claim) applies
to the confirmed/exposed tier, not to a provisional guess nobody's
looked at yet.

### 12.9 Three-tier timeline

Read-time views over `evidence_units`/`claims`/`claim_edges`, not a new
stored table:
- **Complete event timeline** - every `evidence_unit` + every claim
  event, unfiltered (today's `issue_state_history` plus everything else,
  unified).
- **Milestone timeline** - deterministically filtered: commitment
  created, deadline changed, decision recorded, artifact version
  produced, `PreparedAction` succeeded, blocker declared, approval
  received, commitment completed/reopened, work_object merge/split.
- **Activity stream** - the complement (routine comms), collapsed/
  summarized, never the default view.

### 12.10 Prompt-injection boundary

A real, previously-missing gate, independent of scale/tenancy: content
read FROM evidence (email/Teams/attachment text) is structurally
untrusted and can never itself become an operating instruction. Concrete
enforcement point: `workgraph_claims.materialize_claims_for_raw_item` and
`PreparedAction` creation both only ever consume STRUCTURED extraction
output (already-parsed `asks`/`commitments`/etc.) — never raw evidence
text directly — and `PreparedAction.required_approval` defaults `True`
for every `action_type` derived from evidence content, with no code path
that flips it `False` based on anything the evidence itself says (e.g. a
supplier's email cannot mark its own resulting action as pre-approved,
no matter how it's worded). Documented as a standing constraint on every
future action-generating surface, not a one-time check.

### 12.11 Tenant scope — explicitly deferred, no placeholder columns either

Per Marc's direct instruction: held until a second real user exists, same
as the generalization roadmap already said. Not even reserving
`scope`/`tenant_id` columns now, deliberately - this codebase's own
established pattern is a cheap additive `ALTER TABLE ADD COLUMN`
whenever a real need shows up (used throughout this entire doc), so
there's no real cost to waiting versus the v2 refinements' own suggested
hedge of adding the columns early "so it isn't a rewrite later." Revisit
only when Marc declares the stabilization period over per his own
roadmap.

### 12.12 Build order

Sequenced so nothing downstream is built on a foundation that's about to
move:

1. **`work_objects` migration** (12.1) - the foundation everything else
   nests under. Backup first, migrate `issues`/`projects` data, repoint
   every caller. Largest single mechanical step.
2. **`evidence_units`** (12.2) - second, because claim materialization
   and Evidence Assembly both read evidence.
3. **Claims extensions** (12.3): edges, work-state taxonomy + reconciler,
   completion contracts, the actor-resolution correction.
4. **`identity_constraints`** (12.6) - wire `cannot_merge` into
   `merge_issue_into` immediately once it exists, closing a real gap
   (rejected suggestions can currently resurface).
5. **`work_object_signatures`** (12.7) - replaces the flat weighted-sum
   scoring; re-run `backtest_scored_model()`'s equivalent against the new
   signature model before trusting it live, same discipline as the
   original scored-model gate.
6. **`artifact_lineages`/`artifact_versions`** (12.5) - backfill from the
   39 real existing duplicate-hash groups.
7. **`prepared_actions`** (12.4) - the actual execution-safety layer;
   wire `rank_actions` (Section 11) as its first real producer.
8. **Provisional/confirmed + exposure state** (12.8), **three-tier
   timeline** (12.9), **prompt-injection boundary** (12.10) - can land in
   parallel with 4-7, no shared dependency on each other.
9. Tenant scope (12.11): not built, not scheduled.

Each step: backup before any real-DB migration, full test suite green,
live-restart-and-verify, commit and push individually - same discipline
as every prior step in this doc.
