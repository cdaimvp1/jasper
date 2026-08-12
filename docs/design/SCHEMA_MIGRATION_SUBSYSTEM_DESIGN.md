# Schema migration subsystem — design (task #378)

**Status: design only. Nothing in this document has been built.** No `.py`
file was modified in producing it. Marc's review gates any build work.

**Why now.** Marc's own framing: *"the data is becoming Jasper; losing or
subtly corrupting years of learned work history would be catastrophic."*
`workgraph_store.init_workgraph()` is 2,963 lines (lines 92-3051) of
accumulated inline schema work with no ledger, no automatic pre-migration
backup, mostly non-transactional statements, and no post-migration audit.
It has been the right call for rapid single-user iteration and it is now the
single largest concentration of unmanaged risk against an irreplaceable
98 MB database.

**Scope.** This designs the subsystem that replaces that pattern *going
forward*. It does not redesign the schema. Its first real job, per
`ROADMAP.md`, is the FK-target rewrite that task #365 found necessary —
worked through concretely in Section 10.

---

## 0. Grounding: what was actually read, and what was re-verified live

Everything below rests on direct reading, not inference:

- `workgraph_store.py` lines 1-3051 in full (`init_workgraph()` and
  `_connect()`), plus the migration-adjacent helpers.
- `docs/design/ROADMAP.md` in full — the #365 audit findings, the
  "Formalize schema migrations" hardening item, the standing
  candidate-detection guardrail.
