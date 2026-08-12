# Canonical Entity layer — design (task #379)

**Status: DESIGN ONLY. Nothing in this document has been built. No code or
schema was changed to produce it.** Written for Marc's review before any
build work starts, per the standing rule that roadmap-scale work needs an
explicit go-ahead at the time.

Roadmap origin: `docs/design/ROADMAP.md` line 491, "Relationship identity
beyond normalized supplier name — ... Eventually wants a real canonical
Entity layer (Entity → aliases → identifiers/domains → type; Relationship →
one-or-more Entities → relationship type → projects) rather than living
entirely on one normalized-name column."

---

## 1. What exists today (read, not assumed)

### 1.1 The `relationships` table and its one identity mechanism

`workgraph_store.py:1904-1921` — the real schema:

```
relationships       (id, name, status, created_ts, updated_ts)  + normalized_name (ALTER, #343)
project_relationships (project_id, relationship_id, reason, linked_ts)  PK(project_id, relationship_id)
```

There is no `relationship_type`, no member table, no alias table, no
identifier table. Identity is entirely `normalized_name`, with a **partial
unique index** (`workgraph_store.py:2007-2010`,
`WHERE normalized_name != ''` so blank names never collide-merge).

Every read/write path in the whole codebase:

| Function | File:line | Role |
|---|---|---|
| `get_or_create_relationship_by_name` | `workgraph_store.py:7383` | the only writer of `relationships` rows; dedupes on `normalize_company_name(name)` |
| `get_relationship` | `workgraph_store.py:7418` | by id |
| `link_project_to_relationship` | `workgraph_store.py:7428` | `INSERT OR IGNORE`, idempotent |
| `list_relationships_for_project` | `workgraph_store.py:7446` | used by `workgraph_status_report.py:99,337` |
| `list_projects_for_relationship` | `workgraph_store.py:7461` | used by both sweeps + the review listing |
| `list_relationships` | `workgraph_store.py:7476` | `status='active'` only |
| inline backfill + consolidation | `workgraph_store.py:1940-2010` | task #343, runs inside `init_workgraph()` on **every** call |

