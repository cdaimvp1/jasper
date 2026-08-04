# Skill suites — bundling related skills by project situation (task #111)

## Context

Marc's own framing (2026-08-01, mid-implementation of task #15's per-row
multi-recommendation fix): "some of these are context dependent... if I'm
doing a negotiation I may want the full suite of negotiation skills run or
a subset of them, not just one of them. maybe all at once, maybe over
time." He confirmed this is a distinct axis from row-level regex matching
(`workgraph_recommend.py`, tasks #14/#15) — that mechanism looks at ONE
evidence row at a time and can never see "this whole project is in a
negotiation," which is a project-level situation, not a message-level one.

This doc is the design pass named as the next step after task #15 landed.
**No build has started.** Per this codebase's standing discipline (every
prior design doc in `docs/design/` — see `CONFIDENCE_AND_IDENTITY_REDESIGN.md`,
the meeting-grouping plan), building starts only on Marc's explicit
go-ahead, and the UI specifically needs the same "iterative discussion
before mockup" treatment past Detail Panel work got — Marc has a strong,
specific eye for this and guessing a full mockup unilaterally would waste
the discussion, not save it.

## Real grounding

**All 34 registered skills, from `config/skills_registry.json`** (action_kind → what it produces):

| action_kind | produces |
|---|---|
| `audit_invoice` | line-level exception audit and draft dispute notice |
| `category_strategy` | category strategy dashboard or cleaned spend workbook |
| `cda_intake` | completed CDA/NDA from Lilly's standard template |
| `comment_cleanup` | hygiene report and cleaned DOCX comments |
| `commercial_negotiation_prep` | rate benchmarks, TCO analysis, counter-offers, concession framework |
| `contract_compliance_detection` | compliance/spend-variance analysis (scope creep, overbilling, rate-card violations) |
| `contract_renewal_intelligence` | renewal dossier (renew/renegotiate/recompete/terminate) |
| `contract_review` | redlined DOCX + summary/vendor-response draft |
| `deal_room` | live concession ledger updated after each round |
| `deal_tab` | static 4-tab HTML deal dashboard |
| `evaluate_responses` | scored recommendations and RFP communications |
| `executive_summary` | ATC/ATS-ready executive summary |
| `legal_negotiation_prep` | prioritized positions, predicted pushback, red lines |
| `market_rate_benchmark` | rate benchmark cards or portfolio comparison |
| `meeting_prep` | one-page meeting prep brief |
| `negotiation_playbook_learning` | outcome records and playbook amendment recommendations |
| `negotiation_simulator` | roleplay session and coaching debrief |
| `obligations_tracker` | obligations register (xlsx) with deadline alerts |
| `pro_forma` | formula-driven pro-forma workbook |
| `process_qa` | sourced procurement-process answer |
| `procurement_knowledge_base` | answer sourced from Lilly's playbooks/SME contacts/approval rules |
| `rfp_build` | issuance-ready RFP package |
| `rfp_case_manager` | updated RFP case file and status |
| `rfp_response_analysis` | supplier evaluation report |
| `rfx_hub` | static 4-tab RFx dashboard |
| `scope_review` | scope diagnostic and issuance-ready SOW |
| `should_cost` | bottoms-up cost breakdown with price gap |
| `sole_source_challenge` | defensibility verdict/justification or ranked alternatives |
| `supplier_deep_dive` | single-supplier vendor brief |
| `supplier_landscape` | Top-10 supplier shortlist with fit/risk analysis |
| `supplier_qbr_prep` | QBR prep deck (pptx) |
| `timeline_estimate` | phase-by-phase Low/Base/High schedule |
| `voice_profile` | voice-matched draft or profile update |
| `workflow_map` | workflow diagram and task checklist |

**Real `issues.category` values in the live DB** (`SELECT category, COUNT(*) FROM issues GROUP BY category`):
`other` (142), `contract` (96), `financial` (81), `relationship` (14),
`compliance` (9), `expense` (5), `rfp-sourcing` (4), `onboarding` (4),
`savings` (3), `negotiation` (2), `performance` (1).

**`projects.category`** already exists as a real column (`workgraph_store.py:705`),
set once at project creation from the founding issue's category
(`workgraph_projects.py:967,972`) — it does not currently roll up live from
the project's current member issues if their categories drift after the
project forms.

## Design

### 1. Suite taxonomy — a new sibling data file, not a change to skills_registry.json

`skills_registry.json`'s own docstring is explicit that it is ONLY the
"action_kind → installed skill" mapping — mixing a curated grouping
taxonomy into that file would conflate two different concerns (what's
installed vs. how installed things relate to each other). New file:
`config/skill_suites.json`:

