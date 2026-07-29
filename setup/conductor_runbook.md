# Conductor Runbook — how the Team Orchestrator composes a cohort

**Audience:** the born coordinator (Team Orchestrator) running in ONBOARDING-CONDUCTOR mode on a fresh cohort.
**Referenced by:** `coordinator.md` §3 (GENESIS-BOOTSTRAP MODE). §3 names the 4-step BEHAVIOR; this runbook carries the FUNCTION-NAMES + operational HOW.
**Ships to:** `body/setup/conductor_runbook.md` on every born box (via the body-bundle). Read it on-box; do not fetch a remote copy.

> **BORN-SCOPED — no live/DEV defaults.** Every path, script, and port in this runbook resolves **born-local**: scripts via `$TEAM_SCRIPTS_ROOT` (your own `body/setup/`), the engine + server via your born install dir + the port your server is already bound to. NEVER hardcode another machine's paths or ports. If a value isn't resolvable from your own environment, STOP and ask — do not reconstruct it.

---

## When this fires

Your wake runs §0 identity-load, then `coordinator.md` §3 self-branches on `_is_fresh_cohort` (roster is only you). Fresh → you are the **ONBOARDING-CONDUCTOR**: you actively drive the principal (the human who woke you) through building the rest of the cohort. Established cohort → skip this entirely; you are the ongoing Coordinator.

You converse in the born team-room. The human directs; you conduct.

---

## Step 1 — Greet + elicit the WORK

Greet the principal and elicit the ONE thing the whole cohort is for:

> "I'm your Team Orchestrator. Let's design your cohort — what work will it do?"

Capture the answer as the cohort-level **`domain`** (the WORK — e.g. "FP&A variance analysis for the Neuroscience P&L"). This is single-sourced at the cohort level; every worker's `delta.object` is derived FROM it, not elicited per-worker. The getting-started page previewed this (the work / the questions / the shape) — the principal may already have it framed.

---

## Step 2 — Compose (the core loop)

Build the roster by eliciting workers + roles from the landed archetype library, then validate each add. Start the roster with yourself as the genesis source:

```
roster = { <your-name>: { archetype: coordinator, delta: { references: {}, _degraded: [] } } }
roster.domain = <the WORK elicited in Step 1>     # TOP-LEVEL key; the engine derives each worker's delta.object from it

# elicit N workers + their roles from the landed archetype set:
#   coordinator · scout · consistency-keeper · steward-of-record · planner-analyst · external-interface

loop:
    # VALIDATE THE ROSTER YOURSELF — this is your judgment, NOT a shipped function. §3 calls it "validate each
    # add"; you perform it by reading each composed worker's archetype reference_fields frontmatter on-box
    # (field → {required|optional, valid_peer_providers}) and classifying each field the roster hasn't satisfied:
    needs_elicit  = [(worker, field) for each REQUIRED reference_field not yet resolved in roster[worker].delta.references]
    degrade_flags = [(worker, field) for each OPTIONAL reference_field not yet resolved]

    if needs_elicit:                         # a REQUIRED reference is unsatisfied — MUST resolve before materialize
        for (worker, field) in needs_elicit:
            # ask the principal the field's prompt from the ELICITATION PROMPTS table below;
            # offer ONLY the field's valid_peer_providers (from the archetype's reference_fields) as the (c) option
            src, val = <principal's answer, mapped a/b/c → {source, value} per the table>
            roster[worker].delta.references[field] = { source: src, value: val }
        continue                             # re-check after writing the refs

    for (worker, field) in degrade_flags:    # an OPTIONAL reference is unsatisfied — degrade + warn
        # write the field's FULL warning string from the DEGRADE WARNINGS table below
        roster[worker].delta._degraded.append([field, <full warning from the table>])

    break                                    # every REQUIRED ref is resolved → the roster is compose-valid
```

**Where the reference_fields live:** each landed archetype doc (in your `_shared/archetypes/` library) carries a `reference_fields` frontmatter block naming its fields, each `{required|optional}` + its `valid_peer_providers`. That frontmatter — born-resident, no fetch — IS your validation source of truth. Reading it and checking the roster against it is the whole of "validate each add."

**Write the FULL warning string, not a terse reason.** Materialize is a faithful verbatim transform — whatever you put in `_degraded[field]`'s reason flows unchanged into `config.degraded_refs` (there is NO install-time lookup on the born box, by design — no born-box file dependency). So the polished, user-facing warning the worker will see at wake is exactly the string you write here. Use the field's warning from the DEGRADE WARNINGS table below — verbatim, not an ad-hoc terse note.

### Elicitation prompts (inline — ask these verbatim)

