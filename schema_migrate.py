"""schema_migrate.py - rebuild a table to change its FOREIGN KEY declarations.
Task #378, scoped deliberately narrow.

WHY THIS IS NOT A GENERAL MIGRATION FRAMEWORK
---------------------------------------------
#378 was queued as "formalize a real schema-migration subsystem," which is the
kind of scope that never ships. It has exactly one real customer, found by
measurement on 2026-08-21 (see docs/design/SCHEMA_FK_DEBT.md): seven tables
declare foreign keys to `work_objects_pre_fix4` - task #339's backup table,
which no longer exists - or to `issues`, which is a VIEW. SQLite cannot ALTER a
foreign key, so fixing those needs a full table rebuild each.

So this module does that one job, on purpose:
    rebuild_table_fk_targets(table, {old_target: new_target})

Everything else a "migration subsystem" might have (version tables, up/down
scripts, a DSL) is absent because nothing needs it. `init_workgraph()` already
handles additive DDL fine with CREATE TABLE IF NOT EXISTS / ALTER ADD COLUMN.

WHY THE ORPHANS ARE NOT REAL, AND WHY THAT MATTERS HERE
------------------------------------------------------
`PRAGMA foreign_key_check` reports ~61,900 violations across 12 tables, which
looks like mass corruption and is not. Direct LEFT JOINs against the real
parents return ZERO missing rows. Every violation is the DECLARATION naming a
table that is gone. That is why this rebuild is safe: no row has to be deleted,
altered, or reconciled - only the schema text changes.

THE PROCEDURE
-------------
SQLite's own 12-step recipe (sqlite.org/lang_altertable.html#otherfk), with the
two footguns that recipe warns about handled explicitly:

  * `PRAGMA legacy_alter_table` is set ON for the RENAME. With it OFF (the
    modern default) SQLite "helpfully" rewrites references to the renamed table
    inside other schema objects - which would corrupt the very declarations
    this function exists to control.
  * Views are captured before and re-asserted after. `evidence` is a real view
    over `evidence_unit_links`, so a drop/rename can silently invalidate it.

Every step verifies rather than assumes: row counts before/after must match
exactly, and `foreign_key_check` must come back clean for the rebuilt table
before the transaction commits. Any mismatch rolls back.

USAGE
    python schema_migrate.py --report                  # what needs fixing
    python schema_migrate.py --table nba_outcome_log   # one table, dry run
    python schema_migrate.py --table nba_outcome_log --apply
    python schema_migrate.py --all --apply             # all 7

A backup.create_labeled_snapshot() is a hard prerequisite for --apply and this
module will not run it for you; taking a snapshot is a decision, not a step.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Optional

import workgraph_store as ws

#: The measured work list. Maps a dead FK target to the live table it should
#: name. Both dead targets resolve to work_objects: `work_objects_pre_fix4` was
#: renamed away by #339, and `issues` is a view OVER work_objects.
FK_RETARGETS: dict[str, str] = {
    "work_objects_pre_fix4": "work_objects",   # renamed away by #339; absent
    "issues": "work_objects",                  # a VIEW over work_objects
    "issues_pre_workobjects": "work_objects",  # an EMPTY husk table, 0 rows
}


def find_tables_needing_rebuild() -> list[dict]:
    """Read-only. Which tables declare a FK to a target that is not a real
    table? Derived from sqlite_master, never hardcoded, so this stays honest
    if the schema changes."""
    c = ws._connect()
    real_tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    out = []
    for name, sql in c.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE '%REFERENCES%'"):
        bad = []
        for m in re.finditer(r'REFERENCES\s+"?([A-Za-z_][\w]*)"?', sql or "", re.I):
            target = m.group(1)
            # Two ways a target is wrong: it does not exist, OR it is a known
            # stale leftover that still exists. issues_pre_workobjects is the
            # second kind - an empty 0-row husk from the work_objects
            # migration - and checking only the first kind is why the initial
            # pass missed 28,298 violations across 7 tables.
            if target not in real_tables or target in FK_RETARGETS:
                bad.append(target)
        if bad:
            out.append({
                "table": name,
                "dead_targets": sorted(set(bad)),
                "rows": c.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0],
                "retargetable": all(t in FK_RETARGETS for t in set(bad)),
            })
    return sorted(out, key=lambda d: d["rows"])


def _rewrite_ddl(sql: str, retargets: dict[str, str], new_name: str, old_name: str) -> str:
    """Swap the FK targets and the table's own name. Only these two things -
    every other byte of the original DDL is preserved verbatim, including
    CHECK constraints, defaults, and column order (which the INSERT relies on).
    """
    out = sql
    for old_t, new_t in retargets.items():
        # Match REFERENCES "old"( and REFERENCES old( , quoted or not.
        out = re.sub(r'(REFERENCES\s+)"?' + re.escape(old_t) + r'"?(\s*\()',
                     r'\1"' + new_t + r'"\2', out, flags=re.I)
    # Rename only the CREATE TABLE target, anchored, so a column or CHECK
    # literal that happens to contain the table name is untouched.
    out = re.sub(r'^(\s*CREATE\s+TABLE\s+)"?' + re.escape(old_name) + r'"?',
                 r'\1"' + new_name + r'"', out, count=1, flags=re.I)
    return out


def rebuild_table_fk_targets(table: str, *, apply: bool = False) -> dict:
    """Rebuild ONE table so its FK declarations name live tables.

    Returns a report. With apply=False nothing is written and the rewritten
    DDL is returned for inspection - which is the point: the DDL rewrite is
    the only judgment call here, so it should be readable before it runs.
    """
    c = ws._connect()
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()
    if row is None:
        return {"table": table, "error": "no such table"}
    original_ddl = row[0]

    indexes = [r[0] for r in c.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,))]
    triggers = [r[0] for r in c.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))]
    views_before = {r[0]: r[1] for r in c.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view'")}
    dependent_views = {n: s for n, s in views_before.items() if s and table in s}

    tmp = f"{table}__fkfix"
    new_ddl = _rewrite_ddl(original_ddl, FK_RETARGETS, tmp, table)
    rows_before = c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    report = {
        "table": table,
        "rows": rows_before,
        "indexes": len(indexes),
        "triggers": len(triggers),
        "dependent_views": sorted(dependent_views),
        "ddl_changed": new_ddl.replace(f'"{tmp}"', f'"{table}"') != original_ddl,
        "new_ddl": new_ddl,
        "applied": False,
    }
    if triggers:
        # Measured as zero across all 7 targets. If that ever changes, stop:
        # re-creating trigger bodies correctly is a different problem and this
        # function should not pretend to solve it.
        report["error"] = f"{len(triggers)} trigger(s) on {table} - refusing"
        return report
    if not report["ddl_changed"]:
        report["error"] = "DDL rewrite produced no change - nothing to do"
        return report
    if not apply:
        return report

    try:
        c.execute("PRAGMA legacy_alter_table=ON")
        c.execute("BEGIN")
        c.execute(new_ddl)
        c.execute(f'INSERT INTO "{tmp}" SELECT * FROM "{table}"')
        moved = c.execute(f'SELECT COUNT(*) FROM "{tmp}"').fetchone()[0]
        if moved != rows_before:
            c.execute("ROLLBACK")
            report["error"] = f"row count mismatch: {rows_before} -> {moved}"
            return report
        c.execute(f'DROP TABLE "{table}"')
        c.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
        for idx in indexes:
            c.execute(idx)
        # Views must still exist and still be the same text - a rename with
        # legacy_alter_table OFF would have silently rewritten them.
        for vname, vsql in dependent_views.items():
            now = c.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name=?",
                            (vname,)).fetchone()
            if now is None:
                c.execute("ROLLBACK")
                report["error"] = f"view {vname} disappeared during rebuild"
                return report
            if now[0] != vsql:
                c.execute("ROLLBACK")
                report["error"] = f"view {vname} was rewritten during rebuild"
                return report
        after = c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        if after != rows_before:
            c.execute("ROLLBACK")
            report["error"] = f"post-rename count mismatch: {rows_before} -> {after}"
            return report
        violations = c.execute(f'PRAGMA foreign_key_check("{table}")').fetchall()
        if violations:
            c.execute("ROLLBACK")
            report["error"] = f"{len(violations)} FK violations remain after rebuild"
            return report
        c.execute("COMMIT")
        report["applied"] = True
        report["fk_violations_after"] = 0
    except Exception as e:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        report["error"] = f"{type(e).__name__}: {e}"
    finally:
        c.execute("PRAGMA legacy_alter_table=OFF")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--table")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if a.report or not (a.table or a.all):
        print(json.dumps(find_tables_needing_rebuild(), indent=2))
        sys.exit(0)

    targets = [a.table] if a.table else [d["table"] for d in find_tables_needing_rebuild()
                                         if d["retargetable"]]
    results = []
    for t in targets:
        r = rebuild_table_fk_targets(t, apply=a.apply)
        results.append(r)
        print(f'{t:34} applied={r.get("applied")} rows={r.get("rows")} '
              f'err={r.get("error", "")}')
        if r.get("error") and a.apply:
            print("  STOPPING - fix this before continuing")
            break
    print()
    print(json.dumps([{k: v for k, v in r.items() if k != "new_ddl"} for r in results],
                     indent=2))
