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