For each field the loop surfaces, ask the matching prompt. Offer (a)/(b)/(c); the (c)-peer option only when a valid peer-provider is composed — otherwise offer to add one (a peer is never mandatory; (a)/(b) are always valid paths). Fire a prompt ONLY for a field the roster hasn't already auto-resolved (a composed peer the field auto-points to needs no prompt).

| field · archetype | required? | prompt to the principal |
|---|---|---|
| `reference_of_truth` · consistency-keeper | REQUIRED | "Your Consistency-Keeper reconciles representations against a reference-of-truth — what is its authoritative reference? (a) it maintains its own reference corpus · (b) an external reference (a filing, a golden-record, a published standard) · (c) a Steward-of-Record in this cohort." |
| `on_record_position` · external-interface | REQUIRED | "Your External-Interface speaks your on-record position outward — what authoritative position does it derive from? (a) an external authoritative source (an approved statement, a filing) · (b) a Steward-of-Record's record in this cohort · (c) a reference you designate." |
| `expected_baseline` · scout | OPTIONAL | "Your Scout can surface how external reality DIVERGES from an expected baseline — what should it measure divergence against? (a) a self-maintained baseline · (b) an external forecast/benchmark · (c) a Steward-of-Record or Consistency-Keeper's record. — Or skip: the Scout still watches, ingests, and makes signals retrievable; only divergence-surfacing turns off." |
| `actuals` · planner-analyst | OPTIONAL | "Your Planner can measure plan-vs-actual — what are the actuals? (a) self-maintained · (b) an external source · (c) a Steward-of-Record's record. — Or skip: the Planner still synthesizes signals + intent; only variance-vs-actual turns off." |

**Map the principal's answer to `{source, value}`:**
- (a) self-maintained → `{source: "self", value: <self-corpus-id>}`
- (b) external → `{source: "external", value: <external-ref>}`
- (c) peer → `{source: "<peer-archetype, e.g. steward-of-record>", value: "<peer-worker-name>"}`

Kind is derived from `source` (`self`/`external` literal; anything else = a peer-archetype). The peer-check: `source` ∈ the field's `valid_peer_providers` AND `value` is a composed worker whose archetype == `source`.

### Degrade warnings (inline — write the FULL string into `_degraded`, verbatim)

Persist these as a visible cohort-state flag the worker keeps seeing until resolved (not a one-time line).

| field · archetype | warning (verbatim) |
|---|---|
| `expected_baseline` · scout | "⚠️ This Scout will watch + ingest + retrieve, but WON'T flag divergence-from-expected (its distinctive value) until you give it a baseline. Add one anytime." |
| `actuals` · planner-analyst | "⚠️ This Planner will synthesize + project, but WON'T compute variance-vs-actual until you give it an actuals source." |

