# Jasper Roadmap

This is the persistent record of work that's been deliberately identified
and deliberately **not** built yet — as opposed to work that's simply
unplanned. Everything here was scoped from a real gap (found in the code,
or in Marc's own live feedback), then explicitly deferred rather than
queued, for a stated reason. Nothing on this list should be started without
Marc's explicit go-ahead at the time.

This file exists because task-list items get deleted once they're recognized
as roadmap (not active-queue) work, so the roadmap content would otherwise
only survive in conversation history and commit messages. This is the
durable version.

---

## Standing guardrail: the 2-point candidate-detection gate is load-bearing (2026-08-12)

Marc's own framing, verbatim: "whatever we do it need to continue to improve
or maintain [grouping] ... the whole product fails if that regresses at
all." This is a permanent operating constraint, not a one-time note tied to
any single build cycle.

**The rule this protects:** `workgraph_pipeline2.py`'s deterministic
candidate-detection gate — a work object becomes a *candidate* for LLM
judgment only if it shares two or more real, named matched data points with
the new item (`if len(points) >= 2: candidates.append(...)`). This exact
rule was introduced by task #184 (replacing the old weighted-score model)
and has survived unchanged through every commit since, including every
Track A/B/C commit from the 2026-08-11/12 review-response cycle — verified
directly against the commit history, not assumed. Distinct from, and
upstream of, *candidate judgment* (the LLM call deciding which cleared
candidate, if any, is the real match) — that layer has legitimately changed
this cycle (Track B.5's comparative-call rewrite), but never the gate that
decides who's even eligible to be judged.

**The standing rule:** any future change that touches `find_candidates`,
`_matched_data_points`, the `>= 2` threshold itself, or `judge_candidates`'s
verdict logic must be called out explicitly before being built — never
bundled quietly into an unrelated task — and must ship with a before/after
comparison against the existing regression corpus
(`tests/test_workgraph_regression_corpus.py`) plus a live backtest, the
same discipline already used before task #184's rewrite and task #180's
scored-model backtest went live. No exception for "it's just a
refactor" — the whole product's trustworthiness rests on this gate staying
deterministic and at least as accurate as it is today, never quietly
weaker.

**Currently the only queued item this applies to:** aggregating candidates
by parent Project before judgment (see the external-review findings below)
— it does not touch the `>= 2` threshold, only how already-gated
candidates get bucketed before the comparative LLM call, but it's close
enough to the gate to require the shadow-compare treatment above before
shipping.

---

## Track C.11 — labeled judgment corpus + end-to-end simulation test (HELD)

Originally scoped in the 2026-08-11 design plan (`goofy-jingling-owl.md`,
point 11) alongside Tracks A/B/C.9, but explicitly held back on Marc's own
repeated, direct instruction the same session ("do not run c.11 yet",
"we will hold on that for now") — moved here from the active task list
(was task #352) on 2026-08-13 per Marc's own request, specifically so the
full scope survives even though it's off the queue.

**Problem this addresses:** existing tests (`test_workgraph_pipeline2.py`,
`test_workgraph_regression_corpus.py`) mock the LLM entirely — they prove
"Jasper handles a given verdict correctly," never "Claude actually
produces the right verdict on real, messy business traffic." No test in
this codebase measures real semantic-judgment accuracy.

**Full original scope:**

- `tests/fixtures/labeled_judgment_corpus.jsonl`: 300–500 hand-labeled
  evidence pairs across categories: same-Project-across-unrelated-threads,
  same-supplier-different-Project, overlapping-stakeholders-unrelated-work,
  a new reference appearing late, forwarded chains, attachment-only-
  identity, Teams+email combinations, prime/subcontractor cases, amount
  changes, malformed/noisy correspondence, sparse evidence, true
  ambiguity — sourced from real (anonymized) historical evidence where
  possible, synthetic where not.
- `tests/test_judgment_accuracy.py`: runs the real `judge_candidates`
  against the corpus with real model calls, computes precision/recall/
  false-merge-rate/false-split-rate/abstention-rate, asserts a floor and
  logs actual numbers to a tracked file so drift across model/prompt
  changes is visible over time. Gated behind an explicit marker/env var —
  cost and latency mean this must never run in the default fast test loop.
- `tests/test_e2e_chronological_simulation.py`: feeds a scripted month (or
  30–90 days) of evidence through the full ingest→classify→pipeline2→
  synthesis→NBA pipeline in timestamp order against a scratch DB, using a
  comprehensive canned-verdict table (matching the labeled corpus) at the
  LLM boundary — validates pipeline wiring/ordering (e.g. would have
  caught the point-7/settlement-pass staleness gap directly), distinct
  from and complementary to the accuracy test above, not a duplicate of it.

**Agreed cost-control design** (ready to apply the moment this is
unblocked): small corpus (~20–30 pairs to start, sourced from real
anonymized cases) rather than the full 300–500 immediately; an opt-in
pytest marker so it never runs by default; Haiku-tier model by default
for the accuracy test; the e2e simulation test fully mocked (no real LLM
calls at all) since it's testing wiring, not judgment quality.

**Why this matters going forward:** the 2026-08-13 external architecture
review (see "External architecture review findings" below) independently
arrived at the same conclusion — a real semantic-accuracy evaluation is
one of the highest-leverage remaining investments, now that Jasper has
"a lot of capabilities" and the open question is whether its
representation of reality can be trusted, not whether more capability is
needed. That review's "semantic-accuracy evaluation" hardening item and
this track are the same piece of work — do not build twice.

**Trigger to revisit:** Marc's own explicit go-ahead. Not before.

---

## UI / integration parity items (2026-08-11)

Deferred during the Phase 1 build-queue pass. Each is a real, scoped gap;
none blocks anything currently on the active queue.

- **Generalize UI grouping/labels to read from per-role vocabulary**
  (post-demo). Cockpit's grouping labels and category vocabulary are
  currently procurement-specific; this is the UI-facing half of the broader
  generalization-beyond-procurement track below.
- **Teams auto-respond to status/process questions after timeout** —
  deferred pending the new Symphony (Teams-enabled workers) install.
- **Native M365/SharePoint file-sharing (Share button parity)** — matching
  the real Share button behavior users already expect from M365 apps,
  rather than Jasper's current ad hoc link handling.

Explicitly NOT on this roadmap, per Marc's own instruction: "Run multiple
adversarial testing passes against Jasper" was removed entirely, not
deferred — it is not a future task, it was withdrawn.

---

## Phase 4 — deeper authority/policy model (beyond task #317)

Task #317 (queued/building now) covers the one Phase 4 piece that's real
and single-user-relevant: a deterministic dispatch table deciding
`prepared_actions.required_approval` per `action_kind`.

Everything deeper than that — delegation of authority, per-actor role
separation, an audit/appeal flow, a configurable per-org policy layer — is
inherently about **multiple actors** checking or delegating authority to
each other. With exactly one user on one machine, there is no second actor
to gate against or delegate to. Building this now would be speculative
infrastructure with nothing real to exercise it — precisely the anti-
pattern the engineering-direction doc warns against.

**Trigger to revisit:** a second real user/actor exists, or Jasper moves
off Marc's local-only install.

---

## Phase 5 — third learning domain (behavioral adaptation loop)

Of the 5 learning domains named in the original audit, 2 were already real
(grouping/precedent via `workgraph_lessons.py`, keyword mining via
`personal_patterns.py`) and 2 more are single-user-valid and now queued:

- Task #318 — NBA outcome/behavioral learning (accept/dismiss/rewrite
  tracking)
- Task #322 — process learning (recurring work-sequence mining)

The third missing domain is a broader feedback loop that adapts *how*
Jasper behaves based on the accumulated pattern of how Marc actually
responds over time — distinct from #318's narrower per-suggestion signal.
This doesn't strictly require multi-machine or multi-user, but Marc's own
stated plan sequences it last on purpose: it needs weeks of real
accumulated usage data to have anything real to learn from, not more
infrastructure. See `[[jasper-generalization-roadmap]]` (memory), point 3,
for the fuller framing, including Marc's own concrete example (correcting
a suggested action so Jasper remembers the precondition — which maps onto
extending `workgraph_aristotle.py`'s existing gating with a natural-
language rule-teaching path).

**Trigger to revisit:** #318 and #322 have been running long enough to
produce real accumulated signal to learn from.

---

## Phase 6 — multi-install / multi-user (zero code today)

Confirmed zero code exists for this phase. By definition, Phase 6 is about
running Jasper for more than one person or one install — it has no
meaning against a single local machine, so there is nothing "doable today"
to extract from it.

**Trigger to revisit:** Marc's own stated gate — run Jasper stably for
weeks on his own machine first, then give explicit go-ahead to generalize.
See `[[jasper-generalization-roadmap]]` (memory) for the concrete 5-point
scope already identified for that phase (domain vocabulary as config,
de-hardcoding the UI's fixed cast, onboarding friction, a data-driven
skills registry, a real per-org policy layer) and its recommended
sequencing.

---

## Outlook add-in functional hardening (2026-08-12, functional-only review)

Confirmed by directly reading `taskpane.html`'s ~1,080 lines of inline JS
(the add-in's real client, at `C:\Users\lane_marc@lilly.com\outlook-addin-test`
— NOT inside this repo, and NOT in the `Jasper App (source + empty schema).zip`
archive, which is why an earlier external review of that archive couldn't
audit it) and cross-checking every API call against `server_lean.py`,
`outlook_actions.py`, and `workgraph_assistant.py`.

- **Silent failure on the four core one-click actions.** `openEmail`,
  `draftReply`, `draftForward`, `draftHeroReply` all call `.finally()` and
  never inspect the response body. `outlook_actions.py` has a real
  `{"ok": false, "error": ...}` contract (Outlook not running, a stale
  EntryID, a 120s COM timeout) — none of it reaches the user; the button
  just quietly resets as if nothing happened. Highest-leverage fix in this
  whole list — touches the most-used paths, one-line change per action.
- **No success confirmation either** — even a successful draft-open gives
  no "opened in Outlook ✓" signal; the dispatched-chip only fires on a
  keyword guess against chat replies, never on these direct button clicks.
- **Optimistic UI with no rollback** — `resolveHeroAction`/`resolveLaneItem`
  dim/mark an item resolved *before* the fetch completes and swallow the
  error on failure; a failed done/dismiss looks permanently successful.
- **Ribbon/command-surface buttons do nothing distinct.** All four declared
  extension points (message read/compose, appointment organizer/attendee)
  just open the same task pane; `commands.html`/`commands.ts` are inert
  scaffolding. No one-click ribbon action exists anywhere.
- **A real dispatch-confirmation channel** from `workgraph_assistant.ask`
  (e.g. a structured `tool_calls_summary` field) instead of the
  `DISPATCH_MARKERS` keyword-guess against reply text.
- **Compose-mode subject/recipient matching** — currently a static "Jasper
  doesn't smart-match a draft to a project yet" stub; real, useful signal
  sitting unused.
- **Push-based output-badge/notification** instead of 60s polling, once an
  SSE/WebSocket layer exists (ties into the M365_PLUGIN_INTEGRATION.md
  §5c target).
- **A remote-access story** — `JASPER_API` is hardcoded to
  `http://127.0.0.1:8700`; the add-in only works when Outlook and the
  Jasper server share a machine. This is the real ceiling on "powerful"
  until there's a reachable-from-anywhere backend.

  **Deliberately kept off the prioritized "do now" list (2026-08-12):**
  this is a real deployment/infra decision (standing up a personal
  tunnel/VPN, or a reverse proxy, so the same backend is reachable from
  somewhere other than the literal same machine) rather than a code fix —
  genuinely doable solo, with no Graph API or IT/tenant dependency, but
  different in *kind* from the 28 scoped engineering items above/below:
  it's an architecture/deployment choice, not a bounded bug or feature.
  It touches zero backend decision logic — no grouping, no judgment,
  nothing — only how the add-in's JS reaches the already-existing
  backend. Scope this separately, on request, once Marc wants to make
  that call.

---

## External architecture review findings (2026-08-12)

A second, independent review of the regenerated source archive (task #353),
much more aggressive than the first pass, explicitly scoped to backend/data
architecture rather than UI. Every falsifiable claim below was directly
verified against the live code before being added here — exact line
counts, exact file locations, and reproduction logic all checked out.

### Bugs — real, confirmed, fix-now priority

- **Grouping-verdict prompt contradiction** (`workgraph_pipeline2.py`,
  `_COMPARATIVE_JUDGMENT_PROMPT_TEMPLATE`). The prompt defines two valid
  verdicts (`SAME_PROJECT`, `RELATED_DIFFERENT_PROJECT`) but its own
  "respond with EXACTLY these lines" example hardcodes
  `VERDICT: same_project` — the model is being shown a literal example
  that contradicts the instruction one line above it. The parser itself
  correctly accepts both values (`_COMPARATIVE_VALID_VERDICTS`), so this
  is a prompt-text bug, not a parsing bug, but it can actively suppress
  the Project-vs-Relationship distinction the whole comparative-judgment
  rebuild (Track B.5) exists to make. This is very likely a regression:
  task #341 fixed this exact contradiction in the OLD prompt template,
  which Track B.5 then fully deleted and rebuilt from scratch, silently
  reintroducing it. Trivial fix: `VERDICT: <same_project|related_different_project>`,
  plus a test that forces a fake/mocked `related_different_project` output
  through the real parser.
- **Authoritative claim-closure correlation is too broad**
  (`workgraph_claims_backfill.resolve_authoritative_closure_signals`).
  Auto-closes a claim when a deterministic closure signal (e.g.
  `signature_fully_executed`) lands on an issue with EXACTLY ONE open
  ask/commitment claim — but never checks that the closure signal actually
  corresponds to THAT claim. A signature-execution notification on an
  issue whose one open claim is "send Jane the status update" would
  currently auto-close that unrelated claim. The fix needs a real
  correlation (matching reference/artifact/request-type between the
  closure signal and the claim it's closing), not just claim-count
  arithmetic. Silent false-completion is one of the worst error classes
  this system can produce — this is a real, high-priority gap.
- **Settlement pass settles NBA but not alerts**
  (`ingest/scheduled_refresh.py`). `alerts_result_1`/`alerts_result_2` run
  early in the cycle, before synthesis/relationship/noise/lifecycle sweeps
  can change graph state, yet the summary dict labels `alerts_result_2` as
  `"alerts_final"`. The actual end-of-cycle settlement block only calls
  `workgraph_nba.recompute_issues(...)` — there is no matching
  `workgraph_alerts.run()` call there. Confirmed exactly as described.
  Fix: add a real settlement-time alerts pass, or scope it to the same
  `settlement_touched_ids`; stop calling the pre-settlement values
  `"_final"` until they actually are.
- **8-candidate comparative-judgment cap can hide the correct match**
  (`workgraph_pipeline2.judge_candidates`, `_MAX_COMPARATIVE_CANDIDATES = 8`).
  Ranked by matched-signal count, anything past 8 is dropped from the
  prompt entirely (it IS logged — "no silent caps" discipline — but never
  reaches the model). If 9+ candidates tie on signal count, the real
  match can simply not be one of the 8 shown. Needs one of: collapse
  candidates by parent Project before judgment (see next item, likely
  subsumes this), deterministic tie-breakers beyond raw count, batched
  judging with a second comparison pass, or an explicit "too many
  candidates, needs a deeper pass" outcome instead of a silent top-8 slice.
- **Candidate judgment operates at Issue/cluster level, not Project
  level.** `find_candidates`-equivalent logic pulls from `list_issues()` +
  `list_clusters()`; several Issues under the SAME Project can each
  independently consume one of the 8 candidate slots (the only dedupe is
  skipping candidates that already share the NEW item's own project_id,
  which doesn't help when the item has no project yet — the exact case
  candidate search exists for). The real semantic question is almost
  always "which Project does this belong to," not "which Issue inside a
  Project wins" — aggregating qualifying candidates by parent Project
  before judgment, then resolving Issue placement separately, improves
  both accuracy and token use, and likely also fixes the 8-candidate cap
  problem above by shrinking the real candidate count.
- **PowerShell's `missing_attachments` result is discarded.**
  `outlook_actions._run_powershell` returns unconditional `{"ok": True}`
  on exit code 0 — it never parses stdout for the JSON the PowerShell
  compose script actually emits (`attached: [...]`, `missing_attachments:
  [...]`). `compose_new()`'s own docstring explicitly says "the returned
  dict's own missing_attachments list... must be checked by the caller" —
  but that key never exists in the returned dict at all. A caller can
  currently be told a review email went out "with the contract attached"
  when Outlook actually created the draft with zero attachments. Parse
  and propagate the real PowerShell JSON result; for any workflow whose
  purpose requires the attachment, treat "requested N, attached 0" as a
  real failure, not a successfully-created empty draft.
- **`/api/action/compose-new` accepts raw filesystem paths from the
  caller** (`attachment_paths: list[str]`), unlike the safer
  `/api/action/draft-review-request` (which correctly takes an
  `attachment_id`, verifies ownership against the issue's real
  attachments, and resolves the path server-side). Confirmed: no live
  caller currently passes `attachment_paths` through this route (the
  add-in's own `composeToParties` only ever sends `to_emails`; the chat
  tool layer's `jasper_draft_review_request` already uses the safe
  `attachment_id` path) — so today's actual exploitability is low, exactly
  as the review itself caveated. Still a real latent hole the moment this
  route becomes more broadly AI-callable. Fix: attachment IDs everywhere,
  paths resolved server-side, same discipline `draft-review-request`
  already uses.
- **Large payloads still go through Windows argv, not stdin, in two
  places task #309's fix didn't reach.** `outlook_actions.draft_reply`/
  `compose_new` pass `body` as a plain PowerShell CLI argument; separately,
  `workgraph_assistant._run_claude` passes the ENTIRE user chat message as
  a `claude -p <prompt>` argv argument, stacked on top of an already-large
  `--append-system-prompt` (the full `_SYSTEM_PROMPT` text) and a
  comma-joined 29-tool `--allowedTools` list in the same command line.
  Windows' CreateProcess command-line ceiling (~32,767 chars) is generous
  but not infinite, and exactly the kind of content this system is being
  asked to generate more of (portfolio reports, stakeholder updates, long
  drafted replies) is what would trigger it. Same fix task #309 already
  proved out elsewhere: stdin or a temp UTF-8 file, not argv, for anything
  whose length isn't bounded.
- **`focus-email`'s ambiguous-conversation-id handling silently picks one
  project.** `workgraph_store.project_id_for_conversation_id`'s own
  docstring admits a single Outlook `conversationId` can, rarely, span
  items linked to different Projects, and its resolution is "pick
  whichever linked item occurred most recently" — no ambiguity signal
  reaches the caller, no identity-conflict event is logged. Confirmed as
  described, though the docstring itself already flags this as a known,
  accepted rarity. Worth surfacing (`matched: true, ambiguous: true,
  projects: [...]`) rather than hiding, consistent with this session's own
  "never hide an internal graph contradiction" discipline elsewhere
  (Track B.8's identity-conflict audit).
- **`PRAGMA foreign_keys=ON` is never executed** anywhere in
  `workgraph_store.py`. SQLite does not enforce declared FK constraints by
  default, so the schema's many declared foreign keys are currently
  documentation, not enforcement.

  **Task #365 audit (2026-08-12, read-only, run against a copy of the live
  DB, never the production file) found this is NOT a simple "audit orphans,
  repair, flip the switch" job — the real picture is worse than "unenforced":**
  - `issues` and `projects` are VIEWs over `work_objects` (the v2.1
    work_objects migration, `#114`), not tables. `PRAGMA foreign_key_check`
    itself throws `foreign key mismatch` the instant it reaches any FK
    declared `REFERENCES issues(id)` / `REFERENCES projects(id)` — a view
    has no rowid/unique index SQLite can enforce a FK against. 2 columns
    (`nba_outcome_log.issue_id`, `pending_issue_state_suggestions.issue_id`)
    hit this directly.
  - 6 more columns (`artifact_lineages.work_object_id`,
    `data_point_values.work_object_id`, `evidence_unit_links.work_object_id`,
    `work_object_relationships.from_id`/`to_id`,
    `work_object_signatures.work_object_id`) still declare
    `REFERENCES work_objects_pre_fix4(id)` — a table `#339`'s own fix
    renamed away and never restored; the referenced table **does not exist
    at all**. SQLite auto-rewrites a child table's stored FK text when its
    parent is renamed (confirmed live) — these columns are fossils of that
    rename, not of anything in the current schema.
  - 9 more columns (`claims.issue_id`, `issue_state_history.issue_id`,
    `issue_parties.issue_id`, `source_containers.issue_id`,
    `identity_anchors.issue_id`, `checklist_dismissals.issue_id`,
    `nba_choice_log.issue_id`, `work_tasks.issue_id`,
    `lessons.source_issue_id`, plus `project_links.from_project_id`/
    `to_project_id`) still say `REFERENCES issues_pre_workobjects(id)` /
    `REFERENCES projects_pre_workobjects(id)` — the SAME rename-fossil
    pattern from the original `#114` migration. Checking these for real
    finds **~20,700 "orphan" rows** (8,717 in `claims` alone) — but these
    aren't data-integrity bugs; they're rows created normally after the
    migration, whose issue/project ids correctly live in `work_objects`
    and simply don't exist in the frozen pre-migration snapshot table the
    stale FK text still names.
  - Net: of ~43 declared FK columns audited, only a genuine handful
    (`raw_items`, `claims`, `parties`, `attachments`, `data_point_definitions`,
    `work_objects` self-refs, etc. — tables that were never renamed) are
    actually checkable and clean today. **Flipping `PRAGMA foreign_keys=ON`
    in production right now would immediately break every INSERT/UPDATE
    touching the 17 affected columns** (view-mismatch error or "no such
    table" on the ones pointing at vanished snapshot tables) — this is not
    a "some cleanup rows" situation, it's "most of the FK graph points at
    names that no longer resolve to anything real."
  - **The actual fix is a schema rewrite**, not a data repair: every
    affected table needs its `CREATE TABLE` recreated with its FK
    re-pointed at the table that's real *today* (`work_objects` in place of
    `issues`/`projects`/`issues_pre_workobjects`/`projects_pre_workobjects`/
    `work_objects_pre_fix4`) — SQLite can't `ALTER ... REFERENCES` in
    place. That's the same scale of work as the "Formalize schema
    migrations" item below (migration ledger, backup, transactional
    rewrite, post-migration integrity audit) — folded into that item
    rather than attempted as a quick flip. Doing it as an isolated
    one-off, table by table, without that scaffolding, on a DB with real
    accumulated history, is exactly the kind of thing worth a deliberate
    pass, not a same-session patch.
  - **Decision for now:** do not enable `PRAGMA foreign_keys=ON` anywhere
    (tests or production) until the FK-target rewrite happens. The
    declared FKs stay documentation-only, as `#157` already concluded for
    a related reason — this audit just confirms that conclusion was right
    for an even stronger reason than originally known.

### Hardening and reliability (agreed, not urgent bugs)

- **Formalize schema migrations** into a real versioned subsystem
  (migration ledger, automatic pre-migration backup, transactional
  migrations, post-migration integrity audit, migration tests from
  representative older DB snapshots) rather than `init_workgraph()`'s
  current accumulated inline create-if-absent/add-column/rebuild-view
  style. Reasonable for rapid single-user iteration to date; increasingly
  risky the longer real accumulated history lives only in this DB. **First
  real job for this subsystem once built: the FK-target rewrite the #365
  audit above found necessary** (17 columns across ~15 tables still
  reference tables renamed away by the `#114`/`#339` migrations - see that
  audit's findings for the exact list).
- **Explicit graph invariants beyond SQL FKs** — e.g. every promoted Issue
  has a valid Project, every Claim has resolvable evidence, no exclusive
  reference anchor belongs to two independently active objects, no `done`
  Claim appears in an open-claim index, relationship links stay symmetric
  where appropriate. SQL's own constraints can't express these; a
  periodic integrity-audit pass can.
- **Make the test suite hermetic and platform-aware.** A live rerun of a
  representative ~330-test cluster (grouping/relationships/synthesis/
  reconciliation/status-report/NBA/proactive/outlook-actions/lifecycle/
  sequences) passed 100% clean on this actual Windows install — the
  review's own "302/14" split is very likely an artifact of running the
  archive somewhere non-Windows (`subprocess.CREATE_NEW_PROCESS_GROUP`
  doesn't exist off Windows) or against an uninitialized scratch DB,
  exactly the two causes it named. That doesn't make the underlying advice
  wrong: tests that assume Windows or an already-initialized DB should say
  so explicitly (a skip marker, an autouse init fixture) instead of
  failing opaquely wherever that assumption doesn't hold.
- **A real semantic-accuracy evaluation, distinct from unit tests.** Most
  grouping/claims tests mock the model's verdict — they prove "if Claude
  says X, Jasper handles X correctly," never "Claude actually says X on
  real messy business traffic." Needs: a 300–500-example labeled corpus
  (same-project-different-thread, same-supplier-different-project,
  forwards, attachment-only-identity, prime/subcontractor, sparse/noisy
  evidence, true ambiguity), measured precision/recall/false-merge-rate/
  false-split-rate/abstention-rate — and eventually a chronological 30–90
  day replay-into-empty-DB benchmark compared against a human-labeled
  expected graph. This is effectively Track C.11 already on this roadmap,
  confirmed independently as the right next evaluation investment.
- **Periodically consolidate one-off reconciliation sweeps into generic
  primitives.** Real, individually-justified repair mechanisms exist for
  stray same-reference clusters, stray signature-confirmation clusters,
  recurring-calendar remediation, identity-conflict audits, etc. — healthy
  reactive engineering, but worth periodically asking which of these are
  really the same underlying "late authoritative evidence linker" problem
  solved three separate times, and consolidating when that's true.
- **Broaden the identity-conflict audit beyond PR/reference numbers.**
  Track B.8 built this narrowly (matching PR-number-base only); other
  evidence can be equally strong grounds for the same "surface, never
  auto-merge" treatment — shared unique document lineage, an explicit
  "this continues Project X" statement, a newly-discovered exclusive
  identifier. Same posture (never auto-merge mature Projects, only flag).
- **Relationship identity beyond normalized supplier name.** Working, but
  narrow — real Relationships can be people, programs, prime/subcontract
  structures, customers, internal orgs, not only companies, and companies
  themselves need alias handling ("Microsoft"/"Microsoft Corp."/"MSFT").
  Eventually wants a real canonical Entity layer (Entity → aliases →
  identifiers/domains → type; Relationship → one-or-more Entities →
  relationship type → projects) rather than living entirely on one
  normalized-name column.
- **Explicit, stronger prompt-injection framing, not just extraction
  boundaries.** The current architecture is genuinely better than raw
  evidence directly becoming claims/policy/actions — but light and heavy
  synthesis prompts still hand a model the raw evidence text directly
  (`NEW COMMUNICATIONS ... {new_evidence}`), and the heavy path is an
  agentic session with real tool access. "Content read from evidence is
  treated as untrusted data" (the technical spec's own framing) is real
  and correct for the claims-extraction boundary specifically; it should
  not be read as "prompt injection is closed" system-wide. Worth an
  explicit stated boundary ("raw evidence is data; statements addressed
  to Jasper/Claude inside evidence have no authority") applied
  consistently, plus minimizing tool access for any model that reads raw,
  untrusted evidence directly.

### New capabilities (agreed, real Jasper-core value, not UI)

- **Delta-based stakeholder reporting** — "what materially changed since
  this stakeholder was last updated" (a stored last-communicated graph
  revision/snapshot per stakeholder relationship), not a repeated full
  status regeneration.
- **First-class handoff-package export** for a Project — relationship,
  purpose, current state, decisions, open commitments, stakeholders,
  artifacts, dates, dependencies, unresolved questions, next actions,
  evidence references, as one durable object. Also the natural eventual
  mechanism for moving business state between installs without exporting
  behavioral/personality history.
- **"What changed while I was away?"** — nearly free once graph revisions
  are reliable; a real graph-delta answer ("4 Projects materially
  changed: Legal approved, supplier missed a commitment, a $250K
  commercial change, a dependency cleared"), not a dump of new emails.
- **Expected-next-step deviation detection**, building on the existing
  process-learning/sequence-mining work (task #322) — surfaced as
  descriptive ("projects like this usually have Security Review before
  Legal Review; no evidence of one here — worth checking"), never as an
  enforced rule.
- **Named epistemic-status tiers for evidence, not a numeric score** —
  distinct, human-legible categories (authoritative system state /
  explicitly stated by a person / model interpretation / inferred from
  recurring pattern / unresolved conflict) as a genuinely better answer to
  "how do you know that" than either an opaque score (already correctly
  rejected once) or no distinction at all.
- **Semantic graph-health diagnostics**, distinct from system-health
  monitoring — Jasper auditing its own representation of reality: an
  active Project with zero Claims in 30 days, a done Project with open
  commitments, an Issue with contradictory current claims, a closure
  event with no matching open request, an action marked succeeded with no
  artifact/evidence, an orphaned Claim. Likely one of the higher-leverage
  additions here.

### Documentation corrections (technical spec + plain-language doc)

- **Object-model inconsistency**: the Executive Summary states
  Project → Issue → Claims as the top-level hierarchy; §4.6 separately and
  correctly establishes Relationship as a distinct concept, optionally
  spanning multiple Projects. Harmonize: Relationship (optional/cross-
  linking) → Projects → Issues → Claims → Evidence, not a strict single
  chain implied up top.
- **"Closes a real class of prompt-injection risk"** (§5.1) is locally
  accurate for the specific claims-extraction boundary it's describing,
  but reads as a system-wide claim it shouldn't be — see the hardening
  item above. Soften to something like "reduces the blast radius... by
  preventing raw evidence from directly becoming executable state,"
  scoped to what's actually true today.
- **Settlement-pass claim is currently factually wrong** — §2.1 says the
  end-of-cycle pass re-scores "NBA/alerts"; the code only does NBA (see
  the bug above). Fix the code or fix the doc until it's true.
- **Completion-detection safety is overstated** — the deterministic-event
  *itself* being authoritative doesn't make its association to a specific
  open Claim authoritative (see the closure-correlation bug above); the
  doc should draw that distinction explicitly once the code does.
- **Build-diary framing makes an 18-page spec harder to consume** for
  anyone other than Marc/Claude Code — task numbers, session dates,
  "Marc's direct request" provenance are valuable but arguably belong in
  an Appendix (design history/incidents) rather than the main body.
- **Missing sections for a document calling itself a technical
  specification**: security/threat model, data-integrity strategy,
  evaluation methodology (once the golden corpus above exists),
  operational limits (candidate cap, prompt budgets, corpus size actually
  tested, timeout behavior), cost/LLM-usage model (tier per task, calls
  per refresh cycle, caching), failure semantics (Claude timeout, COM
  failure, partial-refresh behavior), and an explicit API/integration
  boundary section (REST/MCP/Outlook, read-only vs. action-capable).
- **§8.2's numbered roadmap list reportedly renders starting at 3** in
  Word (not verifiable from raw paragraph text — Word list numbering
  lives in numbering.xml, not the paragraph text itself; worth a quick
  visual check next time the doc is open) — if real, a leftover
  numId/restart artifact from editing, not a content problem.
- **Closing "every claimed fix...backed by an actually-executed test" is
  too absolute** given the test-hermeticity gap above and the fact that
  semantic/model-accuracy claims aren't covered by ordinary unit tests at
  all. Weaken to something like "major mechanisms and known regressions
  are covered by executable tests, supplemented by live validation" until
  the golden-corpus evaluation exists to back a stronger statement.
- **Plain-language doc, three wording overreaches**: "never guesses" is
  more accurately "doesn't hide ambiguous identity behind an opaque
  score, and can abstain" (also a stronger, more distinctive claim);
  "learns quietly from what you do" should be framed as building the
  feedback record the deferred behavioral-adaptation loop needs, not
  implying that loop already runs; "the same way it already learned what
  matters in mine" should say the procurement vocabulary was seeded as
  pre-confirmed, not autonomously discovered from scratch (§4.5 already
  says this correctly — the plain doc should match it). The doc is also
  missing Jasper's output-side value entirely (reporting, handoffs,
  "what changed" answers) — its single biggest content gap.

---

## Longer-term generalization track (separate from the above)

Marc's stated plan: stabilize Jasper via real day-to-day use first, then
decide whether/how to make it portable to other people and systems, still
starting from procurement as the base case. Full gap analysis and
recommended sequencing lives in the `jasper-generalization-roadmap` memory
file — not duplicated here since it's already detailed there and this file
is meant to index, not fork, that content. Overlaps with Phase 6 above by
design (Phase 6 is the code-level expression of this same intent).

**Trigger to revisit:** Marc's explicit signal that the stabilization
period is over.