```json
{
  "negotiation": {
    "display_name": "Negotiation",
    "trigger_categories": ["negotiation", "contract"],
    "members": ["commercial_negotiation_prep", "legal_negotiation_prep",
                "market_rate_benchmark", "should_cost", "deal_room",
                "deal_tab", "negotiation_simulator", "sole_source_challenge"]
  },
  "contract_review": {
    "display_name": "Contract Review",
    "trigger_categories": ["contract"],
    "members": ["contract_review", "comment_cleanup",
                "contract_compliance_detection", "obligations_tracker"]
  },
  "sourcing_event": {
    "display_name": "Sourcing Event / RFP",
    "trigger_categories": ["rfp-sourcing"],
    "members": ["rfp_build", "rfp_case_manager", "rfp_response_analysis",
                "evaluate_responses", "rfx_hub", "supplier_landscape",
                "supplier_deep_dive", "timeline_estimate", "executive_summary"]
  },
  "renewal": {
    "display_name": "Contract Renewal",
    "trigger_categories": ["contract"],
    "members": ["contract_renewal_intelligence", "market_rate_benchmark",
                "supplier_qbr_prep", "contract_compliance_detection"]
  },
  "category_strategy": {
    "display_name": "Category Strategy",
    "trigger_categories": ["savings", "financial"],
    "members": ["category_strategy", "should_cost", "pro_forma",
                "market_rate_benchmark"]
  },
  "supplier_performance": {
    "display_name": "Supplier Performance Review",
    "trigger_categories": ["performance", "relationship"],
    "members": ["supplier_qbr_prep", "supplier_deep_dive",
                "contract_compliance_detection", "obligations_tracker"]
  }
}
```

A skill can legitimately belong to more than one suite (e.g.
`market_rate_benchmark` in `negotiation`, `renewal`, AND
`category_strategy` — it really is used across all three in practice).
This is a many-to-many mapping, not an exclusive taxonomy.

**The groupings above are a first draft from the real `produces` text
alone — I have no ground truth on Marc's actual workflow shape for most of
these beyond the one example (negotiation) he gave directly.** This needs
his review before anything is built on top of it (see Open decisions).

Skills with no natural suite (used standalone across many situations, not
tied to one project phase): `audit_invoice`, `cda_intake`, `meeting_prep`,
`process_qa`, `procurement_knowledge_base`, `voice_profile`, `workflow_map`.
These simply have no entry in `skill_suites.json` and keep working exactly
as they do today via the row-level mechanism (tasks #14/#15) — untouched.

### 2. Trigger mechanism — reuse `project.category`, no new signal invented

New pure function in `workgraph_recommend.py` (same house style: no LLM
calls, deterministic, computed at read time — matches how
`evidence[].recommendation` is already computed fresh on every read rather
than persisted):

```python
def suites_for_project(project: dict) -> list[dict]:
    """Suite(s) whose trigger_categories include this project's category,
    each annotated with which member skills are actually installed
    (skills_registry.get_skill_for_action) - an uninstalled member is
    listed but flagged, never silently dropped, so a suite doesn't quietly
    shrink without anyone noticing a skill install failed."""
```
Returns `[]` when `project.category` matches no suite — same "silence is
the deliberate fallback" posture already established in this module's own
docstring for the row-level case.

This deliberately does NOT add a live per-project category rollup (e.g.
recomputing from current member issues on every read) — `project.category`
already exists, is already trusted elsewhere, and a rollup is separable,
additive work if the snapshot-at-creation value turns out to drift too
often in practice. Not proposing that now without evidence it's needed.

### 3. Delivery — surfaced in the project API response, no new suggestion table

No new persisted "suggestion" row, and deliberately NOT reusing
`capability_suggestions` (that table is the skill-gap/build-a-new-skill
observation queue — task #43's future mechanism — a different concept:
"we should install/build X" vs. "you have X installed, use the suite").
`suites_for_project()`'s result is added to the existing project-detail API
response next to the project's other computed fields, matching exactly how
`evidence[].recommendation` already rides along in the issue-detail
response — a read-time computation, not a write, so there is nothing to
resolve/dismiss/expire and no new lifecycle to build.

### 4. UI — explicitly NOT designed here, three rough directions as talking points only

Per Marc's own "maybe all at once, maybe over time" framing, there's a real
open question about *pacing*, not just presentation. Three directions,
none built or mocked:

1. **Suite badge + expand.** A small badge on the project header
   ("Negotiation suite: 3/8 run") that expands into a checklist of member
   skills, each individually runnable — cherry-pick, not all-or-nothing.
2. **Single "Run full suite" action + an expand chevron for cherry-picking.**
   Optimizes for the common case (run everything) while keeping (1)'s
   granularity one click away.
3. **Staggered delivery over time.** Surface one member skill now; once its
   output is used/reviewed, surface the next rather than presenting all
   8 at once — directly matches the "maybe over time" half of Marc's own
   quote, but is a materially bigger mechanism (needs to track suite
   progress/sequencing state per project) than (1) or (2).

## Open decisions for Marc (not silently guessed)

1. **Suite membership.** The six suites and their member lists above are a
   first draft from `produces` text alone, not from real observed workflow —
   needs Marc's correction before any code references them.
2. **Trigger source freshness.** `project.category` (existing, snapshot at
   creation) vs. a live rollup from current member issues — recommend
   starting with the existing field and only building a rollup if drift
   turns out to matter in practice.
3. **UI direction** — badge+expand vs. single-button vs. staggered-over-time
   (or a combination) — needs the iterative discussion Marc has asked for
   on prior visual work, not a guessed mockup.
4. Whether a suite recommendation should ever auto-run a skill, or always
   requires an explicit click per skill (recommend: always explicit,
   matching every other skill-trigger mechanism in this codebase today —
   no skill has ever auto-run without a click).

## Build order (once approved — not started)

1. `config/skill_suites.json` + `workgraph_recommend.suites_for_project()`
   + unit tests (pure function, no server change) — lowest-risk, fully
   reviewable before anything touches the API or UI.
2. Wire `suites_for_project()`'s result into the project-detail API
   response.
3. UI — gated entirely on Marc's pick from the three directions above.