- `docs/design/SCHEMA_FK_DEBT.md` in full (task #157).
- `backup.py`, `retention.py`, `health_check.py` (`check_db_integrity`,
  task #332), `paths.py`.
- `tests/conftest.py` (`ws_db` / `isolated_paths` fixtures),
  `tests/test_workgraph_store.py` migration tests,
  `tests/test_multiprocess_concurrency.py`, `tests/test_outlook_ingest.py`
  idempotency test.

The #365 FK audit exists only as prose in `ROADMAP.md` — there is no audit
script checked into the repo (confirmed: zero occurrences of
`foreign_key_check` in any `.py` file). So its findings were **re-verified
live**, read-only, against the production file
`C:\Users\lane_marc@lilly.com\Symphony\new_cohort\data\workgraph.db`
opened with a `file:...?mode=ro` URI. Nothing was written; no migration was
run. The re-audit both **confirms** #365 and **corrects it in three ways
that materially change the design**:

| Fact | ROADMAP (#365) | Live re-audit (2026-08-12) |
|---|---|---|
| FK columns total | "~43" | **43** — confirmed exactly |
| Pointing at `work_objects_pre_fix4` (nonexistent) | 6 | **6** — confirmed exactly |
| Pointing at the `issues` **view** | 2 | **2** — confirmed exactly |
| Pointing at `*_pre_workobjects` snapshots | "9 ... plus `project_links` x2" | **11** — confirmed |
| Summary count | "17 affected columns across ~15 tables" | **19 affected columns across 17 tables.** The "17" is the *table* count, not the column count |
| Fossil snapshot tables | implied to hold the frozen pre-migration rows | **`issues_pre_workobjects`, `projects_pre_workobjects`, `evidence_pre_evidenceunits` all contain 0 rows** |
| "~20,700 orphan rows" | stated as orphans vs. the stale FK target | Confirmed, and fully explained: the declared parents are **empty**, so every child row is trivially an orphan against them |
| Real integrity vs. the *intended* parent | not measured | **0 orphans.** All 11 sampled child columns resolve 100% cleanly against `work_objects` |

The last row is the single most important new fact in this document: **the
data is already fully consistent with the parent it is supposed to point
at.** The FK problem is entirely a schema-text problem, not a data-repair
problem. That is what makes Section 10 tractable.

A fourth finding, not in #365 at all, is discussed in Section 9.3: the live
schema and the source's `CREATE TABLE` text have **already diverged** for at
least one table.

Environment facts worth recording: SQLite **3.49.1**, `journal_mode=wal`,
`PRAGMA user_version = 0` (unused, therefore available), 98,344,960 bytes /
24,010 pages.

---

## 1. Inventory: every migration shape this system has actually needed

The design must handle all of these, not just the easy ones. Each row is a
real, present shape with a line reference into `workgraph_store.py`.

| # | Shape | Count | Line examples | Real hazards already hit |
|---|---|---|---|---|
| S1 | `CREATE TABLE IF NOT EXISTS` (bootstrap) | ~45 | 98, 146, 1004, 2067 | Silently no-ops against an existing **view** of the same name (`issues`/`projects`/`evidence`) — deliberately relied upon (see the comment at 128-144). Also silently no-ops against an existing table with a *different* definition, which is how live/source drift becomes invisible (Section 9.3). |
| S2 | `CREATE INDEX IF NOT EXISTS` | ~60 | 124, 1022, 2095 | "views may not be indexed" once the target became a view — needs `try/except` (169-173, 315-318, 798-801). `IF NOT EXISTS` does **not** protect a *new* index name against a view. |
| S3 | `CREATE UNIQUE INDEX` / partial index | 2 | 362, 2007 | Fails outright against pre-existing duplicate data; must be ordered *after* a data repair, and the repair must drop the index first (1967) or the index rejects its own repair mid-flight (1956-1966). |
| S4 | `CREATE VIRTUAL TABLE ... USING fts5` | 1 | 1778 (`evidence_fts`) | Shadow tables; a rebuild of `raw_items` would need the FTS index reconciled separately. |
| S5 | `ALTER TABLE ADD COLUMN` under `try/except OperationalError: pass` | ~30 | 286, 438, 795, 915, 1036, 1419, 1745, 2182, 2923, 3004 | Four sub-variants: plain nullable; `NOT NULL DEFAULT <const>`; with inline `CHECK`; with inline `REFERENCES`. **The blanket `except` swallows every error, not just "duplicate column name"** — including genuine ordering bugs. Two are documented in-file: the `claim_events` ordering bug (1143-1145) and the Fix-4 ordering bug where a rebuild ran before its own columns existed, so `'dormant'` only landed on a *second* `init_workgraph()` call (2229-2240). |
| S6 | CHECK-widening **rename-old-first** rebuild | **10** | 186 (issues #44), 567 / 605 / 640 / 681 (alerts #55/e14/e16/e17), 769 (projects #62), 1149 / 1192 (claim_events #304/fix3), 1269 (pending_claim_suggestions #304), 2244 (work_objects Fix 4) | Detection is a **substring test on stored DDL text** (`"'dismissed'" not in sql`) — fragile. Column list is sometimes derived from `PRAGMA table_info` (issues, projects) and sometimes hardcoded (alerts, claim_events, work_objects); the hardcoded variant caused a real production bug (210-220). **This shape is the direct cause of the FK fossils** — see Section 6.3. |
| S7 | One-time table split / reshape, gated on existence | 2 | 2062-2167 (`work_objects`, #114), 2575-2630 (`evidence_units`, #340) | The pre-lock existence check is stale by the time the lock is held; needed a post-lock re-check and a private `_MigrationAlreadyDone` sentinel (2100-2124), because a re-run would raise `IntegrityError` — which the `except OperationalError` handler does **not** catch. |
| S8 | Compatibility `VIEW` + `INSTEAD OF` triggers | 3 groups | 2329-2412 (`issues`, `projects`), 2676-2710 (`evidence`) | `issues` uses unconditional `DROP VIEW`+`CREATE VIEW` (a filter change must re-apply); `projects`/`evidence` use `CREATE VIEW IF NOT EXISTS`. `DROP`+`CREATE` is not atomic across processes (2317-2327). `CREATE TRIGGER ... INSTEAD OF` against a still-real table raises uncaught (2632-2671). |
| S9 | Stale view/trigger self-heal | 1 | 2465-2558 | Gated scan of `sqlite_master` for `%work_objects_pre_%`, then whole-group `DROP`+`CREATE`. An earlier *unconditional* version caused a real regression under the 16-process stress test (2433-2451). Must repair view+triggers as a group because dropping a view cascades its triggers (2452-2464). |
| S10 | Data backfill / consolidation inside init | 2 | 247-251 (`issue_state_history` seeding), 1945-2010 (`relationships.normalized_name` backfill + duplicate consolidation) | The second one **imports another module** (`workgraph_signals`) and loops in Python while conceptually mid-migration. |
| S11 | Index drop-then-recreate wrapping a data repair | 1 | 1967 / 2007 | See S3. |
| S12 | Concurrency guards | several | 76-89 (retry/backoff), 2119-2124, 2672-2675 | Every guard is hand-written per migration. The framework should own this once. |

**Two structural observations from the inventory:**

1. **Nothing here is a shape SQLite can't do transactionally** (Section 6).
   The current code is non-atomic because ~135 statements run as individual
   autocommit statements on an `isolation_level=None` connection — not
   because of any SQLite limit.
2. **Every one of these re-runs on every call.** `init_workgraph()` is
   invoked from 11 distinct entry points (`server_lean.py:470`,
   `health_check.py:455`, `ingest/scheduled_refresh.py:367`,
   `ingest/normalize.py:365`, `ingest/outlook_com_ingest.py:497`,
   `ingest/run_pipeline2_with_progress.py:16`, `retention.py:233`,
   `workgraph_alerts.py:157`, `workgraph_classify.py:1591`,
   `workgraph_parties.py:318`, plus every test), each in its own process.
   A ledger replaces ~135 statements and ~12 `sqlite_master` scans per
   process-start with one `SELECT`. That is a secondary benefit, but a real
   one.

---

## 2. Core design shape

A small, explicit, ordered-migration runner. Deliberately not a dependency
(no Alembic, no `sqlite-migrate`) — partly because of the Artifactory-only
package policy, mostly because the requirements here are narrow and the
hazards are specific to *this* schema's history, which a generic tool would
not know about.

```
migrations/
  __init__.py
  runner.py            # discovery, ledger, locking, backup hook, audit hook
  audit.py             # the integrity auditor (Section 7)
  fingerprint.py       # canonical schema fingerprinting (Section 9.2)
  0001_baseline.py     # init_workgraph()'s current body, verbatim, frozen
  0002_fk_target_rewrite.py
  0003_drop_empty_fossil_tables.py
  ...
```

A migration is a module exposing:

```
ID          = "0002_fk_target_rewrite"
TRANSACTIONAL = True          # False only with a written justification
DESCRIPTION = "..."           # one line, goes in the ledger
def precheck(conn) -> dict    # fail-closed assertions; returns captured state
def apply(conn, captured)     # the actual work
def audit(conn, captured) -> dict   # migration-specific post-assertions
```

`runner.run()` becomes the single thing `init_workgraph()`'s 11 callers
invoke (see Section 9.5 for how that swap is sequenced). Its loop:

1. Open one connection via the existing `_connect()` (WAL, `busy_timeout`,
   retry/backoff already proven at lines 76-89).
2. Ensure the ledger table exists (Section 3).
3. Read applied IDs. Compute pending = discovered IDs minus applied IDs.
4. **If pending is empty: return immediately.** This is the common path,
   every process start, one `SELECT`.
5. If pending is non-empty: take the pre-migration snapshot (Section 4).
   Fail closed if it fails.
6. For each pending migration in ID order: `BEGIN IMMEDIATE`; **re-read the
   ledger now that the write lock is held** (generalizing the
   `_MigrationAlreadyDone` lesson at 2100-2124 into the framework); if
   already applied, `COMMIT` and skip; else `precheck` → `apply` →
   `audit` → insert the ledger row → `COMMIT`.
7. Run the global auditor (Section 7). Report.
8. Stop at the first failure. Never continue past a failed migration.

**Non-goals, stated so they don't get assumed:** no down-migrations (see
Section 12), no automatic schema diffing into generated migrations, no
change to `PRAGMA journal_mode`, and no enabling of
`PRAGMA foreign_keys=ON` (Section 11).

---

## 3. The migration ledger

### 3.1 Identity and versioning

IDs are `NNNN_snake_case_slug` — a zero-padded monotonic ordinal plus a
human-readable slug. Zero-padded so lexical sort equals apply order, which
means the filesystem listing *is* the order and there is no separate
ordering metadata to drift.

Not a content hash as the primary key: a hash gives you identity but not
order, and order matters here (Fix 4 had to run after three specific
`ADD COLUMN`s — line 2229-2240 — and the `pending_claim_suggestions`
widening had to run after the `raw_item_id` `ADD COLUMN`). Ordinals make
that dependency expressible and reviewable.

Ordinals are assigned at authoring time. Single-author repo, so the
collision case (two agents both writing `0004_`) is a merge conflict, which
is the correct outcome — a conflict is visible, a silently reordered
migration is not.

### 3.2 Where the state lives

**In `workgraph.db` itself.** Two reasons, both concrete:

- It cannot drift from the schema it describes. A ledger in a sidecar JSON
  file can be restored, copied, or lost independently of the DB — and this
  repo has a documented incident of exactly that class (the `rm -rf`'d
  backup folder, `backup.py` lines 5-11).
- It is captured by the existing snapshot mechanism for free, so every
  backup is self-describing: restore the file, and it tells you which
  migrations it has.

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    id            TEXT PRIMARY KEY,           -- '0002_fk_target_rewrite'
    state         TEXT NOT NULL CHECK (state IN
                     ('applied','baselined','in_progress','failed')),
    applied_ts    REAL NOT NULL,
    completed_ts  REAL,
    applied_by    TEXT,                       -- process/entry-point label
    duration_ms   INTEGER,
    checksum      TEXT NOT NULL,              -- sha256 of the migration source
    backup_ref    TEXT,                       -- snapshot dir for this run
    audit_json    TEXT,                       -- the audit result, verbatim
    note          TEXT
);
```

Deliberately **not** `CHECK`-constrained beyond `state`, and `state`'s
vocabulary is chosen to be complete now — because widening a `CHECK` on
*this* table would need exactly the S6 rebuild dance the subsystem exists to
retire, which would be an embarrassing bootstrap problem. (This is the same
reasoning already applied deliberately at line 2893-2897 for
`raw_items.held_aside_status`.)

`PRAGMA user_version` (currently `0`) gets used as a **cheap secondary
marker only** — set to the highest applied ordinal. It is not the source of
truth (one integer cannot record 40 migrations, their checksums, or their
`baselined` vs `applied` distinction), but it makes "is this DB at or past
level N" answerable without parsing anything, and it survives in a raw file
inspection.

### 3.3 The four states, and why `baselined` is separate

- `applied` — the runner observed this migration's transition on this DB.
- `baselined` — the runner **asserted the end state without observing the
  transition**. This is what the existing live DB gets for `0001`
  (Section 9). Keeping it distinct from `applied` is not pedantry: it is the
  honest record that this DB's pre-ledger history was reconstructed by
  inspection, so any future forensic question ("did migration X really run
  here?") gets a truthful answer instead of a confident wrong one.
- `in_progress` — written and committed *before* the work, only for
  `TRANSACTIONAL = False` migrations (Section 6.4). An `in_progress` row
  found at startup means the runner **refuses to proceed** and reports.
  It never auto-retries a possibly-half-applied non-transactional
  migration; that is a human decision with a backup in hand.
- `failed` — recorded on a best-effort basis after a rollback, purely for
  visibility. The runner treats `failed` as "not applied" and will retry it
  on the next run, because a rolled-back transactional migration genuinely
  did nothing.

### 3.4 Checksums

`checksum` is the sha256 of the migration module's source text, recorded at
apply time. On every later run, a mismatch between the recorded checksum and
the current file is **reported as a warning and never acted on**. An edited
already-applied migration is an authoring mistake to surface, not a
condition to fix by re-running — re-running is precisely the destructive
move this subsystem exists to prevent.

### 3.5 Concurrency

`BEGIN IMMEDIATE` plus a post-lock ledger re-read, per Section 2 step 6.
This is not a new invention; it is the fix pattern this file already
arrived at twice under real load (#339 at lines 2100-2124, #340 at lines
2632-2675). The value of the framework is that it is written **once**
instead of per migration, and that the losing process's outcome is a clean
skip rather than a hand-rolled sentinel exception.

---

## 4. Automatic pre-migration backup

### 4.1 Reuse, with one concrete extension

`backup.py` is the right mechanism and should not be duplicated. It already
solves the two hard parts:

- `snapshot_db()` uses `sqlite3.Connection.backup()`, not a file copy —
  correct for a live WAL database with concurrent writers, which is exactly
  the situation here (11 entry points, many processes). A raw copy could
  capture a torn file (`backup.py` lines 17-21).
- `create_labeled_snapshot()` was built for literally this use case: *"the
  replacement for the old ad hoc 'let me save a copy before I do this risky
  thing' habit"* (line 199-200), and labeled snapshots are **excluded from
  the GFS prune rotation** (line 202-204). A pre-migration backup must never
  be thinned away by retention; that property already exists and is already
  tested (`tests/test_backup.py::test_create_labeled_snapshot_survives_prune_rotation`).

**The one real problem, and the fix.** `create_labeled_snapshot()` reads
`paths.WORKGRAPH_DB` and `paths.BUS_DB` as module constants. But the
`ws_db` test fixture monkeypatches **`workgraph_store.WORKGRAPH_DB`**, not
`paths.WORKGRAPH_DB` (`tests/conftest.py` line 85) — the same trap
`health_check.check_db_integrity` explicitly documents and works around
(lines 369-378). If the migration runner called
`create_labeled_snapshot()` as-is, then **under test it would snapshot the
real production database**, repeatedly, on every fixture setup. That is a
performance disaster and a genuine data-handling surprise.

Recommended extension, backward-compatible:

```python
def create_labeled_snapshot(label: str, *, db_paths: list[Path] | None = None) -> dict:
    if db_paths is None:
        db_paths = [paths.WORKGRAPH_DB, paths.BUS_DB]   # unchanged default
```

The runner then passes `db_paths=[workgraph_store.WORKGRAPH_DB]` explicitly.
This is a two-line change to `backup.py` and keeps every existing caller and
test identical.

### 4.2 Exactly when it triggers

- **Once per run, not once per migration.** Checked after computing
  `pending`, before the first `BEGIN IMMEDIATE`. A first run with 40 pending
  migrations must not produce 40 snapshots of a 98 MB file.
- **Only if `pending` is non-empty.** The overwhelmingly common path (every
  process start, nothing to do) takes zero backup cost.
- **Fail closed.** If the snapshot raises, the run aborts and no migration
  executes. This is a deliberate posture change from `backup.py`'s existing
  callers, which are report-only (`retention.py` line 222,
  `health_check.check_backup_recent`). Report-only is right for a nightly
  rotation; it is wrong for the gate in front of an irreversible schema
  change.
- **A `None` return is not a failure.** `snapshot_db()` returns `None` when
  the file doesn't exist yet — a fresh install with nothing to lose
  (`backup.py` lines 49-51). The runner treats `None` as "nothing to back
  up" and proceeds. This is what keeps fresh installs and the test suite
  fast.

### 4.3 What it backs up, and where

- `workgraph.db` only. `bus.db` is not touched by any migration here.
  (Passing an explicit `db_paths` makes this precise rather than incidental.)
- Destination: `paths.BACKUPS_DIR / "labeled" / "<ts>_premigration_<first>_to_<last>"`,
  e.g. `2026-08-14T031200_premigration_0002_to_0003`. Inside `DATA_DIR`,
  never inside the code repo — the structural property `paths.py` lines
  74-84 exist to guarantee.
- The resulting directory path is written to `backup_ref` on **every** ledger
  row created in that run, so any single row answers "what was the state
  before this?"
- Gzipped by `snapshot_db()`; the live 98 MB file compresses to well under
  that. One snapshot per migration *run* is negligible against the existing
  nightly rotation.

### 4.4 Relationship to task #332

`health_check.check_db_integrity()` (#332) is the *ongoing* daily
`PRAGMA quick_check`, and `check_backup_recent()` confirms a recent snapshot
exists. Neither is a migration gate and neither should become one. The
migration subsystem borrows #332's **check** (Section 7, item E) and
`backup.py`'s **mechanism** (this section), and adds the one thing neither
has: a fail-closed pre-condition tied to a specific about-to-happen change.

---

## 5. Reusing the existing connection semantics

The runner uses `workgraph_store._connect()` unchanged. This matters more
than it sounds:

- It carries the retry/backoff loop that a real, reproducible Windows
  file-lock bug required (lines 52-89). A fresh `sqlite3.connect()` in the
  runner would silently reintroduce that bug on the highest-stakes code path
  in the system.
- It honors the `ws_db` fixture's monkeypatch, so migration tests get
  isolation for free.
- It sets `isolation_level=None`, meaning **the runner must issue explicit
  `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`** — Python's implicit
  transaction management is off. This is already the established pattern in
  every S6/S7 block.

---

## 6. Transactional migrations: what genuinely can and cannot be atomic

This section is deliberately conservative and specific. An aspirational
answer here would be worse than a modest one.

### 6.1 The good news, stated precisely

**SQLite's DDL is transactional.** `CREATE TABLE`, `DROP TABLE`,
`ALTER TABLE`, `CREATE INDEX`, `DROP INDEX`, `CREATE VIEW`, `DROP VIEW`,
`CREATE TRIGGER`, `DROP TRIGGER` all participate in the enclosing
transaction and all roll back. This is a real difference from MySQL and
older Oracle, and it is the reason this design can be genuinely
all-or-nothing where most migration frameworks cannot.

This is not a claim from documentation alone — this codebase has already
**proven it live**. `tests/test_workgraph_store.py::test_alerts_migration_is_crash_safe`
(lines 618-685) injects a real exception immediately after
`ALTER TABLE alerts RENAME TO alerts_pre_task55` inside the transaction, and
asserts that the original table, the original row, and the *absence* of any
orphan `alerts_pre_task55` all survive. That test is the empirical proof
that this design's core assumption holds on this machine, on this SQLite
build.

Concretely, **every shape in the Section 1 inventory — S1 through S11 — can
be wrapped in a single transaction.** That includes:

- `CREATE VIRTUAL TABLE ... USING fts5` (S4). FTS5 creates ordinary shadow
  tables; there is nothing non-transactional about it.
- The Python-looping data repairs (S10). `INSERT`/`UPDATE`/`DELETE` inside a
  Python loop is still just statements in the open transaction.
- `UPDATE sqlite_sequence` (needed in Section 10).
- `PRAGMA defer_foreign_keys=ON` and `PRAGMA legacy_alter_table=ON` — both
  *are* settable inside a transaction.

So the honest headline is: **the current code's non-atomicity is a code
choice, not a SQLite limitation.** ~135 statements run as separate
autocommit statements. Wrapping them is available, and the historical
evidence that it matters is on the record — the #61 adversarial review found
that the pre-transaction version of the alerts migration could leave real
data orphaned under `alerts_pre_task55` behind a fresh, empty `alerts` table
whose schema *already satisfied the "already migrated" check*, so nothing
would ever retry it (lines 547-563). That is a silent, permanent,
undetectable data loss path that existed in production.

### 6.2 The real limits: what cannot be inside a transaction

These are genuine, and a migration needing any of them must declare
`TRANSACTIONAL = False`:

| Operation | Behavior inside a transaction | Consequence for this design |
|---|---|---|
| `PRAGMA journal_mode = WAL\|DELETE` | Cannot change; the pragma returns the current mode and the change silently does not happen | No migration may change journal mode. `_connect()` sets WAL per-connection already. #157 explicitly ruled out journal-mode changes anyway. |
| `VACUUM` | Hard error: cannot VACUUM from within a transaction | Reclaiming space after a large rebuild (Section 10 rebuilds ~8,700 + ~5,900 + ~5,300 + ~5,200 rows) must be a **separate, post-commit, non-atomic step**, or simply skipped. Recommendation: skip it in v1. Space is not the problem; correctness is. |
| `PRAGMA foreign_keys = ON\|OFF` | Documented **no-op** inside a transaction | The SQLite docs' canonical 12-step `ALTER TABLE` procedure puts steps 1 and 12 (disable/re-enable FKs) deliberately *outside* the transaction for exactly this reason. **Today this is moot**: FK enforcement has never been on in this codebase, so steps 1 and 12 are no-ops and the 12-step collapses to steps 2-9 + 11, all inside one transaction. This is a genuine simplification worth stating plainly — and it is also a reason not to enable enforcement casually later (Section 11). |
| `ATTACH` / `DETACH` | Cannot run inside a transaction | Rules out any migration that copies through a second database file. Not needed by anything currently foreseen. |
| `PRAGMA page_size`, `PRAGMA auto_vacuum` changes | Require a subsequent `VACUUM` to take effect | Same conclusion as `VACUUM`. Out of scope. |
| `PRAGMA wal_checkpoint(TRUNCATE)` | Not meaningful mid-transaction | Not needed. |
| **The pre-migration backup** | Requires a *separate connection* | Inherently outside the transaction, by construction. See 6.3 for why that ordering is still safe. |

### 6.3 The rename-order hazard — the most important operational limit

This is not a transactionality limit; it is a correctness limit that has
already caused two production incidents, and the design must encode the fix.

Since SQLite 3.25, `ALTER TABLE x RENAME TO y` **also rewrites every other
reference to `x` in the stored schema** — view bodies, trigger bodies, and
`REFERENCES` clauses in other tables' DDL. That single behavior is the
mechanism behind:

- **All 19 stale FK clauses.** `ALTER TABLE work_objects RENAME TO work_objects_pre_fix4`
  (line 2247) rewrote six child tables' `REFERENCES work_objects(id)` into
  `REFERENCES "work_objects_pre_fix4"(id)`, and the earlier #114 rename did
  the same for eleven more. Verified live — the stored DDL now reads
  `REFERENCES "issues_pre_workobjects"(id)` in `claims`, quoted exactly as
  SQLite's rewriter emits it.
- **Incident #310.** The same rename silently rewrote the *existing*
  `projects` view and its three triggers to reference
  `work_objects_pre_fix4`; `CREATE VIEW IF NOT EXISTS` then saw them as
  present and never regenerated them, so `list_projects`/`get_project`
  raised `no such table: main.work_objects_pre_fix4` on **every call** for
  the entire window between Fix 4 landing and the repair (lines 2414-2431).

The framework's rule, therefore:

> **Table rebuilds MUST follow the SQLite documentation's order:
> create `T__new` → copy → `DROP TABLE T` → `ALTER TABLE T__new RENAME TO T`.
> Never `RENAME T` to a `_pre_*` name first.**

Under that order the rename's rewriting behavior is harmless and in fact
helpful: nothing else in the schema references `T__new`, so nothing gets
rewritten, and `T`'s name is restored intact. Under the current
rename-old-first order, every rebuild is a fossil generator. All ten S6
blocks use the wrong order.

A second, subtler consequence of the same 3.25 behavior: `ALTER TABLE ...
RENAME` must parse the *entire* schema, and **fails if any existing view or
trigger references a missing table**. So a single broken view can block an
unrelated table's rebuild. During the #310 window, that was the live state.
The auditor therefore includes an explicit view-parseability pre-flight
(Section 7, item D). Verified live today: all three views (`issues`,
`projects`, `evidence`) parse cleanly, so there is no such blocker right
now — but there was, for a real and extended period.

`PRAGMA legacy_alter_table=ON` would suppress the rewriting entirely and is
settable inside a transaction. It is **not** recommended here: under the
correct rename order it is unnecessary, and turning it on would suppress
rewriting that is sometimes desirable. Named so the option is on the record,
not adopted.

### 6.4 What a non-transactional migration looks like

If a migration genuinely needs `VACUUM` or an `ATTACH`, it declares
`TRANSACTIONAL = False` and the runner:

1. Commits a `state='in_progress'` ledger row **first**, with `backup_ref`
   already populated.
2. Runs the work outside any transaction.
3. Flips the row to `applied` on success.

A crash leaves a visible `in_progress` row, and the next run **refuses to
continue** and reports it with the backup path. That is the honest contract:
non-transactional migrations are not crash-safe, and the design's answer is
detectability plus a known-good snapshot, not a false claim of atomicity.

**Recommendation: no v1 migration should need this.** `0002` and `0003`
(Section 10) are both fully transactional.

### 6.5 What the transaction does *not* protect against

Stated so the design isn't oversold:

- **File-level corruption.** A transaction guarantees logical atomicity, not
  that the file survives a bad sector or an antivirus process mangling the
  WAL. That is what the Section 4 backup is for, and it is why the backup is
  a hard gate rather than a nice-to-have.
- **A logically wrong but successfully-committed migration.** A migration
  that faithfully copies the wrong columns commits cleanly. That is what
  Section 7's audit and Section 8's tests are for.
- **The whole run.** One transaction per migration means a 3-migration run
  is 3 transactions. If `0003` fails, `0002` stays applied. This is
  deliberate — one giant transaction would hold the write lock across the
  entire run against a DB with 11 live entry points, and would roll back
  good work because of unrelated bad work. The ledger makes partial progress
  legible, which is the right trade.

---

## 7. Post-migration integrity audit

The starting menu, borrowing directly from the #365 audit's own approach
(which I re-ran live in Section 0 — so these are checks with confirmed
present-day baselines, not guesses).

**Schema-shape checks (cheap, run every time):**

- **A. FK-target resolution.** Parse every `REFERENCES <target>(<col>)` out
  of `sqlite_master`'s stored DDL and assert each target exists **as a
  table** — not as a view, not as a missing name. Must handle SQLite's
  quoted rewrite form (`REFERENCES "issues_pre_workobjects"(id)`); an
  unquoted-only regex silently reports zero problems on this exact database,
  which is a mistake I made and corrected while researching this. Baseline
  today: **43 FK columns, 19 failing** (6 nonexistent target, 2 view target,
  11 empty-snapshot target). Target after `0002`: **0 failing**.
- **B. Orphan rows against the *intended* parent.** For each child column,
  count rows whose value has no match in the parent. Critically, run this
  against the parent the column *should* reference, not only the one it
  declares — otherwise the 19 broken columns produce ~20,700 meaningless
  "orphans," which is exactly the confusion #365 had to work through.
  Baseline today: **0 orphans across all 11 work-object-referencing
  columns.** This makes zero-orphans a real, currently-true invariant worth
  asserting rather than an aspiration.
- **C. Stale-DDL-reference scan.** No view or trigger body may mention a
  `_pre_*` table or any nonexistent table. This generalizes the #310
  detector at lines 2465-2471 from the hardcoded `%work_objects_pre_%`
  pattern to "any unresolvable name."
- **D. View parseability.** `SELECT * FROM "<view>" LIMIT 0` for every view.
  Catches the #310 class *before* it blocks an unrelated `ALTER TABLE
  RENAME` (Section 6.3). Cheap, and it is the check whose absence let #310
  run undetected for hundreds of real failures.

**Rebuild-conservation checks (run when a migration rebuilt a table):**

- **G. Row-count conservation.** Count captured in `precheck`, re-asserted
  after the copy, **inside the same transaction**, so a mismatch rolls back
  rather than reporting after the fact.
- **G2. `sqlite_sequence` watermark preservation.** Not theoretical: live
  `data_point_values` has `seq=9935` against only 5,238 rows, and
  `work_object_relationships` has `seq=83` against 39 rows. A naive
  create-copy-drop-rename resets the watermark to `MAX(id)`, so a rebuilt
  table would **re-issue ids that were already used and deleted**. Capture
  the old `seq`, restore it if it exceeds the new `MAX(id)`.
- **H. Index and trigger reconstruction.** The set of index and trigger
  names attached to a rebuilt table must equal the pre-rebuild set. Live
  example of why this is not paranoia: `work_object_relationships` carries
  four indexes (`idx_work_object_relationships_from`, `..._to`,
  `..._pending`, `idx_wor_type`), of which **only `idx_wor_type` appears in
  `workgraph_store.py`'s source at all**. A rebuild that recreated indexes
  from the source text would silently drop three real production indexes.

**Physical-integrity checks:**

- **E. `PRAGMA quick_check`.** Reuse `health_check.check_db_integrity()`
  directly rather than re-implementing — it already documents why
  `quick_check` over `integrity_check` for a routine pass, and why it must
  go through `ws._connect()` to honor the test fixture (lines 352-393).
  Escalate to the full `PRAGMA integrity_check` when a migration rebuilt a
  table, since that is the case where index/table cross-verification
  actually earns its cost.
- **F. `PRAGMA foreign_key_check`.** Gated on check A passing. Today it
  **throws `foreign key mismatch`** the moment it reaches a view-targeted FK
  — which is precisely what #365 hit. After `0002`, this becomes runnable
  for the first time in this database's life, and should be run **as a
  report only**, still without enabling enforcement (Section 11).

**Deliberately out of scope for v1:** the ROADMAP's separate "explicit graph
invariants beyond SQL FKs" item (every promoted Issue has a valid Project,
no exclusive reference anchor on two active objects, no `done` claim in an
open-claim index, etc.). Those are real and the same runner could host them,
but they are semantic invariants about Jasper's model, not migration
correctness, and folding them in would make every migration's pass/fail
depend on unrelated data quality. Keep them as a separate periodic pass that
*reuses* `audit.py`'s plumbing.

**Failure posture.** Checks A/B/C/D/G/G2/H run inside the migration's
transaction and a failure **rolls the migration back**. Checks E and F run
after commit and are **report-only** — a `quick_check` failure after a
committed migration is a restore-from-backup decision for a human, not
something the runner should attempt to auto-remediate.

---

## 8. Migration tests from representative older DB snapshots

### 8.1 Build on what already exists

The suite already has the right instincts; they just aren't systematized.
Named explicitly so the new tests extend rather than duplicate:

- **Forge-an-old-schema-then-re-run**: `test_alerts_migration_preserves_existing_rows_from_old_schema`
  (lines 587-616) drops the table, recreates it with the pre-#55 `CHECK`,
  inserts a canary row, calls `init_workgraph()`, and asserts both the
  canary's survival and the new capability. This is the core pattern.
- **Tear-down-the-later-migration-first**: `test_issues_migration_preserves_existing_rows_from_old_schema`
  (lines 698-779) shows how much work forging a genuinely pre-migration
  state takes once views/triggers/`work_objects`/fossil tables exist — six
  `DROP TRIGGER`s, two `DROP VIEW`s, three `DROP TABLE`s. This is direct
  evidence that hand-forging gets expensive fast, and the reason Section 8.2
  favors captured schema dumps.
- **Crash-safety by statement injection**: `test_alerts_migration_is_crash_safe`
  (lines 618-685) subclasses `sqlite3.Connection` and raises at a chosen
  statement. Note its own hard-won detail: it restores `sqlite3.connect`
  directly rather than calling `monkeypatch.undo()`, because undo would also
  revert the fixture's DB redirection and point the verification at the
  wrong database (lines 667-671).
- **Idempotency by repetition**: `test_outlook_ingest.py` lines 609-614 call
  `init_workgraph()` a second and third time.
- **Self-heal plus no-op gate as a pair**: `test_init_workgraph_self_heals_a_rename_corrupted_projects_view`
  and `test_init_workgraph_repair_is_a_noop_when_nothing_is_stale`
  (lines 3546-3630). The second is as important as the first — it is what
  keeps the repair from firing during the concurrency stress test.
- **Data-consolidation migration**: `test_init_workgraph_consolidates_pre_existing_duplicate_relationships`
  (lines 3180-3214).
- **Real cross-process concurrency**: `tests/test_multiprocess_concurrency.py`
  spawns 4 genuine OS processes via `_stress_worker_helper.py`.

### 8.2 How to actually obtain and store representative snapshots

Three tiers, ranked by cost against what each one catches.

**T3 — Schema-only dumps (mandatory baseline; do this first, today).**
A `.schema`-equivalent text dump: `sqlite_master`'s `sql` column for every
object, plus `PRAGMA table_info` and the `sqlite_sequence` watermarks.
Tiny, diffable, git-trackable, and **contains zero business content**, so
there is no privacy or scrubbing question at all. Checked in as
`tests/fixtures/schema_snapshots/<era>.sql`.

> **The single highest-value, lowest-cost action in this whole design:
> capture `tests/fixtures/schema_snapshots/2026-08-12_live.sql` from the
> production DB before any migration code is written.** It costs one
> read-only command. Without it, every later "did we change the live shape?"
> question is unanswerable. It is also the artifact that made the
> `work_object_relationships` drift in Section 9.3 findable at all.

Additional eras are recoverable retroactively: the ten S6 blocks each embed
the *pre*-migration `CREATE TABLE` text verbatim in the rebuild they
perform, and `git log` on `workgraph_store.py` reconstructs the rest.

**T1 — Synthetic forged fixtures (the default for behavior tests).**
Exactly what the suite already does, but promoted from inline SQL inside
each test to a shared `.sql` script per era plus a `db_at_era(name)` fixture
that applies it to a `tmp_path` DB. Deterministic, fast, git-safe, and it
lets one test body run against several eras via parametrization instead of
each test re-forging by hand.

**T2 — Real anonymized production snapshots (opt-in, local only).**
The only tier that catches "the real DB has a shape no forged fixture
predicted" — which is not hypothetical, since that is exactly the class of
the Section 9.3 drift and of the #310 incident. Mechanism:

1. `backup.create_labeled_snapshot("premigration_test_corpus")` — already
   exists, already excluded from pruning.
2. A scrubber that nulls or hashes the free-text columns
   (`raw_items.subject` / `body_preview` / `participants` / `from_actor` /
   `meta_json` / `entry_id`, `raw_item_extractions.extracted_json`,
   `claims.text`, `evidence_units.summary`, `synthesis.*`,
   `attachments.filename` / `stored_path` / `extracted_text`,
   `assistant_chat_turns.text`) while preserving every id, timestamp,
   foreign key, enum value, and the entire schema. Structure is what
   migration tests exercise; prose is not.
3. Stored **outside git** (~98 MB, and even scrubbed it carries real
   business structure), under a path named by an env var. Tests
   **`pytest.mark.skipif`** when absent, so CI and other machines stay
   green. This matches the existing `test_hermetic_platform_safety.py`
   posture and the ROADMAP's own "tests that assume something should say so
   explicitly" item.

### 8.3 What a migration test asserts

For each migration, eight assertions. Items 1-4 are per-migration; 5-8 are
shared framework tests written once and parametrized.

1. **Fresh-install path.** Runs clean on an empty DB.
2. **Era path.** Runs clean against each stored era snapshot, and the
   resulting schema fingerprint **equals the fresh-install fingerprint**,
   modulo an explicitly-enumerated allowlist of accepted differences. The
   allowlist being explicit is the point — an unexplained difference fails
   the test instead of being absorbed.
3. **True no-op on re-run.** Not merely "didn't raise." Assert the ledger
   short-circuits by counting statements executed on a wrapped connection —
   expected zero. The current code's weakness is precisely that "runs again
   harmlessly" and "runs again as a no-op" look identical from the outside.
4. **Data preservation.** Row counts equal before/after for every touched
   table, plus named canary rows surviving with every column intact. The
   #44 bug (lines 210-220) is the cautionary case: a rebuild that dropped
   three columns failed its `INSERT`, was silently swallowed, and rolled
   back forever — every test passed and the migration never applied.
5. **Crash safety, parametrized over every statement boundary.** Generalize
   `test_alerts_migration_is_crash_safe`'s `_CrashingConnection` into a
   shared fixture that raises at statement index *i* for every *i*, and
   assert the schema fingerprint and all row counts are unchanged, with no
   `_new`/`_pre_` leftovers and no ledger row.
6. **Concurrency.** N real processes run the runner against one DB
   simultaneously; assert exactly one ledger row per migration, zero
   exceptions. Extends `test_multiprocess_concurrency.py`'s existing helper.
7. **Backup gating.** Assert a snapshot is created exactly once per run when
   `pending` is non-empty, zero times when it is empty, and that a
   deliberately-failing snapshot **aborts the run with no migration
   applied**. This is the fail-closed posture from Section 4.2, tested.
8. **The auditor itself is tested.** Feed the auditor a deliberately-broken
   migration (drops a column, loses rows, forgets an index, resets the
   `sqlite_sequence` watermark) and assert each check catches it. An
   unexercised auditor is worse than none, because it manufactures
   confidence.

Per the standing "don't overrun the full suite" discipline: iterate on
targeted files, run the full suite once before commit.

---

## 9. Adoption sequencing: reconciling the already-applied ad hoc migrations

This is the hardest real question in the design. Get it wrong and you either
re-run destructive operations against Marc's real history, or you leave
fresh installs with no schema.

### 9.1 Options rejected, with reasons

- **Re-run everything under the new ledger.** Rejected. Several shapes are
  not safely re-runnable against already-migrated data: the ten S6 blocks
  would re-execute `RENAME`-old-first and **manufacture a fresh generation
  of FK fossils**; the S7 blocks would raise `IntegrityError` on primary-key
  collision (which the existing `except OperationalError` does not even
  catch — lines 2109-2113); the relationships consolidation (S10) would
  re-run its `DELETE`s. This is the destructive outcome the task explicitly
  warns against.
- **Mark everything applied without running anything.** Rejected outright: a
  fresh install would then get an empty database, since `init_workgraph()`
  *is* the bootstrap.
- **Hand-split `init_workgraph()` into ~55 individual retroactive
  migrations.** Rejected as the initial step. It is weeks of work, every
  split is a chance to reorder something whose ordering is load-bearing
  (Fix 4's three-`ADD COLUMN` dependency, the `claim_events` case), and it
  buys nothing over 9.2 — the transitions are already in the past and
  cannot be re-observed. Worth doing incrementally, later, only for blocks
  that need to change.

### 9.2 Recommended: freeze `init_workgraph()` as `0001`, baseline by observation

**Step 1 — Move, don't rewrite.** `init_workgraph()`'s body moves
**verbatim** into `migrations/0001_baseline.py`. Not refactored, not
reordered, not "cleaned up." Every comment travels with it — those comments
are the incident record for a dozen real bugs and are load-bearing
documentation. It stays idempotent, stays the fresh-install bootstrap, and
stays proven by ~11 entry points times months of real calls plus the
existing idempotency tests.

**Step 2 — Classify the DB, then baseline accordingly.** On any DB with no
`schema_migrations` table:

- **No user tables at all** → fresh install. Run `0001` normally. Ledger row
  `state='applied'`.
- **Tables already present** → this is a pre-ledger DB (Marc's live one, or
  any older snapshot). Run `0001` once more — it is idempotent — then insert
  the ledger row with **`state='baselined'`** plus the captured schema
  fingerprint. `baselined` honestly records: *we asserted the end state, we
  did not observe the transition.*

**Step 3 — Fingerprint, and this is the actual safety mechanism.** Baselining
is only trustworthy if the assertion is checked rather than assumed. So the
baseline captures a **canonical schema fingerprint** — `sqlite_master`
normalized (sorted by type then name, whitespace collapsed, comments
stripped) and hashed, plus a per-object inventory (columns with types,
defaults, `NOT NULL`, `CHECK` text, PK; index DDL; trigger DDL; view DDL;
`WITHOUT ROWID`; `sqlite_sequence` watermarks) stored as JSON in the ledger
row.

**Step 4 — Produce the drift report, and require review before `0002`.**
Compare the live fingerprint against the fingerprint a fresh `0001` produces
in a temporary DB. Every difference is either:

- (i) a known, accepted historical artifact — the three empty fossil tables,
  the 19 stale FK clauses; or
- (ii) **genuine, previously-invisible drift that must be named before any
  new migration runs.**

This converts "we hope fresh equals live" into a reviewed list, and it is
the deliverable that makes the whole baseline honest rather than a leap of
faith.

### 9.3 The drift report already has a category-(ii) finding

I found one while researching this, which is itself the argument for the
step. Verified live:

`work_object_relationships` **as it exists in production**:

```sql
CREATE TABLE work_object_relationships (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id              TEXT NOT NULL REFERENCES "work_objects_pre_fix4"(id),
    to_id                TEXT NOT NULL REFERENCES "work_objects_pre_fix4"(id),
    relationship_type    TEXT NOT NULL CHECK (relationship_type IN
                             ('candidate','bridge','confirmed','rejected')),
    match_count          INTEGER NOT NULL,
    matched_signals_json TEXT NOT NULL,
    created_ts           REAL NOT NULL,
    resolved_ts          REAL,
    UNIQUE(from_id, to_id)
)
```

plus four indexes: `idx_work_object_relationships_from`, `..._to`,
`..._pending`, `idx_wor_type`.

`workgraph_store.py`'s `CREATE TABLE IF NOT EXISTS` for the same table
(lines 1847-1860) has **no `CHECK` constraint**, `match_count INTEGER NOT
NULL DEFAULT 0`, `matched_signals_json TEXT` (nullable), and **only
`idx_wor_type`**.

The cause is documented in the file's own comment (lines 1837-1846): the
table *"was created live on the real DB but its `CREATE TABLE` was never
captured as a permanent migration here"* — and when it was finally added, a
different, weaker definition was written. Because `CREATE TABLE IF NOT
EXISTS` silently no-ops against the existing table, **production and fresh
installs have had different schemas for this table ever since, invisibly.**

Two direct consequences for this design:

1. **The drift report is not busywork.** It found a real divergence on its
   first pass, in a table that participates in `pipeline2`'s grouping
   bookkeeping.
2. **Section 10's rebuild must be driven by the LIVE stored DDL, never by
   the source's `CREATE TABLE` text.** Rebuilding
   `work_object_relationships` from source would silently drop a `CHECK`
   constraint, weaken two `NOT NULL`s, and delete three production indexes —
   while every test passed, because tests run against the source-derived
   fresh schema. This is the pivotal design decision in the worked example.

### 9.4 A useful accident: the fossil tables are empty

Also verified live: `issues_pre_workobjects`, `projects_pre_workobjects`,
and `evidence_pre_evidenceunits` all contain **0 rows**. So the eleven
stale-snapshot FK clauses point at empty tables, and dropping those tables
loses nothing.

Recorded honestly: I have not established *why* they are empty when the #114
migration should have left the pre-migration rows behind. It does not affect
`0002` (which only rewrites DDL text and never reads them), but `0003`'s
precheck must **re-assert the 0-row condition at run time** rather than
trusting today's observation, and the question is worth a sentence of
explanation before `0003` ships.

### 9.5 Sequencing, and the deliberate order of "freeze" before "stop running"

1. Capture the T3 schema dump of the live DB (Section 8.2). One command.
   Do this before writing any code.
2. Build `runner.py` + `audit.py` + `fingerprint.py` + the ledger, with
   `0001` as the only migration. `init_workgraph()` becomes a thin
   `runner.run()` shim so all 11 call sites keep working unchanged.
3. Run against the live DB. It baselines, produces the drift report, and
   changes nothing else. **This is a genuinely reversible checkpoint** — a
   new table and a `user_version` bump, nothing more.
4. Review the drift report with Marc. Resolve each category-(ii) item as
   either "live is correct, fix the source" or "source is correct, write a
   migration." The `work_object_relationships` finding is the first entry.
5. **Freeze `0001`.** Hard rule from here: no new statement is ever added to
   it. Every schema change is a new numbered migration.
6. `0002` (FK-target rewrite) and `0003` (drop empty fossils).
7. **Only much later**, once the ledger has proven itself over weeks of real
   runs, replace `0001`'s per-startup execution with a pure ledger check.
   Freeze first, stop-running later — deliberately two separate steps, so a
   ledger bug cannot leave a fresh install unbootstrapped. Until then,
   `0001` keeps running every startup exactly as `init_workgraph()` does
   today, which means step 2 changes behavior for nobody.

Note that this sequencing respects the ROADMAP's standing guardrail: nothing
here touches `find_candidates`, `_matched_data_points`, the `>= 2`
threshold, or `judge_candidates`. It is pure persistence-layer work. The one
adjacency worth flagging is that `0002` rebuilds
`work_object_relationships`, which `pipeline2` writes — so the
before/after index-set assertion (check H) is doing real work there.

---

## 10. Worked example: `0002_fk_target_rewrite`

The #365 finding expressed as a migration under this subsystem. Real table
and column names throughout, from the live re-audit.

### 10.1 The 19 columns across 17 tables

**Group 1 — target does not exist at all (6 columns, 5 tables).**
`REFERENCES "work_objects_pre_fix4"(id)` → must become `work_objects(id)`:

| Table | Column | Rows |
|---|---|---|
| `work_object_relationships` | `from_id` | 39 |
| `work_object_relationships` | `to_id` | 39 |
| `work_object_signatures` | `work_object_id` | 5,891 |
| `artifact_lineages` | `work_object_id` | 176 |
| `evidence_unit_links` | `work_object_id` | 5,255 |
| `data_point_values` | `work_object_id` | 5,238 |

**Group 2 — target is a VIEW (2 columns, 2 tables).**
`REFERENCES issues(id)` → must become `work_objects(id)`:

| Table | Column | Rows |
|---|---|---|
| `nba_outcome_log` | `issue_id` | 0 |
| `pending_issue_state_suggestions` | `issue_id` | 6 |

**Group 3 — target is an empty renamed-away snapshot (11 columns, 10 tables).**
`REFERENCES "issues_pre_workobjects"(id)` / `REFERENCES "projects_pre_workobjects"(id)`
→ must become `work_objects(id)`:

| Table | Column | Rows |
|---|---|---|
| `claims` | `issue_id` | 8,717 |
| `issue_state_history` | `issue_id` | 4,683 |
| `issue_parties` | `issue_id` | 4,607 |
| `source_containers` | `issue_id` | 1,379 |
| `identity_anchors` | `issue_id` | 1,304 |
| `checklist_dismissals` | `issue_id` | 7 |
| `nba_choice_log` | `issue_id` | 7 |
| `work_tasks` | `issue_id` | 0 |
| `lessons` | `source_issue_id` | 0 |
| `project_links` | `from_project_id` | 0 |
| `project_links` | `to_project_id` | 0 |

(Three further stale clauses live *on* the fossil tables themselves —
`issues_pre_workobjects.project_id`, `issues_pre_workobjects.lesson_id_cited`,
`evidence_pre_evidenceunits.issue_id`. They are handled by `0003` dropping
those tables, not by rewriting them.)

### 10.2 `precheck` — fail closed, capture everything

```
def precheck(conn) -> dict:
```

Assertions, any failure aborting before a single write:

1. Every one of the 19 (table, column, declared_target) triples matches
   expectation exactly. If the live DDL has drifted from this list, **stop**
   — do not improvise.
2. Every declared target is in the known set
   `{work_objects_pre_fix4, issues_pre_workobjects, projects_pre_workobjects}`
   or is the `issues` view. Nothing else is rewritten.
3. **Zero orphans against `work_objects`** for all 19 columns. Verified 0
   today for all 11 sampled; the precheck makes it a hard gate. If it is
   ever non-zero, this migration must not run — that would be a genuine data
   problem needing its own decision, not a schema rewrite.
4. All views parse (`SELECT * FROM "<view>" LIMIT 0`) — the Section 6.3
   pre-flight, because a broken view blocks `ALTER TABLE RENAME`.
5. `PRAGMA quick_check` is `ok`.

Captured and returned for later comparison: per table, the exact stored
`sql`; every index and trigger DDL (`sqlite_master WHERE tbl_name = T AND
type != 'table'`); the row count; and the `sqlite_sequence.seq` watermark if
present.

### 10.3 `apply` — one generic rebuild, driven by live DDL

The whole migration is one loop over 17 tables. Per table `T`:

```
1. ddl = captured[T]["sql"]                       # the LIVE stored DDL
2. new_ddl = ddl with, textually:
      REFERENCES "work_objects_pre_fix4"(id)     -> REFERENCES work_objects(id)
      REFERENCES "issues_pre_workobjects"(id)    -> REFERENCES work_objects(id)
      REFERENCES "projects_pre_workobjects"(id)  -> REFERENCES work_objects(id)
      REFERENCES issues(id)                      -> REFERENCES work_objects(id)
   and the CREATE TABLE's own name T -> T__new.  Nothing else changes.
3. conn.execute(new_ddl)                          # CREATE TABLE T__new (...)
4. conn.execute(f'INSERT INTO "T__new" SELECT * FROM "T"')
5. conn.execute(f'DROP TABLE "T"')
6. conn.execute(f'ALTER TABLE "T__new" RENAME TO "T"')     # docs' order
7. for each captured index/trigger DDL: conn.execute(it)
8. if captured seq is not None and seq > new MAX(rowid):
       UPDATE sqlite_sequence SET seq = <captured> WHERE name = 'T'
9. assert COUNT(*) == captured row count           # inside the transaction
10. assert index/trigger name set == captured set
```

All 17 tables in **one** `BEGIN IMMEDIATE ... COMMIT`. `TRANSACTIONAL = True`.

Six deliberate decisions in that loop, each grounded in something verified:

- **Step 2 substitutes text into the LIVE DDL; it never retypes the source
  DDL.** This is the decision Section 9.3 forces. It automatically preserves
  `work_object_relationships`' `CHECK` constraint and `NOT NULL`s,
  `evidence_unit_links`' `WITHOUT ROWID` and composite `PRIMARY KEY`,
  `claims`' `AUTOINCREMENT` and its two trailing `ALTER`-added columns
  (`completion_contract`, `canonical_key`), and every `DEFAULT` — verbatim,
  without anyone having to enumerate them. It is also why `SELECT *` in step
  4 is safe: column order is identical by construction. The ten S6 blocks
  retype their DDL by hand, and that is exactly where the #44 dropped-column
  bug came from.
- **Steps 5-6 use the SQLite documentation's order**, not this codebase's
  historical rename-old-first order. Per Section 6.3, rename-old-first is
  what created these fossils; repeating it would create the next generation.
  Under this order nothing else in the schema references `T__new`, so the
  3.25 rewriting behavior is inert.
- **Step 7 recreates indexes and triggers from the CAPTURED DDL**, not from
  source. Without this, `work_object_relationships` silently loses three
  production indexes. Their DDL names `T`, which step 6 has just restored,
  so they apply cleanly.
- **Step 8 preserves the `AUTOINCREMENT` watermark.** Concretely required:
  `data_point_values` (`seq=9935`, 5,238 rows) and
  `work_object_relationships` (`seq=83`, 39 rows) would otherwise re-issue
  already-used ids.
- **Steps 9-10 assert inside the transaction**, so a mismatch rolls back
  rather than reporting after the damage is committed.
- **`PRAGMA foreign_keys` is never touched.** It has never been on, so the
  12-step procedure's steps 1 and 12 are no-ops here (Section 6.2), and
  leaving it off is the standing decision from #157 and #365.

### 10.4 `audit` — post-migration

Inside the transaction: checks A, B, C, D, G, G2, H from Section 7. Check A
is the headline — **43 FK columns, 0 failing**, down from 19 failing.

After commit, report-only: full `PRAGMA integrity_check` (escalated from
`quick_check` because tables were rebuilt), and — for the first time in this
database's life — `PRAGMA foreign_key_check` as a **report**. Enforcement
stays off.

### 10.5 `0003_drop_empty_fossil_tables`, kept separate

`DROP TABLE issues_pre_workobjects`, `projects_pre_workobjects`,
`evidence_pre_evidenceunits`, gated on a `precheck` that re-asserts **0 rows
in each** at run time (Section 9.4). Separate from `0002` on purpose: `0002`
is a pure DDL-text repair with no data loss possible even if it is wrong,
whereas `0003` is the one genuinely irreversible-without-restore step. They
should be reviewable, and revertible, independently.

### 10.6 Also to be updated, not left contradicting

`docs/design/SCHEMA_FK_DEBT.md` states (line 71-73): *"There is no dangling
reference to a renamed-away or nonexistent table. The debt is specifically
'declared but never validated,' not 'declared incorrectly.'"* That was true
as written on 2026-08-04 and is **now false** — there are 19. The `0002`
work should update that document rather than leave two design docs in
contradiction. `ROADMAP.md`'s "17 columns across ~15 tables" should likewise
be corrected to "19 columns across 17 tables."

---

## 11. Relationship to #157's "no Phase 0 rebuild" decision

Task #157 concluded: document as debt only, no Phase 0 rebuild, no WAL
change. Its stated reasons (`SCHEMA_FK_DEBT.md` lines 76-83) were that
enabling `PRAGMA foreign_keys=ON` without first auditing every rename-based
migration risks breaking the *next* such migration in a way harder to
diagnose than the status quo, and that a silently unenforced constraint is
safer than a half-fixed enforced one.

**This design does not contradict that, and here is the point-by-point:**

- **`PRAGMA foreign_keys` stays OFF throughout.** `0002` fixes the FK
  *declarations*; enabling *enforcement* remains an entirely separate,
  later, explicitly-reviewed decision. #365's standing decision ("do not
  enable `PRAGMA foreign_keys=ON` anywhere, tests or production, until the
  FK-target rewrite happens") is preserved exactly — and `0002` is the
  precondition that decision names, not a violation of it.
- **This is not a "Phase 0 rebuild."** #157 meant a from-scratch schema
  rebuild or re-derivation. `0002` rebuilds 17 specific tables while
  preserving each one's *live* definition verbatim except for one
  `REFERENCES` target per affected column. No table's semantics, columns,
  constraints, indexes, or data change. It is closer to a text edit applied
  through the only mechanism SQLite offers (SQLite cannot
  `ALTER ... REFERENCES` in place) than to a rebuild.
- **It performs the audit #157 asked for as a prerequisite.**
  `SCHEMA_FK_DEBT.md`'s own "what a future pass would need to do" list is
  step 1 "audit every rename-based rebuild migration for FK safety" and step
  2 "audit for orphaned rows." Section 1 is step 1; Section 7 check B, with
  a verified zero-orphan baseline, is step 2. Step 3 ("only then enable it,
  behind its own explicit review") is deliberately left undone.
- **It changes the calculus in one respect, and that is worth stating.**
  #157 reasoned from "declared but never validated." The live re-audit shows
  the stronger fact that **most of the FK graph points at names that no
  longer resolve to anything real** — 19 of 43 columns. #157's conclusion
  (don't enable enforcement) becomes *more* right, not less. But its
  implicit corollary — that the declarations are harmless documentation —
  does not survive: 19 of them are actively *misleading* documentation,
  naming parents that are empty or nonexistent. `ROADMAP.md` reached the
  same conclusion independently ("the actual fix is a schema rewrite, not a
  data repair") and explicitly folded it into this subsystem's scope. So
  `0002` is executing a decision already recorded, with the scaffolding that
  decision said it required.
- **The one thing #157 warned about that this design must keep honoring:**
  "don't break the NEXT rename-based migration." Section 6.3's mandatory
  rename order is precisely that guarantee, and it is a strict improvement
  over the ten existing S6 blocks.

---

## 12. Open questions for Marc

1. **What does `baselined` license?** Baselining asserts the live schema is
   the intended one — but Section 9.3 proves it is not, in at least one
   table. For each drift item, does the *live* shape or the *source* shape
   win? That is a judgment call per item (the
   `work_object_relationships` `CHECK` constraint looks like live-is-better;
   three undocumented indexes look like source-should-be-fixed), and the
   framework cannot decide it. **This is the trickiest genuinely-open
   question in the design.**
2. **Down-migrations: recommend not building them.** For a single-user local
   DB with a mandatory gzipped pre-migration snapshot, "restore the
   snapshot" is simpler, more reliable, and more honest than maintaining
   reverse operations that are rarely exercised and can themselves lose
   data. Worth an explicit yes/no rather than a silent omission.
3. **Should `0001` eventually be split into real per-change migrations?**
   Recommended: no, not as a project. Split opportunistically, only when a
   specific block needs to change. Confirm that's acceptable.
4. **Where do T2 anonymized snapshots live, and is the scrub list right?**
   Needs a location outside git and a review of the column list in
   Section 8.2 for anything sensitive that was missed.
5. **Why are the three fossil tables empty?** (Section 9.4.) Doesn't block
   `0002`; should be understood before `0003`.

---

## 13. Effort and risk estimate

Honest, and deliberately not optimistic.

| Piece | Effort | Risk | Notes |
|---|---|---|---|
| T3 schema dump of live DB | minutes | none | Read-only. Do it first regardless of whether the rest proceeds. |
| `runner.py` + ledger + backup hook | ~1.5 sessions | **low** | Small surface. `_connect()`, `BEGIN IMMEDIATE`, and the post-lock re-check are all established patterns in this file. |
| `audit.py` (checks A-H) | ~1 session | **low** | Every check has a verified present-day baseline from Section 0, so "correct" is testable rather than assumed. |
| `fingerprint.py` + drift report | ~1 session | **medium** | The code is easy; the *output* is unbounded until produced. Section 9.3 found one drift item in a single spot-check, so budget real time for resolving the list. |
| `0001` baseline move + shim | ~0.5 session | **low** | Verbatim move. The risk is a careless "tidy-up" during the move — the mitigation is a strict no-edit rule and a fingerprint equality test. |
| Test scaffolding (fixtures, crash-injection, concurrency, backup gating, auditor-tests) | ~1.5 sessions | **low-medium** | Extends existing patterns rather than inventing. The parametrized crash-injection fixture is the fiddliest part. |
| `0002` FK rewrite + tests | ~1.5 sessions | **low-medium** | Mechanical text substitution over 17 tables, and *all* of the usual unknowns are already resolved: 0 orphans, empty fossils, live DDL captured, watermark hazards identified, index drift identified. The residual risk is a table whose live DDL has a shape the substitution mishandles — which the precheck's exact-match assertion is designed to stop rather than improvise around. |
| `0003` fossil drop | ~0.25 session | **low** | Gated on a re-asserted 0-row check. Irreversible without restore, hence separate. |
| **Total** | **~7 focused sessions** | **medium overall** | Dominated by the drift report's unknown size, not by the code. |

**Why the overall risk is medium rather than high:** the pre-migration
backup is a hard gate, the migration is transactional (proven on this
machine by an existing test), the data is already clean against the
intended parent, and the fossil tables are empty. **Why it is not low:** it
touches `claims` (8,717 rows), `work_object_signatures` (5,891),
`evidence_unit_links` (5,255), and `data_point_values` (5,238) — the
accumulated history Marc named as the thing that must not be lost. That is
the right reason to build the ledger, backup, and audit *before* the repair
rather than doing the repair as a one-off, which is exactly what
`ROADMAP.md` already concluded.

**Highest-leverage single action if nothing else is built:** capture the T3
schema dump (Section 8.2) and produce the drift report (Section 9.2 step 4).
Together they are well under a session, are entirely read-only, and convert
the largest unknown in this whole area — *"is the live schema what we think
it is?"* — into a reviewed list. The answer, on the evidence so far, is
partly no.
