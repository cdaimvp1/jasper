# Schema debt: declared but unenforced foreign keys

**Status:** documentation only (task #157), per the architecture-review
follow-up's explicit instruction - "document as debt only, no Phase 0
rebuild, no WAL change." No live code or schema changed by this note.

## The finding

`workgraph_store.py`'s schema uses `REFERENCES` on dozens of columns
(`issues`, `claims`, `work_objects`, `raw_items`, `projects`, and others -
39 `REFERENCES` clauses across 13 distinct target tables as of
2026-08-04). None of them are actually
enforced, because `_connect()` never sets `PRAGMA foreign_keys = ON`:

```python
def _connect():
    conn = sqlite3.connect(WORKGRAPH_DB, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    ...
```

SQLite's own default is FK enforcement **off** unless a connection
explicitly turns it on - and every connection this codebase ever opens
does not. Every `REFERENCES` clause in the schema is therefore
documentation of intent, not a constraint the database has ever actually
checked. This has been true since the very first schema (`init_workgraph`
predates this finding) - it is not a regression from tonight's work.

## Why this specifically matters now, not hypothetically

Several real migrations in this file already exploit that non-enforcement,
whether anyone realized it at the time or not. The CHECK-widening rebuild
pattern used repeatedly in this file (`issues.state` at task #44 and again
for the `work_objects` migration, `alerts.kind` at task #55, `pending_
project_suggestions.status` at Phase 0/D2) does this, inside one
transaction, to the exact tables that carry the *most* incoming FK
references in the whole schema:

```sql
ALTER TABLE issues RENAME TO issues_pre_task44;
CREATE TABLE issues (...);              -- fresh table, widened CHECK
INSERT INTO issues (...) SELECT ... FROM issues_pre_task44;
DROP TABLE issues_pre_task44;
```

`issues` is referenced by 11 different FK columns across the schema
(`claims.issue_id`, `raw_items.issue_id`, `pending_project_suggestions.
issue_id_a/b`, `identity_constraints.subject_a/b`, and others). Dropping
and recreating it mid-transaction is exactly the operation SQLite's FK
enforcement is designed to block *by default* when other rows still
reference it - it only works today because nothing has ever asked SQLite
to check. This is not a bug in those migrations: `rowid`/`id` values are
preserved by the explicit column-list `INSERT ... SELECT`, so the actual
*data* stays internally consistent through the rebuild. But it means the
codebase already has a working history of table-rebuild migrations that
were never written with FK enforcement in mind, and would need real
changes (wrapping the rebuild in `PRAGMA foreign_keys=OFF` / `PRAGMA
defer_foreign_keys=ON`, or re-deriving referencing rows) before `PRAGMA
foreign_keys=ON` could ever be turned on safely.

## What was checked, to confirm this is real debt and not a typo

Every `REFERENCES <table>` target in `workgraph_store.py` was cross-checked
against a real `CREATE TABLE` for that name - all 13 distinct target
tables (`issues`, `claims`, `work_objects`, `raw_items`, `projects`,
`lessons`, `source_containers`, `pending_actions`, `parties`, `evidence_
units`, `attachments`, `artifact_versions`, `artifact_lineages`) exist.
There is no dangling reference to a renamed-away or nonexistent table.
The debt is specifically "declared but never validated," not "declared
incorrectly."

## Why this is deliberately NOT being fixed now

Per the architecture review's explicit instruction: no Phase 0 rebuild,
no WAL/journal-mode change. Turning on `PRAGMA foreign_keys=ON` today,
without first auditing every rename-based migration in this file for
FK-safety, risks breaking the NEXT such migration (or a future one) in a
way that's much harder to diagnose than the status quo - a silently
unenforced constraint is safer than a half-fixed enforced one. This is a
real, known gap, not an oversight to quietly work around.

## What a future pass would need to do before enabling enforcement

1. Set `PRAGMA foreign_keys = ON` in a **test** connection first, run the
   full test suite, and fix every rename-based rebuild migration to wrap
   its `BEGIN...COMMIT` in `PRAGMA defer_foreign_keys=ON` (SQLite defers
   FK checks to commit time within a transaction, which the rename/
   create/copy/drop dance needs) or explicitly disable/re-enable FK
   checks around it.
2. Audit for orphaned rows that already exist under the unenforced
   regime (a `raw_items.issue_id` pointing at a deleted issue, etc.) -
   turning enforcement on would immediately surface these as constraint
   violations on the next write to that row, not at migration time.
3. Only then enable it in production, behind its own explicit review -
   not bundled with unrelated feature work.

None of this is scheduled. This note exists so the gap is visible instead
of silent.

---

## Re-measured 2026-08-21, while sizing task #378 — and the finding changed

Task #365 is recorded as *"FK/invariant audit + repair, then enable
`PRAGMA foreign_keys=ON`."* **The live state does not match the second half of
that title, and it cannot.** Measured directly:

```
PRAGMA foreign_keys                     -> 0     (never set; _connect() sets
                                                  busy_timeout, journal_mode,
                                                  synchronous, and nothing else)
PRAGMA foreign_key_check                -> RAISES
    sqlite3.OperationalError: foreign key mismatch -
    "nba_outcome_log" referencing "issues"
```

`git log -S foreign_keys -- workgraph_store.py` returns one commit, and it is
#370's, not #365's. So the pragma was never turned on.

### Why it cannot simply be turned on

`issues` and `projects` are **VIEWS**, not tables — the corrected-pipeline
migration made `work_objects` the real table and left `issues`/`projects` as
views over it:

```
issues        -> view
projects      -> view
work_objects  -> table
```

SQLite cannot enforce a foreign key whose parent is a view. Two tables still
declare `REFERENCES issues(...)`, so `foreign_key_check` aborts on a schema
mismatch before it ever examines a single row. This is not a data-quality
problem that an audit-and-repair pass can clear; it is a structural one.

### What this means for #378 (formalize a schema-migration subsystem)

#378's stated justification is the FK-target debt. But a migration subsystem is
a better way to *run* schema changes — it does not decide *what* the schema
should be, and it would not have prevented or fixed this. The real question
underneath the debt is a design decision nobody has made yet:

  **Option A** — re-point the 2 `REFERENCES issues(...)` declarations at
  `work_objects(id)`, then `foreign_key_check` can actually run and
  `PRAGMA foreign_keys=ON` becomes reachable. Small, concrete, and it makes the
  remaining FK declarations honest.
  **Option B** — accept permanently that declared FKs are documentation, drop
  the `REFERENCES` clauses that point at views so the schema stops asserting
  something untrue, and keep integrity where it already lives (procedural
  checks + the #365 invariant audit).

Either is defensible. Neither needs a migration subsystem. **Recommendation:
decide A vs B before building #378**, because if the answer is B then #378 has
lost its stated customer, and if it is A then #378 is not on the critical path
to it either.

Recorded rather than acted on: changing FK declarations touches schema shape
across two tables and is Marc's call, not a cleanup to slip into an unrelated
sweep.

### CORRECTION, same day (2026-08-21): the "62k orphans" are not orphans

The section above stopped too early and I recorded a wrong conclusion in commit
`d215b02` ("neither option needs a migration subsystem"). Carrying the
measurement further reverses it.

Per-table `PRAGMA foreign_key_check` (the global form aborts on the 2 view
declarations, the per-table form does not) reports violations in **12 tables,
~61,900 rows**. That number looks alarming and is almost entirely meaningless
as a data-quality signal. Checked directly with LEFT JOINs against the real
parents:

```
data_point_values rows with a definition_id missing from definitions : 0
claims rows with raw_item_id missing from raw_items                  : 0
issue_parties rows with party_id missing from parties                : 0
```

**Zero dangling references to real data.** The violations come from the FK
*declarations*, not the rows. Reading the actual check output rather than the
count:

```
PRAGMA foreign_key_check(data_point_values)
  -> ('data_point_values', 11, 'work_objects_pre_fix4', 0)
```

`work_objects_pre_fix4` was the backup table from task #339's view/trigger
repair. **It no longer exists**, and five tables still declare foreign keys to
it:

    work_object_relationships · work_object_signatures · artifact_lineages
    evidence_unit_links · data_point_values

Plus the two pointing at the `issues` VIEW (`nba_outcome_log`,
`pending_issue_state_suggestions`). This is exactly the "columns already point
at tables renamed away" debt this file recorded in the first place - now
confirmed live, with the specific dead target named.

### What that does to A vs B, and to #378

The earlier framing said neither option needs a migration subsystem. **That was
wrong.** SQLite cannot `ALTER` a foreign key: changing one requires the 12-step
table rebuild (create replacement, copy rows, drop original, rename, recreate
indexes and triggers). Option A means doing that for **seven-plus tables
carrying real production data**, in the right order, idempotently, resumably.

That is precisely what task #378 exists to provide. So #378 does have a real,
concrete customer, and it is this.

And the payoff is now worth having, which it would not have been if the orphans
were real: because there are **zero** genuine dangling references, once the
declarations point at `work_objects`, `PRAGMA foreign_keys = ON` would actually
hold rather than immediately rejecting writes. Enforcement becomes reachable
*and* safe, in that order.

**Revised recommendation: A, sequenced through #378.**
  1. #378 first, scoped narrowly to "rebuild a table to change its FK
     declarations, safely and resumably" - not a general-purpose framework.
  2. Re-point the 5 `work_objects_pre_fix4` declarations and the 2 `issues`
     declarations at `work_objects(id)`.
  3. Re-run per-table `foreign_key_check`; expect zero violations.
  4. Only then enable `PRAGMA foreign_keys = ON` in `_connect()`.
Each step is independently verifiable and step 4 is reversible by one line.

Option B (drop the clauses, keep FKs as documentation) remains defensible and
is strictly cheaper, but it forfeits real enforcement on a database holding
contract and supplier data when that enforcement is now demonstrably attainable.