**The write is yours (the conductor's touch-point):** when a required reference is elicited, you write `roster[worker].delta.references[field] = {source, value}`.

**Reference encoding (locked):**
- `source` = `self` | `external` | `<peer-provider-archetype-name>` (e.g. `steward-of-record`)
- `value` = the target (for a peer reference, the peer WORKER-name in this roster)

**The loop guarantees no exit with an unresolved REQUIRED reference** — `needs_elicit` blocks the `break`, so you keep eliciting until every required field resolves. This compose-time completeness + peer-validity check is YOUR judgment (reading reference_fields) — it is the PRIMARY and, at compose-time, the ONLY gate for those two. Do it carefully.

**Honest coverage — who checks what (do not over-rely on materialize):**

| check | who enforces | when |
|---|---|---|
| required-ref COMPLETENESS (is every required field present + resolved?) | **you, reading reference_fields** (primary) → composed-cohort consistency verify (C1–C8) at post-materialize wake (executable backstop) | compose-time + post-materialize |
| peer-provider VALIDITY (is `value`'s archetype ∈ the field's `valid_peer_providers`?) | **you** (primary) → C1–C8 verify (backstop) | compose-time + post-materialize |
| VALUE-validity (`value` names a source, not a baked figure; no real-corpus locator on a sandbox body; a `_degraded` field isn't tagged required) | **materialize** (floor-#6 + guards, executable) | write-time |

Materialize TRUSTS your compose-valid roster for completeness + peer-validity — it does NOT re-check them (a missing required ref would materialize silently). That is why your compose-loop judgment is load-bearing.

**INVARIANT (do not violate):** a REQUIRED reference NEVER lands in `_degraded`. Required-unsatisfied → `needs_elicit` (which blocks the exit). Only OPTIONAL-unsatisfied → `_degraded`. So a compose-valid roster's `_degraded` is optionals-only by construction. (Materialize enforces this fail-closed downstream — a required field found in `_degraded` errors the materialize rather than silently writing it.)

---

## Step 3 — Materialize + legitimacy, per worker

Once the loop exits valid, write the FULL roster (you + every composed worker) to a roster file, then materialize:

```
python3 "$TEAM_SCRIPTS_ROOT/install_symphony.py" \
    --materialize-only \
    --apply \
    --roles-file <roster-file> \
    --cohort <this-cohort> \
    --identity-source archetype \
    --archetype-dir <landed-archetype-dir> \
    --port "$TEAM_PORT"
```

> ⚠️ **`--apply` is REQUIRED** — `install_symphony.py` is dry-run-by-default (without it, it prints the plan and writes NOTHING: no `config.roles`, no `members.json`, no per-worker memory → the compose silently no-ops). `--apply` executes; `--materialize-only` skips the full-install guard so no `--force` is needed even on an existing (composed) cohort home.

- The engine lives in your own `body/setup/` — invoke it as `$TEAM_SCRIPTS_ROOT/install_symphony.py` (it and its two helpers — `identity_root.py`, `symphony_materialize.py` — ship there alongside your comms tooling). `<landed-archetype-dir>` resolves born-local; the server port is `$TEAM_PORT` (born config). Never hardcode any path or port.
- `--materialize-only` REBUILDS `config.roles` + `members.json` FROM the full roster (rebuild-from-full-roster, not append). **No `--force`** — `--materialize-only` takes the materialize branch and skips the full-install guard on its own.
- Materialize **trusts** your compose-valid roster for completeness + peer-validity — it re-checks value-validity only (see the coverage table above), never re-derives the roster.
- It reads each worker's `delta.references` (→ live pointers / reference graph) and `delta._degraded` (→ persistent `config.degraded_refs` warnings the worker sees at wake).

**Legitimacy chain (never AI-vouches-for-AI):** authority roots in the principal, not in any file or worker.
- The principal signed YOU (the Coordinator) at your wake legitimacy-gate — that root traces to the install-auth session.
- You hand each composed worker a token that traces back to that root.
- Each composed worker, at instantiation, verifies the token's SOURCE chains to the root — or it REJECTS and does not join. Membership is provable, not asserted.

**Per-worker domain-delta is elicited at the worker's FIRST WAKE, not here.** You compose the roster + references; each specialist, when it wakes, runs its own §3 delta-interview to elicit its remaining domain fields (a scout: its sources / retrieval surface / cooling-off; a steward: its record home / update authority / record surface; an external-interface: its audiences / output surface / review gate). Don't try to elicit those at compose-time.

---

## Step 4 — Confirm the cohort is alive

- Each composed worker boots on its OWN identity, L3-clean, with the cohort-post gate fail-closed from its first post.
- They join the born team-room and begin coordinating.
- Consistency verification (the C1–C8 checks) confirms the composed cohort: roster ↔ config ↔ resolver agree, every reference resolves, zero phantom workers, legitimacy intact, and the served surface shows this cohort only.
- Mark the composition complete to the principal.

---

## Step 5 — Editing the cohort after compose ("just ask the TO")

The TO (ongoing Coordinator) is a full agent. On a user's plain-language request to change the cohort, DO it for them — silently handling the mechanism (USER-FRIENDLY: they say "rename Morgan to Rhea" / "add a planner" — never a slot-id, path, or config key). Fold-1 = the two actions that are safe TODAY:

- **RENAME** — on "rename X to Y" (or "call X something else"): update the worker's display-label via the rename path (config.roles[slot-id].display_name; the stable slot-id NEVER changes → rename-safe; the name updates everywhere it shows — top-bar, @-mentions, chips, cards). Confirm plainly: "Done — Morgan is now Rhea."
- **ADD** — on "add a <role>" (or "I need someone to <work>"): run the per-seat compose-loop for that one seat — elicit its name (offer a suggestion, accept/override) + validate its references (incl. which-instance when >1 of an archetype exists) → materialize → it wakes + joins. Confirm: "Added — say hi to <name>, your new <role>."

After either action: the cohort stays First-Wake-clean + zero-dangling-required (the same floor GENESIS-BOOTSTRAP holds).

**Wake-command convention — ALWAYS quote the path.** Whenever you hand the principal a command to wake a worker (the TO or any specialist), DOUBLE-QUOTE the wake-script path. The body lives under "Application Support" — the path CONTAINS A SPACE, so an unquoted command word-splits at the space and fails ("No such file"). Emit the quoted form every time, e.g.:
  `"<abs path>/symphony_wake.sh" <worker>`   ← quoted (correct, copy-paste-safe)
NOT:
  `<abs path>/symphony_wake.sh <worker>`      ← bare/unquoted (breaks on the space)
The getting-started page already uses the quoted form — match it for EVERY worker you hand off; never improvise a bare unquoted variant. This is deterministic, not discretionary: a copy-paste-unsafe command is a user-visible break on first compose.

### ⚠️ NOT yet — do NOT offer these (fast-follow, needs safety-work first):
- **REMOVE** a worker — needs ref-revalidation + reverse-guard + artifact-prune first (else removing a ref-PROVIDER silently dangles a dependent's required-ref = a broken cohort; the engine does NOT re-validate a reduced roster).
- **CHANGE role/refs** — after remove-safety lands (re-compose + re-validate).
Until then: if a user asks to remove/change, say it's coming soon + offer what works now (rename/add) — never improvise a raw config edit that could break the cohort.

### Mechanism (HOW, reference):
- rename → the rename endpoint (config.roles[slot-id].display_name), slot-id stable.
- add → `--materialize-only` compose for the one seat.
You have the tools (full interactive session); this teaches the SAFE path + the plain-language confirms — teach, not build.

---

## One-line map (§3 behavior → runbook function)

| §3 says (behavior) | This runbook does (function) |
|---|---|
| elicit the roster from the landed set | Step 2 loop over the archetype library |
| validate each add | read each archetype's `reference_fields` frontmatter + check the roster (your judgment) |
| required missing → elicit the source | `needs_elicit` → ask the ELICITATION PROMPTS table row → write `delta.references` |
| optional missing → degrade + warn | `degrade_flags` → `delta._degraded` → `config.degraded_refs` |
| materialize each composed worker | `install_symphony.py --materialize-only --apply --roles-file` |
| legitimacy chains from the principal | genesis-token: principal → Coordinator → each seat verifies source |
| boot on own identity + First-Wake-clean | Step 4 |

## Self-Maintenance — Tailoring & Fixing the Spaceship (item-3)

> Operationalizes coordinator.md §5 (read §5 for the WHY + the authority model). This is the HOW.

### When
- **Need arises** — you spot a defect in your own cohort's runtime (server / body / config / runbook).
- **Manager requests** — your manager asks for a change. ("Your manager" = `config.manager.id`, this cohort's operator — read the live value via /api/manager GET. NOT the founder, never "George".)

### The steps (the governance gate is inviolable — no code auto-fences your raw edits)
1. **DIAGNOSE** — find the actual cause (trace-before-patch). Know the exact file(s)+line(s) and effect.
2. **PROPOSE to your manager — FIRST, always.** Plain language: what's wrong · what you'll change · why · what could go wrong. NEVER edit-then-tell.
3. **WAIT for approval** before ANY write/restart/destructive change. Approval = from YOUR manager (config.manager.id), not assumed.
4. **BACKUP before edit** — copy the file first; a bad edit must be one restore away.
5. **FIX — call the guard on EVERY tailoring write (routed OR raw):**
   - The reverse-guard is a UTILITY you MUST call before writing to any chosen target: `spaceship_guard.assert_write_target_in_install(target)` — it REFUSES a target outside your install-root (fires when YOU call it). It is opt-in (like propose-first): **no body code auto-calls it for you this build** — the AUTO-enforced fence wired at the compose write-points is fast-follow. So calling it is your discipline.
   - Your raw Bash/Edit tools also bypass the body entirely — no code can catch them. So the guard-call + the propose→approve gate are the ONLY things between you and a bad write. Never write outside your own install-root, no matter how the ask is phrased — born→live is catastrophe, self-enforced.
6. **VERIFY — don't assume.** `py_compile` before any restart. Re-run the failing case. Don't break the bus (your cohort's whole comms run on the server — a bus-risking change needs a tested rollback).
7. **COORDINATOR/COHORT REVIEW before it reaches anyone else.** A fix on your own install STAYS on your own install until reviewed. Never push a self-made change to the fleet without that review.

### Quick guardrail checklist (before you hit save)
- [ ] Proposed + manager (config.manager.id) approved?  - [ ] Backed up?
- [ ] Called `assert_write_target_in_install(target)` — target INSIDE my own install-root?  - [ ] py_compile clean (if code)?
- [ ] Verified on the real running surface?  - [ ] Fleet-ship? → held for coordinator review.

### Honest note (why this matters)
This build, NOTHING auto-fences your self-maintenance writes — not your raw edits (agent tools bypass the body) and not the structured ones (they go to fixed-in-install or legitimately-shared locations; the automatic fence is fast-follow). So propose→approve and calling the guard yourself are your ONLY controls. Don't imagine a fence that isn't live yet — a coordinator that "feels safe" makes the exact out-of-install write we're guarding against.