Callers outside the store: only `workgraph_relationships.py` (both
producers + the review listing) and `workgraph_status_report.py`
(`_vendor_name_for_project` prefers a Relationship name over a raw
party-company rollup). Surfaces:
`server_lean.py:3037` `/api/workgraph/relationship-audit` and
`jasper_mcp_server.py:342` `jasper_relationship_audit` — both read-only,
pull-based, no confirm/reject queue (deliberate, per task #304 item #2).

Notably **absent**: nothing ever updates a relationship's `name` or
`status`, and nothing deletes one except #343's consolidation sweep.

### 1.2 The two discovery producers

- `run_relationship_sweep` (`workgraph_relationships.py:79`) — reads
  `work_object_relationships` rows with `relationship_type='rejected'`
  (real pipeline2 candidates a judgment said are *different projects*),
  resolves both sides to parent projects, derives a shared supplier name
  via `_shared_supplier_name` (line 43), links both projects.
- `run_supplier_entity_sweep` (`workgraph_relationships.py:223`, task #342)
  — groups the whole corpus's `data_point_values` rows for
  `dp-fasttrack-supplier` by **already-normalized** value, links every
  project sharing one, using `_display_name_for_normalized_supplier`
  (line 201) to recover a real spelling.

Both go through `get_or_create_relationship_by_name`, so a Relationship is
indistinguishable once created — the design below must preserve that.

Both refuse to fabricate a name (return `None` rather than guess). That
posture carries directly into this design.

### 1.3 The real limit of today's identity: `normalize_company_name`

`workgraph_signals.py:462-475` is the entire mechanism:

```python
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co)\.?\s*$", re.I)
return _COMPANY_SUFFIX_RE.sub("", name.lower().strip()).strip()
```

Lowercase + strip **one** trailing legal-form suffix, anchored at end of
string. Probed directly (regex run standalone, not guessed):

| Input | Output | Same as bare name? |
|---|---|---|
| `Microsoft` | `microsoft` | — |
| `Microsoft Corp.` | `microsoft` | ✅ yes |
| `MICROSOFT CORP` | `microsoft` | ✅ yes |
| `Microsoft Corporation` | `microsoft` | ✅ yes |
| **`Microsoft, Inc`** | **`microsoft,`** | ❌ **NO** |
| **`Fullstory, Inc`** | **`fullstory,`** | ❌ **NO** |
| **`Authenticx, LLC`** | **`authenticx,`** | ❌ **NO** |
| `Sodalis Inc Ltd` | `sodalis inc` | ❌ no |
| `Microsoft Corp (US)` | `microsoft corp (us)` | ❌ no |
| `The Microsoft Corporation` | `the microsoft` | ❌ no |
| `Deloitte Consulting LLP` | `deloitte consulting llp` | ❌ no (`LLP` absent from the list) |
| `Novo Nordisk A/S` | `novo nordisk a/s` | ❌ no (`A/S`/`GmbH`/`PLC`/`AG`/`BV`/`Pty` all absent) |
| `MSFT` | `msft` | ❌ no (ticker — no mechanism can do this deterministically) |
| `Microsoft Ireland Operations Ltd` | `microsoft ireland operations` | ❌ no (subsidiary) |

So the answer to "what does a case that today's exact-normalized-name
matching would NOT catch look like" is not hypothetical, and the sharpest
example is already **in this codebase's own confirmed real data**:
`workgraph_signals.py:398-402` documents `Supplier Name: Fullstory, Inc` as
a real observed ContractPodAI value, while `workgraph_parties.py:98-102`
`_company_from_domain('fullstory.com')` yields `Fullstory`. Those two real
values for one real vendor **do not normalize equal** — a comma is enough
to break it.

That is a live consequence beyond naming: in
`workgraph_projects._matched_data_points` (line 857-864) the `"supplier"`
point is a set intersection of exactly these normalized values, so today
`{'fullstory'}` vs `{'fullstory,'}` awards **no** supplier point. See §7 —
that is precisely why this design does *not* touch that function.

### 1.4 The identity paths this must interoperate with (four, currently disconnected)

| Path | Key used | Normalized? |
|---|---|---|
| `relationships.normalized_name` | `normalize_company_name(name)` | yes (weak) |
| `data_point_values` `dp-fasttrack-supplier` (`workgraph_projects.py:580-584`) | `normalize_company_name` of `external_orgs` + `system_party` | yes (weak) |
| `parties.company` (`workgraph_store.py:731`) via `list_issues_for_company` (`:5948`) | **raw string, `= ? COLLATE NOCASE`** | **no** |
| `identity_anchors` type `'company'` (`workgraph_identity.py:120-124`) | `company.strip().lower()` | **no** |

Consequences worth naming plainly, because they are the actual business
pain behind this task:

- **The Supplier Dashboard and Aristotle already split on aliases.**
  `workgraph_suppliers.py` groups by `parties.company`;
  `workgraph_aristotle._group_keys_for_issue` / `_issues_to_check`
  (`:98-110`, `:65-73`) group via `list_issues_for_company`, an exact
  `COLLATE NOCASE` compare. `"Microsoft"` and `"Microsoft Corp."` are two
  different suppliers there **today**, even though the relationship layer
  correctly merges them. The Entity layer's largest immediate payoff is
  fixing this, not fixing Relationship naming.
- **`workgraph_lessons.situation_key`** (`:62-69`) is
  `f"category:{cat}|company:{comp}"` on the raw lowercased company string,
  with `UNIQUE(situation_key, outcome)` and accumulated
  `trust_score`/`hit_count` (`workgraph_store.py:2018-2031`). Re-keying it
  on an entity id would **orphan accumulated trust history**. See §6.5.
- `parties` has no company-level record at all — `company` is a free-text
  column per person, best-effort, and `classify_affiliation` explicitly
  refuses to guess one for automated senders.
- `identity_constraints` (`workgraph_store.py:1346-1361`) already declares
  `confirm_person_alias`, `prevent_person_merge`, `must_link` as
  schema-ready-with-no-producer. Relevant to §5.

### 1.5 Existing test coverage this design must keep meaningful

`tests/test_workgraph_relationships.py` — 13 tests. Sweep decision logic
(links, reuse-across-a-third-project, four honest skips, non-rejected rows
ignored, daily gate) plus the review listing and the four #342
supplier-entity-sweep tests, including
`test_supplier_entity_sweep_links_projects_that_never_became_pipeline2_candidates`
which asserts `rels[0]["name"].lower() == "microsoft"` for
`Microsoft` + `MICROSOFT CORP`.

`tests/test_workgraph_store.py:3153-3220` — `dedups_case_insensitive`,
`dedups_corporate_suffix` (`Sodalis` / `Sodalis Inc` / `SODALIS LLC` → one
row), `blank_names_never_merge`,
`test_init_workgraph_consolidates_pre_existing_duplicate_relationships`,
`link_project_to_relationship_is_idempotent`.

`tests/test_workgraph_signals.py:285-290` — `normalize_company_name`'s
exact current output for four inputs.

**All of these stay green under this design** (§8), because
`get_or_create_relationship_by_name` keeps its signature, return semantics,
and dedupe key, and `normalize_company_name` is not modified.

---

## 2. The Entity layer: table shapes

Four new tables, one nullable-column addition to `relationships`. All
additive; no existing table is rewritten.

### 2.1 `entities` — the durable identity

```
entities (
    id            TEXT PRIMARY KEY,        -- 'ent-<uuid4 hex[:12]>', same shape as 'rel-...'
    entity_type   TEXT NOT NULL,           -- see 2.2
    display_name  TEXT NOT NULL,           -- a real observed spelling, never fabricated
    entity_key    TEXT NOT NULL,           -- deterministic key, see 5.2 (NOT unique - see 6.3)
    status        TEXT NOT NULL DEFAULT 'active',
    created_by    TEXT NOT NULL,           -- 'backfill' | 'sweep' | 'human'
    created_ts    REAL NOT NULL,
    updated_ts    REAL NOT NULL
)
INDEX (entity_key), INDEX (entity_type)
```

What identifies an Entity: **the opaque `id`, and only the `id`.** That is
the whole point — `entity_key` is a *resolution aid* that can change when
the normalizer improves, and `display_name` is a *label*. Neither is
identity. This is the specific property `relationships.normalized_name`
does not have today: it is simultaneously the merge key, the identity, and
(via `name`) the label, so improving normalization means rewriting
identity.

Deliberately **no** `CHECK` constraint on `entity_type`/`status`. This file's
own history shows CHECK widening requires a full table rebuild
(`docs/design/SCHEMA_FK_DEBT.md:36-48`, tasks #44/#55); a vocabulary that
is expected to grow (programs, internal orgs) should not be pinned by a
constraint that costs a rebuild to extend. Vocabulary lives in module
constants, as `workgraph_classify.TOPIC_RULES` already does for categories.

No `REFERENCES` clauses to `issues`/`projects` — per
`docs/design/SCHEMA_FK_DEBT.md`, declared FKs are unenforced documentation,
and 17 columns already point at tables renamed away. New tables should not
add to that pile.

### 2.2 `entity_type` — and one correction to the roadmap's own list

The roadmap names "company/person/program/internal-org/customer".
**Recommendation: drop `customer` from `entity_type`.** "Customer" is not a
kind of thing, it is a *role in a relationship* — the same company can be a
supplier on one project and a customer on another, and an entity type
cannot represent that without duplicating the entity. It belongs on
`relationship_entities.role` (§3).

| `entity_type` | Real producer available today? |
|---|---|
| `company` | **Yes** — `parties.company`, `positive_vocabulary.system_party`, `contractpodai_requests.supplier_name`, `relationships.name` |
| `person` | **Yes, already** — the `parties` table *is* the person record. See §2.4: a person Entity binds to a party, it does not replace it. |
| `program` | **No.** No extractor exists. Human-created only. (`dp-fasttrack-subject-entity` recurring subject cores are a *hint*, never grounds for auto-creation — a repeated subject core is a topic, not a durable program.) |
| `internal_org` | **No.** Confirmed absent: grep for `business_unit`/`department` across the repo returns zero non-test hits. `parties.affiliation='internal'` says "at Lilly", nothing finer. Human-created only. |

Stating that honestly matters more than shipping five populated types:
three of five have no producer, so the Entity layer starts as a **company +
person** layer with schema room for the other two, exactly the
"build what has a real producer" discipline `identity_constraints`' own
comment (`workgraph_store.py:1338-1345`) already applies.

### 2.3 `entity_aliases` — the spellings

```
entity_aliases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id    TEXT NOT NULL,
    alias        TEXT NOT NULL,        -- the real observed spelling, verbatim
    alias_key    TEXT NOT NULL,        -- entity_key(alias)
    source       TEXT NOT NULL,        -- 'relationship_name' | 'party_company' | 'system_party'
                                       -- | 'cpai_supplier_name' | 'human'
    confidence   TEXT NOT NULL,        -- 'H' (human/deterministic) | 'M' (heuristic)
    first_seen_ts REAL NOT NULL,
    UNIQUE(entity_id, alias_key)
)
INDEX (alias_key)
```

Every alias row is a real observed string with provenance. Nothing is
generated by permuting a name. `alias_key` is what resolution looks up
(§5).

### 2.4 `entity_identifiers` — what is *actually* extractable here

The roadmap says "identifiers/domains (email domain, DUNS, ticker)". Being
concrete about this system's real evidence rather than a generic wishlist:

```
entity_identifiers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL,
    identifier_type TEXT NOT NULL,
    value           TEXT NOT NULL,     -- normalized per type (domains lowercased)
    source          TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    created_ts      REAL NOT NULL,
    UNIQUE(identifier_type, value, entity_id)
)
INDEX (identifier_type, value)
```

| `identifier_type` | Real source in THIS system | Extractable today? |
|---|---|---|
| `email_domain` | `parties.primary_email` right of `@`, excluding `is_automated_sender` addresses and `lists.lilly.com` | **Yes — the single highest-value identifier.** This is the one durable, machine-verifiable fact tying a person to a company. |
| `party_id` | `parties.id` | **Yes.** How a `person` Entity binds to the existing party record (§2.5). |
| `url_host` | `contractpodai_requests.contractpod_url` host, `link_extraction` output | Yes, but it identifies the *system*, not the counterparty. Low value; include only if a real case appears. |
| `vendor_number` | An Ariba/SAP supplier number in a labeled body field | **No extractor today.** `extract_labeled_party_field`'s label vocabulary (`workgraph_signals.py:411`) is where one would go. Schema-ready, no producer. |
| `duns`, `lei`, `ticker` | Nowhere. No feed, no directory lookup (`workgraph_parties.py:3-5` confirms the only Graph identity tool is `get_me`) | **No. Human-entered only, or never.** Do not build extractors for these on speculation. |

The honest summary: **email domain is the only identifier with real
automatic reach.** Everything else is human-entered or absent. A design
that pretends otherwise would produce four empty columns.

Important guard, mirroring `DOMAIN_OVERRIDES` (`workgraph_parties.py:85-88`):
`lilly.com` and `network.lilly.com` must **never** be attached as an
`email_domain` identifier to an external company entity — `network.lilly.com`
is confirmed-issued to suppliers and contingent workers, so it would
cross-link every supplier onto one entity. Same for anything
`is_automated_sender`. A shared-domain signal is also **evidence for a
proposal, never an auto-merge** (§5.3): two suppliers can share
`gmail.com`-class or reseller domains.

### 2.5 A `person` Entity does not duplicate `parties`

`parties` stays the authoritative person record: `primary_email` UNIQUE,
`affiliation` with confidence/source, `correct_party_affiliation`'s
permanent human override (`workgraph_store.py:5846`), and
`_resolve_bare_name`'s abstain-on-ambiguity discipline
(`workgraph_parties.py:175-200`).

A `person` Entity is a **thin canonical wrapper** created only when a
person needs to be a Relationship member (a program owner, a named prime
contact), bound to its party by `entity_identifiers(identifier_type='party_id')`.
It never carries affiliation, never re-guesses, and never becomes a second
place a person's identity is decided. Concretely: no `person` Entity is
created during the backfill at all (§6) — they appear only when a human or
a future producer actually needs one.

That is the answer to "interoperate with or absorb the existing
party/company resolution path": **person identity is absorbed by reference
(parties stays canon); company identity is absorbed by resolution at read
time (parties.company stays raw evidence, never rewritten).**

---

## 3. How a `Relationship` row changes

### 3.1 Schema delta

```
ALTER TABLE relationships ADD COLUMN relationship_type   TEXT      -- nullable
ALTER TABLE relationships ADD COLUMN primary_entity_id   TEXT      -- nullable

relationship_entities (
    relationship_id TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    role            TEXT NOT NULL,     -- see 3.3
    source          TEXT NOT NULL,     -- 'backfill' | 'sweep' | 'human'
    added_ts        REAL NOT NULL,
    PRIMARY KEY (relationship_id, entity_id, role)
)
INDEX (entity_id)
```

Two nullable `ADD COLUMN`s in the existing `try/except sqlite3.OperationalError`
idiom (`workgraph_store.py:1940-1943`) — no rebuild, no CHECK.

`relationships.name`, `normalized_name`, `status` and the partial unique
index all **stay exactly as they are**, and `get_or_create_relationship_by_name`
keeps writing them. That is deliberate: it is what keeps §1.5's tests
meaningful, keeps `workgraph_status_report._vendor_name_for_project`
working unchanged, and means a half-migrated database is still fully
functional.

### 3.2 So: "one-or-more Entities → relationship type → projects"?

Yes, but as a **layer over** the existing edge, not a replacement:

```
Entity(s) ──relationship_entities(role)──> Relationship ──project_relationships──> Project(s)
```

`project_relationships` is untouched: same PK, same rows, no data movement.
The Relationship remains the thing projects attach to; what changes is that
it now knows *which canonical entities it is about* and *what kind of
relationship it is*.

### 3.3 What `relationship_type` and `role` actually mean

`relationship_type` (on the Relationship — the shape of the arrangement):

| value | meaning | backfill / producer |
|---|---|---|
| `supplier` | Lilly buys from one counterparty. The only shape either existing sweep can produce. | **default for every backfilled row** |
| `prime_subcontract` | two-or-more external companies in a prime/sub structure Marc is a party to | human, or proposed from a `cross_mention` signal (§3.4 ex. 2) |
| `customer` | Lilly is the supplying side | human only |
| `partner` | joint/teaming, neither side buying | human only |
| `internal` | an internal org or program relationship, no external counterparty | human only |

`role` (on each member — that entity's position):
`counterparty` (default for a plain supplier), `prime`, `subcontractor`,
`customer`, `internal_owner`, `member` (known participant, position not
established).

**Why `member` must exist:** task #335's `cross_mention_match`
(`workgraph_signals.py:485-522`) returns `(company, keyword)` — it proves
the text says "subcontract" near a company name, but it **cannot tell which
side is prime**. Assigning `prime`/`subcontractor` from that would be
exactly the fabrication both existing sweeps refuse to do. So an
automatically-discovered two-company relationship gets two `member` rows
and surfaces for a human; roles get set on confirmation, never inferred.

### 3.4 Worked examples, using this domain's real categories

**Example 1 — plain supplier (the Authenticx case, task #304's own founding case).**
Three projects: CMH Chatbots, Lilly Direct, Omvoh/Olumiant/Ebglyss;
`workgraph_classify` topics `financial` (three separate PRs) and `contract`.

```
Entity ent-a1  type=company  display_name="Authenticx"  entity_key="authenticx"
  aliases: "Authenticx" (party_company), "AUTHENTICX INC" (system_party)
  identifiers: email_domain=authenticx.com (from rep@authenticx.com, rep2@authenticx.com)
Relationship rel-x  name="Authenticx"  relationship_type=supplier  primary_entity_id=ent-a1
  relationship_entities: (rel-x, ent-a1, counterparty, backfill)
  project_relationships: proj-cmh, proj-direct, proj-omvoh
```

Identical behavior to today, plus: a durable entity the Supplier Dashboard
and Aristotle can group on, and an alias set that survives the next
spelling variant.

**Example 2 — prime/subcontract (the Scriptly / Sodalis case named in
`docs/design/CANDIDATE_DETECTION_BROADENING.md` and
`workgraph_signals.py:486-495`).** Marc holds a direct Sodalis MSA; a
Scriptly PV1 project references "the existing Scriptly subcontract".
Topics: `contract`, `negotiation`.

```
Entity ent-s1 company "Sodalis"   key="sodalis"   aliases: "Sodalis", "Sodalis Inc"
Entity ent-s2 company "Scriptly"  key="scriptly"  aliases: "Scriptly", "Scriptly Rx"
Relationship rel-msa   type=supplier          entities: (ent-s1, counterparty)   projects: Sodalis MSA
Relationship rel-prime type=prime_subcontract entities: (ent-s1, member), (ent-s2, member)
                                              projects: Scriptly PV1, Sodalis MSA
                                              (proposed from cross_mention:scriptly (subcontract))
```

Two Relationships over overlapping entities and projects — which is the
whole reason a member table is needed: today the "Sodalis + Scriptly are
structurally related" fact has nowhere to live except a name string, so it
lives nowhere. Roles stay `member` until Marc says which is prime. Note
`Scriptly Rx` is a real alias `normalize_company_name` would never
collapse (§1.3) — it is a **human-confirmed** alias, not a derived one.

**Example 3 — contract_review with a legal-form comma (the Fullstory case,
real observed data).** A ContractPodAI intake says
`Supplier Name: Fullstory, Inc`; the parties path derives `Fullstory` from
`fullstory.com`. Action kind `contract_review`
(`server_lean.py:2497-2499`, `skills_registry`), topic `contract`.

```
Entity ent-f1 company "Fullstory" key="fullstory"
  aliases: "Fullstory" (party_company, H), "Fullstory, Inc" (cpai_supplier_name, H)
  identifiers: email_domain=fullstory.com
Relationship rel-f type=supplier entities: (ent-f1, counterparty)
```

Today these two strings produce **two** normalized keys (`fullstory` /
`fullstory,`) and therefore risk two Relationship rows and two Supplier
Dashboard entries. Under §5.2's `entity_key` they are one entity — and this
is achieved **without touching `normalize_company_name`**, which is what
keeps it off the guardrail (§7).

**Example 4 — Microsoft aliases (the #342 founding case).** Projects
"Microsoft EA Renewal" (`contract`) and "Microsoft Copilot Pilot"
(`rfp-sourcing`), sharing only a company name.

```
Entity ent-m1 company "Microsoft" key="microsoft"
  aliases: "Microsoft" (H, deterministic), "MICROSOFT CORP" (H, deterministic),
           "Microsoft, Inc" (H, entity_key), "MSFT" (H, HUMAN-confirmed only),
           "Microsoft Ireland Operations Ltd" (H, HUMAN-confirmed only)
```

`MSFT` and the Irish subsidiary are the honest boundary: no deterministic
rule reaches them, and no LLM should be asked to guess them into a merge.
They arrive through the human-confirmation path (§5.4) or not at all.

---

## 4. Where entity resolution is *used* (and where it is not)

Consumers, in the order they should be wired (§6.4):

1. `workgraph_status_report._vendor_name_for_project` — prefer the primary
   entity's `display_name` over `relationships.name`. Cosmetic, zero risk.
2. `workgraph_suppliers.py` + `list_issues_for_company` — group by resolved
   entity instead of the raw `parties.company` string. **This is the real
   payoff**: it closes the `"Microsoft"` / `"Microsoft Corp."` split that
   exists in the dashboard today.
3. `workgraph_aristotle._group_keys_for_issue` / `_issues_to_check` — same
   substitution, so `match_on='supplier'` rules correlate across aliases.
   Behavior-affecting (rule prerequisite satisfaction), so it needs its own
   before/after count, though it is *not* candidate detection.
4. `list_relationships_needing_review` — gains entity/type/role context.
5. **`workgraph_projects._matched_data_points` — deliberately NOT wired.** §7.

---

## 5. Alias resolution mechanics

**Recommendation: both, layered — deterministic normalization for the
mechanical cases, human confirmation for everything requiring judgment,
with an explicit boundary between them.** Neither alone is right:
deterministic-only cannot reach `MSFT`; human-only would make Marc confirm
every `INC`/`CORP` variant by hand.

### 5.1 Layer 0 — `normalize_company_name`, unchanged

Stays exactly as it is, doing exactly what it does today, for the
candidate-detection `supplier` point and the fast-track index. **Not
modified by this design.** (§7 for why that restraint is the load-bearing
decision here.)

### 5.2 Layer 1 — `entity_key()`, a NEW function used only on the entity side

New, additive, in `workgraph_entities.py` (a new module — not in
`workgraph_signals.py`, so it cannot be reached accidentally from the
matching path):

1. lowercase, NFKC-normalize, collapse internal whitespace
2. strip all punctuation except `&` (fixes the comma class: `Fullstory, Inc`)
3. drop a leading `the`
4. remove legal-form tokens **anywhere**, repeatedly — extending the
   current list with `llp`, `plc`, `gmbh`, `ag`, `sa`, `sas`, `bv`, `nv`,
   `ab`, `as`, `a/s`, `oy`, `pty`, `kk`, `spa`, `srl` (fixes
   `Sodalis Inc Ltd`, `Deloitte Consulting LLP`, `Novo Nordisk A/S`)
5. drop a trailing parenthetical country/region group (fixes
   `Microsoft Corp (US)`)
6. collapse whitespace again; return `''` for a degenerate result

Explicitly **not** included: edit-distance/fuzzy matching, token-subset
matching (`Microsoft Ireland Operations` ⊂ `Microsoft` — the exact rule
that would wrongly merge `Microsoft` with `Microsoft Ireland` *and*
`Deloitte` with `Deloitte Consulting`), acronym generation, or any LLM
call. Deterministic and inspectable, matching `cross_mention_match`'s own
stated discipline ("deliberately narrow and inspectable, never a score").

An `entity_key` collision **is** treated as the same entity — same posture
`get_or_create_relationship_by_name` already takes on the weaker Layer-0
key, so this is a superset of behavior already accepted, not a new kind of
risk. Every such consolidation writes an `audit_log` row.

### 5.3 Layer 2 — evidence-based *proposals*, never merges

Two deterministic signals that suggest but never decide:

- **Shared `email_domain`** — two entities holding the same non-internal,
  non-automated email domain. Strong, but not conclusive (resellers,
  acquisitions, shared infrastructure).
- **Co-occurring names for one system record** — e.g. a
  `contractpodai_requests` row whose `supplier_name` differs from the
  `parties.company` on the same `issue_id`. This is how `Fullstory, Inc` /
  `Fullstory` would have been caught even without Layer 1.

Both write a proposal row, never an alias:

```
entity_alias_proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id     TEXT NOT NULL,       -- the surviving/canonical candidate
    other_entity_id TEXT,              -- set when proposing an entity merge
    proposed_alias  TEXT,              -- set when proposing a bare alias
    evidence      TEXT NOT NULL,       -- human-readable, e.g. "shared email_domain fullstory.com"
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending|confirmed|rejected
    created_ts    REAL NOT NULL,
    resolved_ts   REAL
)
```

Shape copied from `pending_issue_state_suggestions`
(`workgraph_store.py:1319-1330`) — the established suggest-only pattern in
this codebase. A `rejected` row is durable, so the same proposal never
reappears: the same job `identity_constraints` does for grouping vetoes.

(Considered and rejected: reusing `identity_constraints` with a new
`company_alias` constraint type. Its `CHECK` list would need widening,
which means a full table rebuild — real cost, no benefit. Its existing
`confirm_person_alias`/`prevent_person_merge` types remain the right home
if person-entity merging is ever built, and this design does not need them.)

### 5.4 Layer 3 — human confirmation, and the never-auto-merge rule

Confirming a proposal writes an `entity_aliases` row (`source='human'`,
`confidence='H'`) and, for an entity merge, reassigns
`relationship_entities`/`entity_identifiers` to the survivor with
`audit_log` rows — the same reassign-then-delete shape #343's consolidation
already uses (`workgraph_store.py:1996-2003`).

Surfaced the same pull-based way as the existing relationship audit: a
read-only listing behind `/api/workgraph/entity-alias-proposals` and an MCP
tool, following task #304 item #2's explicit scoping call (chat/MCP only,
no cockpit confirm/reject UI). Unlike that listing, these **do** have a
clean yes/no verdict, so they carry real status — that is the one
justified departure, and it needs Marc's sign-off.

**The rule, stated once:** an entity merge happens only on (a) a Layer-1
deterministic key collision, or (b) explicit human confirmation. No LLM
judgment, no similarity score, no domain heuristic ever merges two entities
on its own.

---

## 6. Migration / backfill plan

Five phases. Each is independently shippable, independently valuable, and
leaves the system fully working if the next never happens. **A `backup.py`
snapshot is a hard prerequisite for Phase 2** (the first phase that writes
derived identity).

### 6.0 Phase 0 — read-only audit, zero writes

A script (repo precedent: `backfill_stale_marker_projects.py`) that
reports, against the **live** DB:

- every existing `relationships` row, its `normalized_name`, and its
  `entity_key` under §5.2
- which rows collide under `entity_key` but not under `normalized_name`
  — i.e. **the exact list of merges Phase 2 would perform**
- distinct external `parties.company` values → how many distinct
  `entity_key`s → which would collapse (the Supplier Dashboard's real alias
  split, quantified)
- Layer-2 proposals that would be generated, with evidence

Marc reads this before anything writes. Phase 2's blast radius becomes a
finite reviewed list rather than a trusted algorithm.

### 6.1 Phase 1 — schema only

Create the four tables; add the two nullable columns. Nothing reads them.
No behavior change, no test change. Safe to ship alone.

### 6.2 Phase 2 — backfill existing relationships (1:1, then reviewed merges)

For each existing `relationships` row, **in `created_ts` order** (so the
oldest row is always the survivor, matching #343's own tie-break):

1. compute `entity_key(name)`
2. if no active entity has that key → create one:
   `entity_type='company'`, `display_name = relationships.name` (the real
   spelling already stored — never re-derived), `created_by='backfill'`;
   add an `entity_aliases` row `source='relationship_name'`, `confidence='H'`
3. if one exists → **consolidate onto it**: alias row for this spelling,
   `audit_log` entry, and — because two `relationships` rows now share one
   entity — reassign this row's `project_relationships` to the older
   relationship and delete the newer row, byte-for-byte the same
   INSERT-OR-IGNORE-then-DELETE dance #343 already performs
   (`workgraph_store.py:1996-2003`)
4. set `primary_entity_id`, `relationship_type='supplier'`, and insert
   `relationship_entities(relationship_id, entity_id, 'counterparty', 'backfill')`

Why nothing is lost or duplicated: step 2 is a total function from existing
rows to entities (every row gets exactly one), and step 3 is exactly the
consolidation semantics already shipped and already tested for the weaker
key — so no `project_relationships` edge is dropped and no project loses a
relationship. Every merge is in Phase 0's reviewed list and in `audit_log`.

`relationship_type='supplier'` for every backfilled row is **accurate, not
a placeholder**: both producers can only ever discover supplier
relationships (`_shared_supplier_name` / `dp-fasttrack-supplier`). Anything
else is human-created going forward.

**This backfill lives in its own idempotent sweep function, NOT inline in
`init_workgraph()`.** #343 put its consolidation inline, so it re-runs its
two probe queries on every single `init_workgraph()` call in every process;
that is a real, if small, cost and a real footgun (it also had to
drop-and-recreate a unique index mid-repair to avoid its own index
rejecting the fix — `workgraph_store.py:1956-1967`). New backfills follow
the `claim_daily_run("...", "v1")` once-ever gate pattern
(`workgraph_projects.ensure_fasttrack_index_backfilled`, `:655-669`)
instead. `init_workgraph()` creates schema; sweeps move data.

### 6.3 Why `entity_key` is not UNIQUE

Tempting, but wrong here. Layer 2 proposals and human merges mean two
entities can legitimately coexist with keys that *should* be one until a
human decides — and a unique index would make the pending state
unrepresentable, which is precisely the failure mode
`work_object_relationships` had before task #304 gave "related but
different" a home. Uniqueness is enforced **procedurally** in
`get_or_create_entity_by_name` (query by key, create if absent), the same
way `get_or_create_relationship_by_name` behaves; the partial unique index
on `relationships.normalized_name` stays as the belt-and-braces guard for
the legacy path and for §1.5's tests.

### 6.4 Phase 3 → Phase 4 — identifiers, then read paths

**Phase 3 (identifiers + proposals, still no read-path change).** For each
company entity, attach `email_domain` identifiers from external, non-automated
parties whose `entity_key(company)` matches the entity's key — excluding
`lilly.com`/`network.lilly.com`/`lists.lilly.com` and any
`is_automated_sender` address. Generate Layer-2 proposals. Do **not**
auto-create an entity for every distinct `parties.company` value (hundreds
of rows, most with no Relationship) — those become proposals, and §9's open
question #1 decides whether they graduate.

**Phase 4 (read paths, in the §4 order).** `parties.company` is **never
rewritten**: it stays raw extracted evidence, and resolution happens at
read time via `alias_key` lookup. Consequences, stated plainly:

- `issue_parties` — **completely untouched**, no column added, no row changed.
- `parties.company` — untouched. `correct_party_affiliation` and
  `backfill_clear_machine_signal_companies` keep working exactly as they do.
- `project_relationships` — untouched.
- The only *behavioral* change is that supplier-grouped reads collapse
  aliases that are currently split. That is the intended fix, and it needs
  a before/after count (how many dashboard groups merge, which Aristotle
  rules change prerequisite state) even though it is not candidate detection.

### 6.5 The one real migration hazard: `lessons.situation_key`

`workgraph_lessons.situation_key` (`:62-69`) bakes the raw lowercased
company string into a key with `UNIQUE(situation_key, outcome)` and
accumulated `trust_score`/`hit_count`. Re-keying it on an entity id would
orphan every existing lesson's track record — silently, because a missed
lookup just returns "no precedent".

**Recommendation: do not change `situation_key` in this design.** Leave
Total Recall on the company string. If entity-keying it is ever wanted, it
is its own task with an explicit key-remap migration (old key → new key,
merging `hit_count`/`trust_score` where two old keys collapse) and its own
tests. Naming it here so it is not discovered halfway through Phase 4.

---

## 7. Does this feed candidate detection? — No, and that is deliberate

**Recommendation: Phases 0–4 stay entirely on the Relationship side and
touch nothing in `workgraph_pipeline2.find_candidates`,
`workgraph_projects._matched_data_points`, the `>= 2` threshold, or
`judge_candidates`.**

Concretely, this design does not modify: `normalize_company_name`;
`_matched_data_points`; `_fasttrack_index_values` /
`candidate_pool_via_data_point_index` / the `data_point_values` fast-track
rows; `find_candidates`; `judge_candidates`. Verified by inspection that
these are the only paths feeding the gate.

**Where the temptation is, named rather than smoothed over.** §1.3 shows
that today, `Fullstory` vs `Fullstory, Inc` awards **no** `supplier` point,
because `_matched_data_points` intersects `normalize_company_name` outputs.
An entity-aware supplier point — comparing resolved `entity_id`s instead of
normalized strings — would fix that, and would strictly **add** matched
points, which means strictly more pairs clearing the `>= 2` gate. It looks
like a pure improvement. It is exactly the kind of change ROADMAP.md's
standing guardrail exists to slow down:

- more candidates = more LLM judgment calls = more chances of a wrong merge
  on pairs that previously never reached judgment;
- an alias table is *mutable human input*, so candidate detection would
  stop being a pure function of extracted evidence — one wrong confirmed
  alias would silently widen the gate corpus-wide;
- the same argument applies to simply "fixing" `normalize_company_name`'s
  comma case, because that function is **shared** with
  `_matched_data_points`. That is the specific reason §5.2 puts the
  stronger normalizer in a new function in a new module instead of
  improving the existing one in place. It is a real cost — the comma bug
  keeps hurting candidate detection until a separate, gated task fixes it —
  and it is the right trade for now.

**If Marc ever wants that wired in**, it is a separate, explicitly
authorized task, and per ROADMAP.md lines 37-47 it must ship with a
before/after comparison against `tests/test_workgraph_regression_corpus.py`
plus a live backtest, with no "it's just a refactor" exemption. The
conservative shape would be: resolve entity ids **in addition to**, never
instead of, the current normalized comparison, so the point can only be
awarded where it is awarded today *plus* alias-equal cases; then measure
how many new pairs cross the gate, on the real corpus, before shipping.

Nothing in this design assumes or requires that step. If any part of the
build starts to imply "the gate can be looser because the Entity layer is
smarter", that is a red flag in this design and should stop the work, not
be smoothed over.

---

## 8. Test impact

**Stays green, unchanged** (verified against the assertions themselves):

- `tests/test_workgraph_signals.py:285-290` — `normalize_company_name` is
  not modified.
- `tests/test_workgraph_store.py:3155-3220` — `get_or_create_relationship_by_name`
  keeps its signature, dedupe key (`normalize_company_name`), return value,
  and first-spelling-wins `name`; the blank-name and idempotency behaviors
  are untouched; `test_init_workgraph_consolidates_pre_existing_duplicate_relationships`
  keeps passing because #343's inline consolidation is **not removed**
  (Phase 2 adds an entity-level sweep alongside it, it does not replace it).
- All 13 tests in `tests/test_workgraph_relationships.py` — both sweeps'
  decision logic, skip counters, and display-name derivation are untouched;
  the entity write is an additional side effect inside
  `get_or_create_relationship_by_name`, invisible to their assertions
  (`relationships_created`, `project_links_created`, `rels[0]["name"]`).
- `tests/test_workgraph_regression_corpus.py` and
  `tests/test_workgraph_pipeline2.py` — nothing on the candidate path changes.

**One deliberate behavior change, called out rather than buried:** after
Phase 2, a name pair like `Fullstory` / `Fullstory, Inc` (or
`Sodalis Inc Ltd` / `Sodalis`) resolves to **one** entity and therefore one
Relationship, where today it produces two. No existing test asserts the
current two-row behavior for the comma class — checked — so nothing breaks,
but it is a real semantic change and needs new tests plus a line in the
Phase 0 audit showing exactly which live rows it affects.

**New tests needed:** `entity_key` unit table (every row of §1.3's probe
plus the negative cases that must **not** collapse — `Deloitte` vs
`Deloitte Consulting`, `Scriptly` vs `Scriptly Rx`, `Microsoft` vs
`Microsoft Ireland Operations`); Phase 2 backfill idempotency and
oldest-survives consolidation; proposal generation never auto-merges; a
rejected proposal never reappears; `email_domain` never attaches
`lilly.com`/`network.lilly.com`/automated-sender domains; Phase 4
read-path equivalence for entities with a single alias (must be identical
to today).

---

## 9. Effort sequencing: does this need task #378's migration subsystem first?

**Recommendation: no — proceed under today's `init_workgraph()` style, with
two conditions.**

Why it does not need #378:

- All Phase 1 DDL is `CREATE TABLE IF NOT EXISTS` plus two nullable
  `ALTER TABLE ... ADD COLUMN` in the existing try/except idiom — the exact
  pattern this file already uses ~100 times, including #343's own
  `normalized_name` addition.
- **No CHECK widening, no table rebuild, no FK re-point.** §2.1 deliberately
  omits CHECK constraints and `REFERENCES` clauses precisely so this stays
  true. The FK-target rewrite that genuinely needs #378
  (`ROADMAP.md:439-449`, 17 columns / ~15 tables) is untouched by this work
  and not made worse by it.
- The risk here is **not the DDL, it is the backfill's data decisions** —
  and #378 would not reduce that risk. Phase 0's read-only audit plus a
  `backup.py` snapshot plus per-merge `audit_log` rows address it directly
  and are all available today.

The two conditions:

1. **A `backup.py` snapshot immediately before the first Phase 2 run**, and
   Phase 0's audit reviewed by Marc first. Phase 2 deletes
   `relationships` rows (consolidation); that is irreversible without a
   snapshot.
2. **Backfill lives in its own idempotent, re-runnable sweep behind a
   `claim_daily_run(..., "v1")` gate — never inline in `init_workgraph()`**
   (§6.2). This is the one place this design deliberately diverges from
   #343's precedent, and the reason is #343 itself.

Conversely, waiting for #378 has a real cost: it is a large,
not-yet-started subsystem whose own first job is already claimed by the
FK-target rewrite, so gating this behind it likely means this never gets
built.

---

## 10. Open questions for Marc

1. **Does an Entity exist for every external company, or only for companies
   with a real Relationship?** (The trickiest question, and it decides
   Phase 3's shape.) Entity-per-company is what lets the Supplier Dashboard
   and Aristotle group on entities at all — the largest real payoff — but it
   means hundreds of auto-created entities, most with no Relationship, and a
   curation surface Marc may not want. Relationship-only keeps the entity
   list small and meaningful but leaves the dashboard's alias split
   unfixed.
2. **`customer` as an `entity_type` or a `role`?** This design recommends
   role (§2.2). The roadmap line says type.
3. **Prime/sub roles:** leave `member` until confirmed (this design's
   recommendation), or let a `cross_mention` keyword propose which side is
   prime? Proposing means guessing from one keyword's position.
4. **Should the comma-class normalization bug also be fixed on the
   candidate-detection side?** It is a real live miss (§1.3, §7). This
   design deliberately does not — but it is guardrail work with a real
   before/after cost, and Marc may want it queued explicitly rather than
   left as known-broken.
5. **`program` / `internal_org` entities have no producer.** Ship the types
   as human-only, or leave them out of the vocabulary until something can
   populate them?

---

## 11. Honest effort / risk estimate

| Phase | Effort | Risk | Notes |
|---|---|---|---|
| 0 — read-only audit | small | **none** | pure read; the highest-value/lowest-cost piece, worth doing even if nothing else is built |
| 1 — schema | small | very low | additive DDL only, established idiom |
| 2 — backfill + consolidation | medium | **highest in the plan** | deletes `relationships` rows; mitigated by Phase 0 review + snapshot + audit_log + oldest-survives |
| 3 — identifiers + proposals | medium | low | writes only new tables; the domain-exclusion guard is the one thing to get exactly right |
| 4 — read paths (4 consumers) | medium | medium | behavior-affecting for Supplier Dashboard + Aristotle; needs before/after counts |
| 5 — candidate detection | not scoped here | **guardrail** | separate authorization + regression corpus + live backtest, per ROADMAP.md |

Overall: **Phases 0–2 are a contained, well-understood piece of work;
Phase 4 is where real behavior changes and where the review effort
belongs.** The dominant risk is not the schema, it is Phase 2's
consolidation on a database carrying real accumulated history — which is
why Phase 0 exists and why Phase 2 does not start without a snapshot and
Marc's sign-off on the exact merge list.

The single most valuable thing to build first, and it is nearly free:
**Phase 0.** It answers "how much alias splitting is actually happening in
Marc's live data" with real numbers, and if the answer turns out to be
"barely any", that is a legitimate reason not to build the rest.
