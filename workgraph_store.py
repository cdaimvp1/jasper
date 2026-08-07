"""
workgraph_store.py — SQLite-backed work graph for the Symphony cockpit.

Same connection/WAL shape as bus.py's core (`_connect`), deliberately without
bus.py's dual-write/event-log machinery — that's specific to migrating the
legacy `events` table and has no bearing on this brand-new database.

raw_items      — one row per ingested item (mail/Teams/calendar/SharePoint),
                 pre-classification.
issues         — the curated work graph itself.
evidence       — append-only links from an issue back to its source items
                 (and to worker-generated actions).
ingest_cursors — per-source incremental watermarks.
worker_status  — rich live status a worker reports about itself (the binary
                 active/idle/unknown heartbeat in server_lean.py's
                 _cohorts_feed() is a separate, existing mechanism this
                 supplements, not replaces).
pending_actions — action-bridge requests issued from the cockpit, so the
                 frontend can show request -> in-progress -> done without a
                 push channel.
alerts         — curated, deterministic attention-worthy events surfaced above
                 the issue list (stale waiting/blocked issues, newly-classified
                 high-priority asks, off-channel anomalies, stuck pending
                 actions). See workgraph_alerts.py for the scan logic.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import threading
import time
import zlib
from typing import Any, Optional

from paths import WORKGRAPH_DB, ensure_dirs

_lock = threading.Lock()


class _MigrationAlreadyDone(Exception):
    """Internal sentinel only - never escapes init_workgraph(). Signals
    that a losing process's wait for the work_objects migration lock
    ended AFTER a different process already completed the migration, so
    the loser should cleanly skip the migration body rather than re-run
    it against data that's no longer in its original shape."""


def _connect() -> sqlite3.Connection:
    # Real, reproducible bug found while running the full suite for an
    # unrelated change (E6): with no busy_timeout, even the very first
    # PRAGMA journal_mode=WAL on a freshly-created database file can raise
    # "database is locked" immediately, with zero retry, when 2+ processes
    # race to initialize the same brand-new file - confirmed live via
    # test_multiprocess_concurrency.py failing intermittently with exactly
    # that error at exactly this line.
    #
    # busy_timeout=5000 alone was NOT sufficient - re-verified live with a
    # 5x-repeated run of that same test AFTER adding it, and it still
    # failed once (1/5) with the identical "database is locked" error at
    # this same WAL pragma. On this machine that's consistent with Windows
    # file-lock contention (e.g. AV/EDR scan pauses on a corporate host)
    # occasionally outlasting a single connection's busy_timeout budget,
    # not just an in-process SQLITE_BUSY retry gap. So this now has two
    # layers of defense: SQLite's own internal busy-handler (busy_timeout,
    # handles ordinary short contention without any Python-level retry)
    # plus an outer retry-with-backoff loop here (handles the rarer longer
    # stalls that outlast busy_timeout entirely). Every OTHER lock-
    # contention path in this file already retries by hand (merge_issues_
    # txn's own BEGIN IMMEDIATE loop, etc.) - this was the one gap with no
    # retry at all, because it fires before any of that code runs.
    last_exc: Optional[sqlite3.OperationalError] = None
    for attempt in range(8):
        try:
            conn = sqlite3.connect(WORKGRAPH_DB, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 7:
                raise
            last_exc = exc
            time.sleep(0.1 * (2 ** attempt))
    raise last_exc


def init_workgraph() -> None:
    ensure_dirs()
    with _lock:
        conn = _connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS raw_items (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    source       TEXT NOT NULL,      -- outlook_mail | teams_chat | calendar | sharepoint
                    stable_key   TEXT NOT NULL,       -- conversationId / chat_id+message_id / event_id / sp_item_id
                    thread_key   TEXT NOT NULL,       -- best-known pre-issue grouping key
                    dedupe_key   TEXT NOT NULL,       -- sha256(date|sorted-participants|sourceRef), belt+suspenders vs stable_key
                    occurred_ts  REAL NOT NULL,
                    subject      TEXT,
                    from_actor   TEXT,
                    participants TEXT,                -- JSON list
                    body_preview TEXT,
                    raw_ref      TEXT,                -- path to full content, optional
                    ingested_ts  REAL NOT NULL,
                    issue_id     TEXT,                 -- NULL until curated
                    classified   INTEGER NOT NULL DEFAULT 0,
                    item_class   TEXT,                 -- ACTIONABLE-ASK | WAITING-ON-OTHERS | FYI-EVIDENCE | NOISE
                    direction    TEXT,                 -- inbound | outbound | internal
                    direction_inferred INTEGER NOT NULL DEFAULT 0,
                    topic        TEXT,
                    topic_inferred INTEGER NOT NULL DEFAULT 0,
                    sentiment    TEXT,
                    sentiment_inferred INTEGER NOT NULL DEFAULT 0,
                    anomaly_flag INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(dedupe_key)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_unclassified ON raw_items(classified, ingested_ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_thread_key ON raw_items(thread_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_stable_key ON raw_items(source, stable_key)")

            # --- work_objects (design doc Section 12.1, 2026-08-03): issues and
            # projects are migrated into ONE typed, nestable table further
            # below, AFTER every legacy issues/projects block below has run -
            # deliberately left completely unmodified rather than wrapped in a
            # Python-level guard (confirmed empirically, safe either way:
            # CREATE TABLE IF NOT EXISTS silently no-ops against an existing
            # VIEW of the same name; the CHECK-widening rename/rebuild blocks
            # already gate on `type='table'` in sqlite_master, which a view
            # never matches; every ALTER TABLE ADD COLUMN below is already
            # try/except sqlite3.OperationalError-wrapped, and "cannot add a
            # column to a view" is exactly that error type - all three cases
            # this file's own existing code already handles safely without
            # any change here). Once `issues`/`projects` become views (further
            # below), this entire block keeps running every init_workgraph()
            # call as pure, harmless no-ops - not deleted, since it's still
            # the real, load-bearing setup path for a brand-new install where
            # work_objects doesn't exist yet.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS issues (
                    id               TEXT PRIMARY KEY,
                    title            TEXT NOT NULL,
                    category         TEXT,
                    state            TEXT NOT NULL CHECK (state IN ('active','waiting','blocked','done','noise-archived')),
                    priority         TEXT NOT NULL DEFAULT 'med' CHECK (priority IN ('high','med','low')),
                    priority_score   REAL,
                    nba_action_kind  TEXT CHECK (nba_action_kind IN ('draft','review','approve','chase','wait','read','none')),
                    nba_reason       TEXT,
                    owner            TEXT NOT NULL DEFAULT 'marc',
                    due              TEXT,
                    opened_at        REAL NOT NULL,
                    updated_at       REAL NOT NULL,
                    confidence_tier  TEXT CHECK (confidence_tier IN ('H','M','L'))
                )
            """)
            # Real indices, only ever meaningful while `issues` is still a real
            # table (first run) - "views may not be indexed" is a real SQLite
            # error post-migration (unlike CREATE TABLE IF NOT EXISTS, which
            # silently no-ops against an existing view of the same name; an
            # index name that doesn't exist yet isn't caught by its own IF NOT
            # EXISTS clause). work_objects gets the equivalent indices directly,
            # further down.
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(state)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_priority ON issues(priority_score DESC)")
            except sqlite3.OperationalError:
                pass  # issues is already a view (work_objects has its own indices)

            # Task #44: a real 'dismissed' outcome, distinct from 'done' - using
            # "done" for "this was wrong/not needed" would corrupt whatever
            # learning signal comes from resolution outcomes. Same detect+
            # rebuild-via-sqlite_master migration already used for alerts.kind
            # (task #55) - SQLite has no ALTER TABLE for widening a CHECK
            # constraint, and a table created before this migration still
            # enforces the OLD constraint even though CREATE TABLE IF NOT
            # EXISTS above is a no-op against it.
            existing_issues_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='issues'"
            ).fetchone()
            if existing_issues_sql and "'dismissed'" not in (existing_issues_sql["sql"] or ""):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ALTER TABLE issues RENAME TO issues_pre_task44")
                    conn.execute("""
                        CREATE TABLE issues (
                            id                      TEXT PRIMARY KEY,
                            title                   TEXT NOT NULL,
                            category                TEXT,
                            state                   TEXT NOT NULL CHECK (state IN ('active','waiting','blocked','done','noise-archived','dismissed')),
                            priority                TEXT NOT NULL DEFAULT 'med' CHECK (priority IN ('high','med','low')),
                            priority_score          REAL,
                            nba_action_kind         TEXT CHECK (nba_action_kind IN ('draft','review','approve','chase','wait','read','none')),
                            nba_reason              TEXT,
                            owner                   TEXT NOT NULL DEFAULT 'marc',
                            due                     TEXT,
                            opened_at               REAL NOT NULL,
                            updated_at              REAL NOT NULL,
                            confidence_tier         TEXT CHECK (confidence_tier IN ('H','M','L')),
                            project_id              TEXT REFERENCES projects(id),
                            lesson_id_cited         INTEGER REFERENCES lessons(id),
                            has_unmet_prerequisite  INTEGER NOT NULL DEFAULT 0
                        )
                    """)
                    # Column list driven by whatever issues_pre_task44 ACTUALLY
                    # has (not a hardcoded guess) - three columns
                    # (project_id/lesson_id_cited/has_unmet_prerequisite) were
                    # added to this table by later ALTER TABLEs after the base
                    # CREATE TABLE above was first written, and a real bug here
                    # (missing them from the rebuilt table, caught live against
                    # production before this migration ever committed) would
                    # have made every INSERT below fail with "no such column",
                    # caught by the except clause, silently rolling back and
                    # leaving the OLD constraint in place forever - never
                    # corrupting data, but never actually migrating either.
                    cols = [r["name"] for r in conn.execute("PRAGMA table_info(issues_pre_task44)").fetchall()]
                    col_list = ", ".join(cols)
                    conn.execute(f"INSERT INTO issues ({col_list}) SELECT {col_list} FROM issues_pre_task44")
                    conn.execute("DROP TABLE issues_pre_task44")
                    conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass  # no transaction was actually open (e.g. BEGIN itself failed) - nothing to roll back

            conn.execute("""
                CREATE TABLE IF NOT EXISTS issue_state_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id    TEXT NOT NULL REFERENCES issues(id),
                    from_state  TEXT,
                    to_state    TEXT NOT NULL,
                    changed_ts  REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_state_history_issue ON issue_state_history(issue_id, changed_ts)")

            # Backfill: issues created before this table existed have no history
            # row yet. Seed one synthetic "opened" entry per issue so the
            # timeline UI never starts on a blank slate. Idempotent (only
            # touches issues with zero existing history rows).
            conn.execute("""
                INSERT INTO issue_state_history (issue_id, from_state, to_state, changed_ts)
                SELECT id, NULL, state, opened_at FROM issues
                WHERE id NOT IN (SELECT DISTINCT issue_id FROM issue_state_history)
            """)

            # Task #44, checklist-item-level half: individual ask/decision/
            # commitment/repeat-signal rows have no stable id of their own
            # (they're strings inside a JSON array column, keyed only by the
            # parent extraction's raw_item_id - many-to-one). item_key is a
            # derived, deterministic fingerprint (kind + raw_item_id + a hash
            # of the item's own text) - "stable enough" rather than a true
            # id. Disclosed limitation: if curator re-extracts and reorders or
            # rewords the same underlying ask, the hash changes and an old
            # dismissal silently stops matching (the item reappears) - there
            # is no way to do better without minting a real id at extraction
            # time, which is a bigger change than this task scoped.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checklist_dismissals (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id      TEXT NOT NULL REFERENCES issues(id),
                    item_key      TEXT NOT NULL,
                    kind          TEXT NOT NULL,
                    text_snippet  TEXT NOT NULL,
                    dismissed_ts  REAL NOT NULL,
                    actor         TEXT,
                    UNIQUE(issue_id, item_key)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_checklist_dismissals_issue ON checklist_dismissals(issue_id)")
            # Task #59: this table shipped dismissal-only (task #44); the
            # checklist row's "Mark done" icon needs the same real persistence
            # dismiss got. Plain ALTER TABLE ADD COLUMN (no CHECK constraint -
            # validated in Python instead, deliberately, after the CHECK-
            # widening migration bug caught live in task #44) rather than
            # renaming the table; it shipped with ~zero real rows so a rename
            # isn't worth the extra migration-crash-safety surface for a name
            # that was never load-bearing outside this file.
            try:
                conn.execute("ALTER TABLE checklist_dismissals ADD COLUMN status TEXT NOT NULL DEFAULT 'dismissed'")
            except sqlite3.OperationalError:
                pass  # column already exists

            conn.execute("""
                CREATE TABLE IF NOT EXISTS work_tasks (
                    id          TEXT PRIMARY KEY,
                    issue_id    TEXT NOT NULL REFERENCES issues(id),
                    label       TEXT NOT NULL,
                    state       TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','waiting','blocked','done')),
                    depends_on  TEXT NOT NULL DEFAULT '[]',
                    due         TEXT,
                    action      TEXT CHECK (action IN ('approve','draft','chase','review','upload','sign','none')),
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_issue ON work_tasks(issue_id)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS evidence (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id    TEXT NOT NULL REFERENCES issues(id),
                    raw_item_id INTEGER,
                    type        TEXT NOT NULL CHECK (type IN ('email','teams','calendar','sharepoint','worker_action')),
                    summary     TEXT NOT NULL,
                    ts          REAL NOT NULL
                )
            """)
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_issue ON evidence(issue_id, ts DESC)")
            except sqlite3.OperationalError:
                pass  # evidence is already a view (evidence_units/evidence_unit_links have their own indices)

            # Identity formalization v0 (2026-08-03, docs/design/CONFIDENCE_
            # AND_IDENTITY_REDESIGN.md Section 3.3): additive overlay, empty
            # until backfill_identity_anchors() runs (workgraph_identity.py).
            # Deliberately simpler than the Blueprint's full schema - no
            # work_objects table exists yet, so anchors/containers point at
            # TODAY's real entity (issues.id) directly; upgradeable when/if
            # work_objects lands.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS source_containers (
                    id             TEXT PRIMARY KEY,
                    source         TEXT NOT NULL,
                    container_type TEXT NOT NULL CHECK (container_type IN (
                        'email_conversation','teams_chat','calendar_series','sharepoint_item')),
                    exact_key      TEXT NOT NULL,
                    key_quality    TEXT NOT NULL CHECK (key_quality IN ('exact','heuristic','fallback')),
                    issue_id       TEXT REFERENCES issues(id),
                    created_ts     REAL NOT NULL,
                    UNIQUE(source, container_type, exact_key)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source_containers_issue ON source_containers(issue_id)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS identity_anchors (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    anchor_type      TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    anchor_strength  TEXT NOT NULL CHECK (anchor_strength IN ('exact','strong','weak','negative')),
                    exclusive        INTEGER NOT NULL DEFAULT 0 CHECK (exclusive IN (0,1)),
                    issue_id         TEXT NOT NULL REFERENCES issues(id),
                    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','rejected')),
                    first_seen_ts    REAL NOT NULL,
                    last_seen_ts     REAL NOT NULL,
                    created_by       TEXT NOT NULL DEFAULT 'backfill',
                    reason_json      TEXT NOT NULL DEFAULT '{}'
                )
            """)
            # Exclusive anchors (reference numbers, Jasper Ref: tags) can
            # only ever belong to one ACTIVE issue at a time - the whole
            # point of I2's disjoint-reference veto. Non-exclusive anchors
            # (party, company) are deliberately NOT covered by this index -
            # a person/company legitimately touches many issues.
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_anchor_exclusive
                ON identity_anchors(anchor_type, normalized_value) WHERE exclusive = 1 AND status = 'active'
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_identity_anchors_issue ON identity_anchors(issue_id, status)")

            # Teams sub-session boundaries (2026-08-03, workgraph_sessionize.py):
            # a container can hold more than one real session over time (the
            # real marc-362 shape - one Teams chat mixing several unrelated
            # PR approvals plus ordinary conversation). Additive, observe-only
            # for now - populated by the identity backfill, not yet consulted
            # by the live classify/grouping path (see backfill_identity_
            # anchors' own docstring for why that stays a deliberate, reviewed
            # step, not an automatic wire-in).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS source_sessions (
                    id                   TEXT PRIMARY KEY,
                    source_container_id  TEXT NOT NULL REFERENCES source_containers(id),
                    session_sequence     INTEGER NOT NULL,
                    started_ts           REAL NOT NULL,
                    ended_ts             REAL,
                    boundary_reason      TEXT NOT NULL,
                    UNIQUE(source_container_id, session_sequence)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source_sessions_container ON source_sessions(source_container_id)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS ingest_cursors (
                    source     TEXT NOT NULL,
                    cursor_key TEXT NOT NULL,
                    value      TEXT,
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY (source, cursor_key)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS worker_status (
                    worker       TEXT PRIMARY KEY,
                    state        TEXT NOT NULL,      -- working | idle | blocked
                    current_task TEXT,
                    detail       TEXT,
                    updated_ts   REAL NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_actions (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id       TEXT NOT NULL,
                    action_kind    TEXT NOT NULL,
                    worker         TEXT NOT NULL,
                    instructions   TEXT,
                    status         TEXT NOT NULL DEFAULT 'requested',  -- requested | in_progress | done | failed
                    message_id     TEXT,
                    requested_ts   REAL NOT NULL,
                    updated_ts     REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_issue ON pending_actions(issue_id, requested_ts DESC)")

            # Part E2 of the grouping/NBA redesign (2026-07-30): the first
            # real "what did we offer vs. what did Marc actually do" audit
            # trail - feeds a later learning step once enough real data
            # accumulates (explicitly out of scope for this build). Follows
            # the lessons table's own real-repeated-outcome convention, not
            # a write-once-and-forget log.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nba_choice_log (
                    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id                    TEXT NOT NULL REFERENCES issues(id),
                    offered_ts                  REAL NOT NULL,
                    offered_json                TEXT NOT NULL,   -- ranked [{kind,label,rationale,score,source_surface}]
                    scoring_inputs_json         TEXT NOT NULL,   -- state/days_stale/due/value/category/lesson snapshot at offer time
                    status                      TEXT NOT NULL DEFAULT 'offered' CHECK (status IN ('offered','chosen','ignored','expired')),
                    chosen_action_kind          TEXT,
                    chosen_ts                   REAL,
                    resulting_pending_action_id INTEGER REFERENCES pending_actions(id),
                    chosen_note                 TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nba_choice_log_issue_status ON nba_choice_log(issue_id, status, offered_ts DESC)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id     TEXT,
                    kind         TEXT NOT NULL CHECK (kind IN ('stale','high_priority_ask','anomaly','stuck_action','unmet_prerequisite','reference_id_collision','conflicting_value_figures')),
                    severity     TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
                    summary      TEXT NOT NULL,
                    source_ref   TEXT,
                    created_ts   REAL NOT NULL,
                    dismissed    INTEGER NOT NULL DEFAULT 0,
                    dismissed_ts REAL
                )
            """)
            # SQLite has no ALTER TABLE for widening a CHECK constraint - an
            # alerts table created before task #55 added 'unmet_prerequisite'
            # still enforces the OLD constraint even though the CREATE TABLE
            # above is a no-op against it (IF NOT EXISTS). Detect that exact
            # case and rebuild, migrating every existing row.
            #
            # Fixed (adversarial review, task #61): the rename+rebuild+copy+
            # drop used to run as 4 independent autocommit statements on this
            # isolation_level=None connection - a crash between RENAME and
            # DROP left the real data permanently orphaned under
            # alerts_pre_task55 with a fresh, EMPTY alerts table silently
            # taking its place (and the empty table's schema already
            # satisfies the "already migrated" check above, so nothing would
            # ever retry it). Wrapped in an explicit transaction: SQLite's DDL
            # is fully transactional, so this is now all-or-nothing - a crash
            # mid-migration rolls back to the untouched original table, not a
            # half-renamed mess. Also fixes the concurrent-process case (this
            # init_workgraph() runs from many independent processes against
            # the same WAL file): BEGIN IMMEDIATE serializes against a
            # concurrent migration instead of both racing the same renames,
            # and the broad except below is the same "another process beat us
            # to it, and that's fine" pattern already used for every other
            # migration in this file.
            existing_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='alerts'"
            ).fetchone()
            if existing_sql and "unmet_prerequisite" not in (existing_sql["sql"] or ""):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ALTER TABLE alerts RENAME TO alerts_pre_task55")
                    conn.execute("""
                        CREATE TABLE alerts (
                            id           INTEGER PRIMARY KEY AUTOINCREMENT,
                            issue_id     TEXT,
                            kind         TEXT NOT NULL CHECK (kind IN ('stale','high_priority_ask','anomaly','stuck_action','unmet_prerequisite','reference_id_collision','conflicting_value_figures')),
                            severity     TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
                            summary      TEXT NOT NULL,
                            source_ref   TEXT,
                            created_ts   REAL NOT NULL,
                            dismissed    INTEGER NOT NULL DEFAULT 0,
                            dismissed_ts REAL
                        )
                    """)
                    conn.execute("""
                        INSERT INTO alerts (id, issue_id, kind, severity, summary, source_ref,
                                             created_ts, dismissed, dismissed_ts)
                        SELECT id, issue_id, kind, severity, summary, source_ref,
                               created_ts, dismissed, dismissed_ts FROM alerts_pre_task55
                    """)
                    conn.execute("DROP TABLE alerts_pre_task55")
                    conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass  # no transaction was actually open (e.g. BEGIN itself failed) - nothing to roll back
            # Same rebuild-and-copy pattern as the task #55 migration just
            # above, for enhancement idea panel #14's new 'reference_id_
            # collision' kind - a table created before today still enforces
            # the OLD (pre-#14) constraint even though CREATE TABLE IF NOT
            # EXISTS above is a no-op against it.
            existing_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='alerts'"
            ).fetchone()
            if existing_sql and "reference_id_collision" not in (existing_sql["sql"] or ""):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ALTER TABLE alerts RENAME TO alerts_pre_e14")
                    conn.execute("""
                        CREATE TABLE alerts (
                            id           INTEGER PRIMARY KEY AUTOINCREMENT,
                            issue_id     TEXT,
                            kind         TEXT NOT NULL CHECK (kind IN ('stale','high_priority_ask','anomaly','stuck_action','unmet_prerequisite','reference_id_collision','conflicting_value_figures')),
                            severity     TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
                            summary      TEXT NOT NULL,
                            source_ref   TEXT,
                            created_ts   REAL NOT NULL,
                            dismissed    INTEGER NOT NULL DEFAULT 0,
                            dismissed_ts REAL
                        )
                    """)
                    conn.execute("""
                        INSERT INTO alerts (id, issue_id, kind, severity, summary, source_ref,
                                             created_ts, dismissed, dismissed_ts)
                        SELECT id, issue_id, kind, severity, summary, source_ref,
                               created_ts, dismissed, dismissed_ts FROM alerts_pre_e14
                    """)
                    conn.execute("DROP TABLE alerts_pre_e14")
                    conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass  # no transaction was actually open (e.g. BEGIN itself failed) - nothing to roll back
            # Same rebuild-and-copy pattern again, for enhancement idea panel
            # #16's new 'conflicting_value_figures' kind.
            existing_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='alerts'"
            ).fetchone()
            if existing_sql and "conflicting_value_figures" not in (existing_sql["sql"] or ""):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ALTER TABLE alerts RENAME TO alerts_pre_e16")
                    conn.execute("""
                        CREATE TABLE alerts (
                            id           INTEGER PRIMARY KEY AUTOINCREMENT,
                            issue_id     TEXT,
                            kind         TEXT NOT NULL CHECK (kind IN ('stale','high_priority_ask','anomaly','stuck_action','unmet_prerequisite','reference_id_collision','conflicting_value_figures')),
                            severity     TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
                            summary      TEXT NOT NULL,
                            source_ref   TEXT,
                            created_ts   REAL NOT NULL,
                            dismissed    INTEGER NOT NULL DEFAULT 0,
                            dismissed_ts REAL
                        )
                    """)
                    conn.execute("""
                        INSERT INTO alerts (id, issue_id, kind, severity, summary, source_ref,
                                             created_ts, dismissed, dismissed_ts)
                        SELECT id, issue_id, kind, severity, summary, source_ref,
                               created_ts, dismissed, dismissed_ts FROM alerts_pre_e16
                    """)
                    conn.execute("DROP TABLE alerts_pre_e16")
                    conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass  # no transaction was actually open (e.g. BEGIN itself failed) - nothing to roll back
            # Same rebuild-and-copy pattern again, for enhancement idea panel
            # #17's new 'duplicate_ask_across_project' kind - the same
            # canonical_key open on 2+ DIFFERENT issues that are members of
            # the SAME project (canonical-key dedup at materialize time is
            # deliberately issue-scoped, so this is the only place that
            # catches the same ask/commitment/decision tracked twice, or
            # with conflicting details, once two issues that both carry it
            # have been grouped into one project).
            existing_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='alerts'"
            ).fetchone()
            if existing_sql and "duplicate_ask_across_project" not in (existing_sql["sql"] or ""):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ALTER TABLE alerts RENAME TO alerts_pre_e17")
                    conn.execute("""
                        CREATE TABLE alerts (
                            id           INTEGER PRIMARY KEY AUTOINCREMENT,
                            issue_id     TEXT,
                            kind         TEXT NOT NULL CHECK (kind IN ('stale','high_priority_ask','anomaly','stuck_action','unmet_prerequisite','reference_id_collision','conflicting_value_figures','duplicate_ask_across_project')),
                            severity     TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
                            summary      TEXT NOT NULL,
                            source_ref   TEXT,
                            created_ts   REAL NOT NULL,
                            dismissed    INTEGER NOT NULL DEFAULT 0,
                            dismissed_ts REAL
                        )
                    """)
                    conn.execute("""
                        INSERT INTO alerts (id, issue_id, kind, severity, summary, source_ref,
                                             created_ts, dismissed, dismissed_ts)
                        SELECT id, issue_id, kind, severity, summary, source_ref,
                               created_ts, dismissed, dismissed_ts FROM alerts_pre_e17
                    """)
                    conn.execute("DROP TABLE alerts_pre_e17")
                    conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass  # no transaction was actually open (e.g. BEGIN itself failed) - nothing to roll back
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_dismissed ON alerts(dismissed, created_ts DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_issue_kind ON alerts(issue_id, kind)")

            # --- Parties (people seen across all communications) -------------
            # Affiliation is a best-effort heuristic (domain-based), NOT an
            # authoritative directory lookup - no Graph "look up any user"
            # tool exists in this toolset, confirmed by direct search. A
            # lilly.com address does NOT reliably mean "Lilly employee" -
            # some suppliers are provisioned on Lilly's network with
            # lilly.com-style addresses (guest/vendor accounts). Once Marc
            # corrects one, that correction is permanent (see
            # correct_party_affiliation) - it never re-guesses that party.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS parties (
                    id                     TEXT PRIMARY KEY,
                    primary_email          TEXT NOT NULL UNIQUE,
                    display_name           TEXT,
                    affiliation            TEXT NOT NULL DEFAULT 'unknown' CHECK (affiliation IN ('internal','external','unknown')),
                    affiliation_confidence TEXT NOT NULL DEFAULT 'M' CHECK (affiliation_confidence IN ('H','M','L')),
                    affiliation_source     TEXT NOT NULL DEFAULT 'domain_heuristic',
                    company                TEXT,
                    first_seen_ts          REAL NOT NULL,
                    last_seen_ts           REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_parties_affiliation ON parties(affiliation)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS issue_parties (
                    issue_id TEXT NOT NULL REFERENCES issues(id),
                    party_id TEXT NOT NULL REFERENCES parties(id),
                    role     TEXT,
                    PRIMARY KEY (issue_id, party_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_issue_parties_party ON issue_parties(party_id)")

            # --- Projects (groups related Issues/threads) ---------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id         TEXT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    category   TEXT,
                    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','waiting','done','archived','dismissed')),
                    opened_at  REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            # Task #62: real project-level "dismissed" state (same distinct-
            # from-done reasoning as issues.state's task #44) - there was
            # previously no way to dismiss a whole project at all, backend or
            # UI. Same detect+rebuild migration pattern as #44's issues.state
            # widening - schema confirmed against the LIVE database first
            # (exactly 6 columns, no later ALTER TABLE ADD COLUMNs unlike
            # issues) specifically to avoid repeating that bug.
            existing_projects_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
            ).fetchone()
            if existing_projects_sql and "'dismissed'" not in (existing_projects_sql["sql"] or ""):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ALTER TABLE projects RENAME TO projects_pre_task62")
                    conn.execute("""
                        CREATE TABLE projects (
                            id         TEXT PRIMARY KEY,
                            name       TEXT NOT NULL,
                            category   TEXT,
                            status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','waiting','done','archived','dismissed')),
                            opened_at  REAL NOT NULL,
                            updated_at REAL NOT NULL
                        )
                    """)
                    cols = [r["name"] for r in conn.execute("PRAGMA table_info(projects_pre_task62)").fetchall()]
                    col_list = ", ".join(cols)
                    conn.execute(f"INSERT INTO projects ({col_list}) SELECT {col_list} FROM projects_pre_task62")
                    conn.execute("DROP TABLE projects_pre_task62")
                    conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass  # no transaction was actually open (e.g. BEGIN itself failed) - nothing to roll back

            try:
                conn.execute("ALTER TABLE issues ADD COLUMN project_id TEXT REFERENCES projects(id)")
            except sqlite3.OperationalError:
                pass  # already added by a prior init_workgraph() call
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project_id)")
            except sqlite3.OperationalError:
                pass  # issues is already a view (work_objects has its own indices)

            try:
                # Project Deep-Dive (design doc Section 10.4): set ONLY by
                # the deterministic mark_project_deep_dived() call the
                # routine makes when it finishes a wake - never inferred
                # from the model's own prose, same discipline as
                # synthesized_from_marker/claims_revision.
                conn.execute("ALTER TABLE projects ADD COLUMN last_deep_dive_ts REAL")
            except sqlite3.OperationalError:
                pass
            try:
                # Short, honest, freeform account of what was actually
                # searched and found (or why the run stopped early) -
                # Section 10.4's inspectable trail, given live M365 search
                # has no code-verifiable success proxy the way relay's
                # calendar-cursor check does.
                conn.execute("ALTER TABLE projects ADD COLUMN last_deep_dive_note TEXT")
            except sqlite3.OperationalError:
                pass

            # 2026-07-31: durable relationships between two DIFFERENT real
            # projects that should NOT become one project - e.g. "same
            # vendor team, adjacent topics" (two distinct recurring PwC
            # meeting series) or "one enables the other" (H1 helping exit an
            # old contract so a new H1 deal can proceed). Project-level, not
            # issue-level: these are relationships between established
            # bodies of work, matching how the cockpit already renders
            # projects - an issue-level link would go stale the moment
            # issue-to-project membership shifts.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS project_links (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_project_id TEXT NOT NULL REFERENCES projects(id),
                    to_project_id   TEXT NOT NULL REFERENCES projects(id),
                    link_type       TEXT NOT NULL CHECK (link_type IN
                                        ('related','enables','blocks','depends_on','same_supplier','follow_on')),
                    reason          TEXT NOT NULL,
                    created_ts      REAL NOT NULL,
                    created_by      TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_project_links_from ON project_links(from_project_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_project_links_to ON project_links(to_project_id)")

            # capability_suggestions - a NOTE any worker can log when it
            # notices a real gap during normal work ("I keep seeing X pattern
            # with no good handling"), never a code change on its own.
            # Same pending/confirmed/rejected shape as pending_project_
            # suggestions on purpose - Marc reviews and greenlights (or
            # rejects) each one; nothing gets built until he does. Confirming
            # one here is NOT an action, unlike confirm_suggestion for
            # projects - it just means "worth building," the build itself is
            # still a separate, explicit step (a conversation, same as every
            # enhancement made this way so far).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS capability_suggestions (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin           TEXT NOT NULL,
                    observation      TEXT NOT NULL,
                    suggestion       TEXT NOT NULL,
                    rationale        TEXT,
                    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','rejected')),
                    created_ts       REAL NOT NULL,
                    resolved_ts      REAL,
                    resolution_note  TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_capability_suggestions_status ON capability_suggestions(status)")

            # --- Task ownership learning ---------------------------------------
            try:
                conn.execute("ALTER TABLE work_tasks ADD COLUMN owner TEXT")
            except sqlite3.OperationalError:
                pass  # already added by a prior init_workgraph() call

            conn.execute("""
                CREATE TABLE IF NOT EXISTS ownership_rules (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_field    TEXT NOT NULL,      -- category | party_company | sender_domain | topic
                    match_value    TEXT NOT NULL,
                    default_owner  TEXT NOT NULL,       -- 'marc' or a parties.id
                    created_ts     REAL NOT NULL,
                    created_reason TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ownership_rules_match ON ownership_rules(match_field, match_value)")

            # --- Generalized audit log (new entities only - issues keep their
            # existing issue_state_history table as-is, not migrated here to
            # avoid touching an already-tested path mid-build) -----------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,   -- project | party | task_owner
                    entity_id   TEXT NOT NULL,
                    field       TEXT NOT NULL,
                    old_value   TEXT,
                    new_value   TEXT,
                    changed_ts  REAL NOT NULL,
                    reason      TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id, changed_ts)")

            # --- Per-communication extraction (real LLM judgment, computed ONCE
            # per raw_item and never recomputed - see workgraph_synthesis.py /
            # SYNTHESIS_ROUTINE.md). Additional to the deterministic
            # item_class/topic/sentiment classification above, not a
            # replacement for it. -------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS raw_item_extractions (
                    raw_item_id     INTEGER PRIMARY KEY REFERENCES raw_items(id),
                    extracted_json  TEXT NOT NULL,   -- {"asks": [...], "decisions": [...], "dates_mentioned": [...], "commitments": [...], "key_facts": [...]}
                    extracted_ts    REAL NOT NULL
                )
            """)

            try:
                # Corrected-extraction reconciliation (2026-08-04,
                # architecture-review follow-up P1): create_extraction is an
                # UPSERT (a re-extraction just overwrites extracted_json in
                # place), but materialize_claims_for_raw_item's OLD guard
                # (has_claims_for_raw_item) only ever asked "does this
                # raw_item have ANY claims at all" - a corrected extraction
                # silently never re-materialized, so the claims ledger and
                # the extraction blob could diverge forever with no way to
                # tell. content_hash is a canonical-JSON hash of the CURRENT
                # extracted_json (set automatically by create_extraction on
                # every write); materialized_hash records which hash the
                # claims table was last reconciled against - the two
                # differing is exactly "a correction landed that claims
                # hasn't caught up to yet" (see workgraph_claims.
                # materialize_claims_for_raw_item's own docstring).
                conn.execute("ALTER TABLE raw_item_extractions ADD COLUMN content_hash TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE raw_item_extractions ADD COLUMN materialized_hash TEXT")
            except sqlite3.OperationalError:
                pass

            # --- Per-entity synthesis (Project, or a standalone Issue not yet
            # grouped into one) - ONE synthesis reflecting the whole underlying
            # negotiation, not one per thread. synthesized_from_marker is the
            # deterministic staleness fingerprint from
            # workgraph_synthesis.compute_evidence_marker, computed and stored
            # server-side at write time, never trusted from the caller. -----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS synthesis (
                    entity_type             TEXT NOT NULL CHECK (entity_type IN ('issue','project')),
                    entity_id               TEXT NOT NULL,
                    summary                 TEXT,
                    next_steps              TEXT,      -- JSON list: [{"step": "...", "current": true/false,
                                                        --   "estimate_days_low": N, "estimate_days_high": N,
                                                        --   "estimate_confidence": "documented"|"model"|"unknown",
                                                        --   "estimate_note": "..."}, ...] - the estimate_* fields
                                                        -- are optional per step, grounded in the sourcing-process
                                                        -- knowledge base's own [DOCUMENTED]/[MARC'S MODEL]/
                                                        -- [UNKNOWN] honesty labels (never invented).
                    suggested_actions       TEXT,       -- JSON list: [{"task_id": "...", "label": "...", "rationale": "..."}, ...]
                    synthesized_at          REAL,
                    synthesized_from_marker TEXT,       -- e.g. "count:14|max_ts:1785200000.0"
                    PRIMARY KEY (entity_type, entity_id)
                )
            """)
            try:
                # Derived title (e.g. "UneeQ pricing negotiation") - a real synthesized
                # label, not the raw email subject line issues.title falls back to.
                conn.execute("ALTER TABLE synthesis ADD COLUMN derived_title TEXT")
            except sqlite3.OperationalError:
                pass  # already added by a prior init_workgraph() call
            try:
                # Roof-level completion estimate - JSON {"note": "...", "confidence":
                # "documented"|"model"|"unknown"}. A real "not enough documented timing
                # to estimate" is a valid, expected value here, not a failure to fill in.
                conn.execute("ALTER TABLE synthesis ADD COLUMN estimated_completion TEXT")
            except sqlite3.OperationalError:
                pass  # already added by a prior init_workgraph() call

            # --- Phase 3 (design doc Section 9): claims materialize the ask/
            # decision/commitment/date fields already sitting in
            # raw_item_extractions.extracted_json into real, typed, deduped,
            # actor-attributed rows - not a new extraction pass, see
            # workgraph_claims.py. author is deterministic (raw_items.direction),
            # never a keyword guess (Section 9.4). owner is derived from
            # author+claim_type for ask/commitment, and from the extraction's
            # own 'whose' judgment for date claims (Section 9.7, task #57) -
            # NULL for decisions, which are joint facts, not obligations.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS claims (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id        TEXT NOT NULL REFERENCES issues(id),
                    raw_item_id     INTEGER NOT NULL REFERENCES raw_items(id),
                    claim_type      TEXT NOT NULL CHECK (claim_type IN ('ask','decision','commitment','date')),
                    text            TEXT NOT NULL,
                    author          TEXT NOT NULL CHECK (author IN ('marc','counterparty','unknown')),
                    author_basis    TEXT NOT NULL CHECK (author_basis IN ('direction','unresolved')),
                    owner           TEXT CHECK (owner IN ('marc','counterparty','unknown')),
                    date_kind       TEXT CHECK (date_kind IN ('hard','soft')),
                    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','superseded','dismissed')),
                    superseded_by   INTEGER REFERENCES claims(id),
                    escalated       INTEGER NOT NULL DEFAULT 0,
                    escalation_note TEXT,
                    first_seen_ts   REAL NOT NULL,
                    last_seen_ts    REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_issue ON claims(issue_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_raw_item ON claims(raw_item_id)")

            try:
                # Right-sized completion-contract support (design doc Section
                # 12.3): a predicate JSON blob a caller can set at claim-
                # creation time to check before accepting a completion, e.g.
                # {"requires": ["outbound_message_after_request",
                # "artifact_attached"]}. Deliberately NOT auto-populated from
                # extraction yet - that needs its own SYNTHESIS_ROUTINE.md
                # contract change (same shape as Section 9.7's `whose` field),
                # not built in this pass since no real extraction support
                # exists to state one. Column is schema-ready; NULL means "no
                # stated contract," never silently inferred.
                conn.execute("ALTER TABLE claims ADD COLUMN completion_contract TEXT")
            except sqlite3.OperationalError:
                pass

            try:
                # Canonical claim deduplication (2026-08-04, architecture-
                # review follow-up P1): repeat_signals-driven dedup (Section
                # 9.3, find_open_claim_by_text) requires a byte-EXACT text
                # match, which real production data confirmed misses real
                # duplicates - the same Ariba PR reminder re-sent with
                # slightly different wording around an identical reference
                # ID (PR1161567/PR1170816/PR1169904/PR854779-V4 all real live
                # cases). canonical_key is a SEPARATE, additive fallback
                # dedup key (see workgraph_claims.canonical_key_for_claim) -
                # deliberately NOT a UNIQUE constraint: the live backfill
                # (workgraph_claims.backfill_canonical_keys_and_merge_
                # duplicates) must run and merge existing duplicate groups
                # BEFORE any uniqueness could be enforced without breaking
                # on pre-existing data, and application-level check-then-
                # insert-or-touch (same pattern find_open_claim_by_text
                # already uses) is sufficient - no DB-level constraint
                # needed for a single-writer-at-a-time store. NULL is a
                # legitimate value (no definitive reference AND normalized
                # text below the trust bar), never backfilled to a guess.
                conn.execute("ALTER TABLE claims ADD COLUMN canonical_key TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_canonical ON claims(issue_id, claim_type, canonical_key)"
            )

            # claim_edges (design doc Section 12.3 / 8.2): the edge types
            # Section 8.2 named back when Phase 3 was built. Corrected
            # 2026-08-07 (DB audit): NONE of the four edge types has a real
            # production writer yet, including `supersedes` - this comment
            # used to claim touch_claim wrote supersedes edges through here,
            # but touch_claim only ever bumps last_seen_ts/escalation, and
            # the real supersede mechanism (update_claim_status's own
            # superseded_by column write) never calls create_claim_edge
            # either. create_claim_edge/list_claim_edges_for_claim exist and
            # are tested, just not called from any live pipeline path -
            # schema-ready, genuinely empty, same "don't build a producer
            # nothing calls yet" discipline as everything else in this doc.
            # Evidence Assembly's conflict detection (Section 8.1) remains
            # the real intended future producer for contradicts/supports.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS claim_edges (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_claim_id INTEGER NOT NULL REFERENCES claims(id),
                    to_claim_id   INTEGER NOT NULL REFERENCES claims(id),
                    edge_type     TEXT NOT NULL CHECK (edge_type IN
                                     ('contradicts','supports','derived_from','supersedes')),
                    created_ts    REAL NOT NULL,
                    created_by    TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_edges_from ON claim_edges(from_claim_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_edges_to ON claim_edges(to_claim_id)")

            # claim_events (design doc Section 12.3): right-sized against the
            # Blueprint's full 14-event work-state taxonomy (REQUEST_WORK/
            # COMMIT_WORK/.../REOPEN_WORK) - that full taxonomy needs curator
            # to classify EVERY restatement into one of 14 buckets, which is a
            # much bigger extraction-contract change than anything built so
            # far this session, with no current producer for most of them.
            # Built instead: the 5 event types that already have a real,
            # deterministic signal today - CREATE (materialize_claims_for_
            # raw_item's own insert), ESCALATE/ACKNOWLEDGE (touch_claim's
            # existing escalated flag, Section 9.3), COMPLETE/DISMISS (the
            # checklist done/dismiss actions, now synced to claim status for
            # the first time - see workgraph_claims.py). The fuller taxonomy
            # stays a real, named gap, not silently dropped - revisit if a
            # real case shows these 5 aren't enough (the same demand-driven
            # bar every other genuinely-deferred piece in this doc uses).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS claim_events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id   INTEGER NOT NULL REFERENCES claims(id),
                    event_type TEXT NOT NULL CHECK (event_type IN
                                  ('create','escalate','acknowledge','complete','dismiss')),
                    ts         REAL NOT NULL,
                    actor      TEXT NOT NULL,
                    note       TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_events_claim ON claim_events(claim_id, ts)")

            try:
                # Provenance fix (2026-08-04, architecture-review follow-up
                # P1): claims.raw_item_id is fixed at creation time and
                # never updated - a claim touched 5 times by 5 different
                # repeat messages had no record of which raw_item caused
                # each individual touch, only the FIRST one. A nullable
                # raw_item_id on claim_events closes this without a new
                # table - every log_claim_event call site (create AND the
                # repeat/dedup touch paths) now passes the raw_item that
                # triggered it.
                conn.execute("ALTER TABLE claim_events ADD COLUMN raw_item_id INTEGER REFERENCES raw_items(id)")
            except sqlite3.OperationalError:
                pass

            # pending_claim_suggestions (2026-08-04, architecture-review
            # follow-up P1, task #155): same review-then-confirm shape as
            # pending_project_suggestions - claims materialize and dedupe
            # correctly now, but nothing ever suggests a claim IS resolved.
            # suggestion_kind='resolve' (a specific raw_item's content
            # directly says an open claim was fulfilled) is a proposal to
            # mark the claim done; 'contradiction' (an issue closed while a
            # claim under it is still open) has no single obvious
            # resolution and is suggest-only in a different sense - it
            # surfaces a mismatch, it never implies the claim should
            # auto-close. evidence_type is a small, closed enum - no
            # fuzzy/heuristic "probably done" scoring, see
            # workgraph_reconcile.py's own module docstring for why.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_claim_suggestions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id        INTEGER NOT NULL REFERENCES claims(id),
                    suggestion_kind TEXT NOT NULL CHECK (suggestion_kind IN ('resolve','contradiction')),
                    evidence_type   TEXT NOT NULL CHECK (evidence_type IN
                                       ('explicit_resolution_signal','issue_closed_with_open_claims')),
                    evidence_note   TEXT,
                    raw_item_id     INTEGER REFERENCES raw_items(id),
                    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','rejected','expired')),
                    created_ts      REAL NOT NULL,
                    resolved_ts     REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_suggestions_status ON pending_claim_suggestions(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_claim_suggestions_claim ON pending_claim_suggestions(claim_id, evidence_type)")

            # identity_constraints (design doc Section 12.6): extends
            # pending_project_suggestions' pairwise dedupe - today PENDING-only,
            # forgotten the moment a suggestion is rejected/expires - into a
            # real, durable veto. Schema-ready for all 10 constraint types the
            # design doc names, but only cannot_merge/cannot_link get a real
            # producer (workgraph_projects.reject_suggestion) and consumer
            # (_create_project_suggestion_on, below) in this pass. The other 8
            # (must_link, confirm_anchor, downweight_anchor,
            # mark_artifact_generic, override_container_class,
            # confirm_person_alias, prevent_person_merge,
            # confirm_work_object_parent) have no current caller that would
            # ever write one - stay schema-ready-only, same "build what has a
            # real producer" discipline as claim_edges' contradicts/supports
            # above.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS identity_constraints (
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
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_identity_constraints_a ON identity_constraints(constraint_type, subject_a)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_identity_constraints_b ON identity_constraints(constraint_type, subject_b)")


            # work_object_signatures (design doc Section 12.7): a cached,
            # invalidate-on-write signature per work_object, read by
            # scored_grouping_decision/backtest_scored_model instead of
            # each of those re-querying parties/containers/references from
            # scratch for every OTHER issue on every single call - a real,
            # measured cost today (_issue_signal_snapshot is recomputed per
            # candidate, per call). Invalidated (row deleted, recomputed
            # lazily on next read - see workgraph_projects.
            # get_or_compute_work_object_signature) at every real write site
            # that changes what's IN a signature: link_raw_item_to_issue,
            # link_party_to_issue, add_evidence, reject_suggestion (changes
            # cannot_link_ids), merge_issue_into (the survivor absorbs the
            # loser's parties/raw_items/containers). accepted_lineages stays
            # an honest '[]' - artifact_lineages (12.5/v2.6) doesn't exist
            # yet, nothing to populate it with. positive_vocabulary/negative_
            # vocabulary stay NULL, same "no real producer" reason claim_
            # edges' contradicts/supports and identity_constraints' 8 unused
            # types are named rather than silently dropped - topic matching
            # stays the existing direct SequenceMatcher comparison
            # (workgraph_projects._matched_data_points), not
            # folded into a vocabulary field nothing extracts yet.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS work_object_signatures (
                    work_object_id      TEXT PRIMARY KEY REFERENCES work_objects(id),
                    definitive_ids      TEXT NOT NULL,
                    accepted_lineages   TEXT NOT NULL,
                    containers          TEXT NOT NULL,
                    external_orgs       TEXT NOT NULL,
                    participant_roles   TEXT NOT NULL,
                    active_period_start REAL,
                    active_period_end   REAL,
                    positive_vocabulary TEXT,
                    negative_vocabulary TEXT,
                    cannot_link_ids     TEXT NOT NULL,
                    updated_ts          REAL NOT NULL
                )
            """)

            # Fixed 2026-08-05 (real live bug, found investigating why the
            # Ariba supplier signal never showed up in a retroactive pass):
            # this cache had NO way to know compute_work_object_signature's
            # own OUTPUT SHAPE had changed (a code deploy, not a data write) -
            # 355 of 361 real issues already had a cached row from before
            # today's ariba_supplier field existed, and get_or_compute_work_
            # object_signature trusted every one of them forever, silently
            # never recomputing. schema_version lets a stale row be detected
            # and treated as a cache miss the moment the code's own idea of
            # "what a signature contains" changes, without a bulk one-time
            # clear that would just recur on the next such change. Bump
            # workgraph_projects._SIGNATURE_SCHEMA_VERSION, not this column
            # def, whenever compute_work_object_signature's real output
            # shape changes again.
            try:
                conn.execute("ALTER TABLE work_object_signatures ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # already added by a prior init_workgraph() call

            # docs/design/PERSONALIZED_DATA_POINT_DISCOVERY.md - per-installation
            # discovered data-point vocabulary, replacing the hardcoded procurement-
            # specific fields (ariba_requester/ariba_descriptor/value_amount/
            # system_party in positive_vocabulary above, the fixed point-type list
            # in workgraph_projects._matched_data_points) with real, confirmed,
            # per-person configuration. point_type is deliberately a small
            # STRUCTURAL taxonomy (how a value participates in matching: can it
            # auto-merge, does it count toward the 2+-point gate, etc.) - never a
            # content category. A definition starts 'proposed' (discovery found it,
            # nothing trusts it yet) and only becomes 'confirmed' via explicit human
            # review - same abstain-by-default discipline as Aristotle's
            # detect_candidate_rules() and workgraph_lessons' trust arithmetic,
            # which trust_score here deliberately reuses the same shape of (never a
            # hard cliff, bump on repeat-confirm, penalty on reversal).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS data_point_definitions (
                    id                  TEXT PRIMARY KEY,
                    name                TEXT NOT NULL,
                    description         TEXT,
                    point_type          TEXT NOT NULL CHECK (point_type IN
                                           ('entity','reference','amount','person','date','freetext')),
                    deterministic_rule  TEXT,
                    status              TEXT NOT NULL DEFAULT 'proposed'
                                           CHECK (status IN ('proposed','confirmed','rejected')),
                    trust_score         REAL NOT NULL DEFAULT 0.6,
                    discovered_from     TEXT,
                    last_matched_ts     REAL,
                    created_ts          REAL NOT NULL,
                    confirmed_ts        REAL,
                    confirmed_by        TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_data_point_definitions_status ON data_point_definitions(status)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS data_point_values (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    definition_id    TEXT NOT NULL REFERENCES data_point_definitions(id),
                    work_object_id   TEXT NOT NULL REFERENCES work_objects(id),
                    value            TEXT NOT NULL,
                    extraction_source TEXT NOT NULL CHECK (extraction_source IN
                                          ('deterministic','llm_backfill','llm_judgment')),
                    extracted_ts     REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_data_point_values_wo ON data_point_values(work_object_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_data_point_values_def ON data_point_values(definition_id)")

            # contractpodai_requests (task #265, 2026-08-07): a real, external
            # SYSTEM's own structured fields, kept in a table scoped to that
            # system rather than folded into generic personal vocabulary
            # (data_point_definitions above) - Marc's own direct correction,
            # since a field like "Request ID" means something completely
            # different depending on which system produced it (this table's
            # own request_id is a ContractPodAI-internal number, unrelated to
            # an Ariba PR/PO despite the shared English label). One row per
            # real request (request_id is that system's own stable key, with
            # a real cloud22.contractpod.com permalink - not a guess), fields
            # populated incrementally as different real ContractPodAI
            # notification templates about the SAME request arrive (see
            # workgraph_signals.extract_contractpodai_request_fields) - a
            # later template filling in a field an earlier one didn't carry
            # is expected, not an error.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS contractpodai_requests (
                    request_id           TEXT PRIMARY KEY,
                    contractpod_url      TEXT,
                    sourcing_lead        TEXT,
                    functional_area      TEXT,
                    s2p_action           TEXT,
                    supplier_name        TEXT,
                    priority             TEXT,
                    primary_assignee     TEXT,
                    additional_assignees TEXT,
                    reviewer             TEXT,
                    requester            TEXT,
                    agreement_title      TEXT,
                    raw_item_id          INTEGER,
                    issue_id             TEXT,
                    first_seen_ts        REAL NOT NULL,
                    last_seen_ts         REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cpai_requests_issue ON contractpodai_requests(issue_id)")

            # ariba_requisitions (task #267, 2026-08-07): same system-scoped
            # treatment as contractpodai_requests above, for the requester/
            # descriptor/amount fields workgraph_signals.extract_ariba_
            # requisition_fields already pulls off an Ariba requisition-
            # approval subject line. Reference-ID matching itself needs no new
            # table - REFERENCE_ID_RE's generic full-text scan already catches
            # "PR1193376" the same as any other PR/PO number - this table's
            # only job is to persist the requester/descriptor/amount that were
            # previously read-and-discarded every time (used only as a one-
            # shot "is this significant" boolean in workgraph_classify.
            # _has_matchable_signal, never stored anywhere queryable).
            # pr_number is the primary key (not a separate id) because
            # extract_ariba_requisition_fields is only ever called on an
            # Ariba-shaped subject that already contains the real PR number -
            # there is no case where this table has a row without one.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ariba_requisitions (
                    pr_number     TEXT PRIMARY KEY,
                    requester     TEXT,
                    descriptor    TEXT,
                    amount        REAL,
                    raw_item_id   INTEGER,
                    issue_id      TEXT,
                    first_seen_ts REAL NOT NULL,
                    last_seen_ts  REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ariba_requisitions_issue ON ariba_requisitions(issue_id)")

            # PERSONALIZED_DATA_POINT_DISCOVERY.md section 3's continuous, cheap
            # (no LLM cost) tracker - counts a recurring pattern's real
            # occurrences/distinct-thread spread until it crosses the real
            # significance bar (5 occurrences, 2+ distinct threads, 60-day
            # window - Marc's own numbers), which is what triggers the one real
            # LLM call that drafts an actual proposal. pattern_signature is a
            # normalized key (sender domain / labeled-field name / structural
            # signature) - deliberately NOT a FK to anything, since most
            # observations never graduate into a real definition at all.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candidate_pattern_observations (
                    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_signature         TEXT NOT NULL UNIQUE,
                    occurrence_count          INTEGER NOT NULL DEFAULT 0,
                    distinct_thread_count     INTEGER NOT NULL DEFAULT 0,
                    first_seen_ts             REAL NOT NULL,
                    last_seen_ts              REAL NOT NULL,
                    promoted_to_definition_id TEXT REFERENCES data_point_definitions(id)
                )
            """)

            # Implementation-level addition to the design doc's §4 sketch
            # (2026-08-06, task #213): distinct_thread_count above is an
            # aggregate counter, but correctly incrementing it needs to know
            # WHICH thread_keys have already been counted for a given
            # pattern_signature (Marc's own significance bar is "2+ genuinely
            # DISTINCT threads," not just 2+ occurrences - 5 copies of the
            # same forwarded email on one thread must not count). This table
            # is that membership set - INSERT OR IGNORE on (signature,
            # thread_key) is a real, cheap way to ask "have we already
            # counted this thread for this pattern?" without rescanning
            # every raw_item's text on every new occurrence.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pattern_observation_threads (
                    pattern_signature TEXT NOT NULL,
                    thread_key        TEXT NOT NULL,
                    PRIMARY KEY (pattern_signature, thread_key)
                )
            """)

            # Task #232 (2026-08-06): the live add-in assistant's Claude
            # session id, persisted SERVER-SIDE instead of living only in
            # the task pane's own JS variable - a pane reload/reopen (or,
            # eventually, a second host like Teams) can pick the same
            # ongoing conversation back up instead of silently starting a
            # brand-new one. Single-row table (id fixed to 'default') -
            # Jasper is single-user right now; a real per-user key can be
            # added if that ever changes, without a schema change (id
            # would just stop being a constant).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS assistant_sessions (
                    id           TEXT PRIMARY KEY,
                    session_id   TEXT NOT NULL,
                    updated_ts   REAL NOT NULL
                )
            """)

            # artifact_lineages/artifact_versions (design doc Section 12.5):
            # the real answer to attachment-hashing's open question - sha256
            # was already computed on every attachment (task #29) but never
            # read for anything beyond upload-time dedup
            # (find_attachment_by_hash). id is deterministic
            # (lineage-<first 16 hex chars of the sha256>), not a random
            # UUID - same content-derived-id convention source_containers
            # already uses. Real producer, wired live (not just backfilled):
            # create_attachment (below) links a NEW attachment into an
            # existing/new lineage the moment a SECOND attachment with the
            # same hash shows up - a genuinely unique hash never gets a
            # speculative lineage of its own, since nothing today can
            # connect it to a later real version by content alone (no
            # redline-detection producer exists - see create_artifact_
            # version's own docstring on document_role).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS artifact_lineages (
                    id             TEXT PRIMARY KEY,
                    work_object_id TEXT REFERENCES work_objects(id),
                    title          TEXT NOT NULL,
                    created_ts     REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_artifact_lineages_work_object ON artifact_lineages(work_object_id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS artifact_versions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    lineage_id      TEXT NOT NULL REFERENCES artifact_lineages(id),
                    attachment_id   INTEGER NOT NULL REFERENCES attachments(id),
                    document_role   TEXT NOT NULL CHECK (document_role IN
                                       ('original','redline','counter_redline','clean_copy',
                                        'executed_copy','exhibit','other')),
                    derived_from_id INTEGER REFERENCES artifact_versions(id),
                    sha256          TEXT NOT NULL,
                    created_ts      REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_artifact_versions_lineage ON artifact_versions(lineage_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_artifact_versions_attachment ON artifact_versions(attachment_id)")

            # prepared_actions (design doc Section 12.4): the real execution-
            # safety layer between a candidate action and dispatching it.
            # Real producer/consumer: server_lean.py's /api/cockpit/actions
            # route - the ONE real dispatch point in this codebase (every
            # action today is human-click-initiated via the cockpit UI, then
            # relayed to a worker over team_room; nothing here is ever
            # autonomously triggered). claim_id/evidence_refs stay NULL/[]
            # for a cockpit action not tied to one specific claim (an issue-
            # level "summarize" isn't about a single ask/commitment) -
            # honest, not guessed. required_approval defaults to 1 and is
            # trivially satisfied by the human click that creates the row -
            # no separate approval GATE exists yet (named gap, same
            # discipline as everywhere else in this doc). idempotency_key
            # is what actually earns this table its keep today: real,
            # observable risk (a double-click, a browser retry) getting a
            # concrete block, not a rewrite of the full autonomous-execution
            # lifecycle the state list describes - most of that lifecycle
            # (executing -> succeeded/failed/uncertain) has no real resolver
            # yet, since nothing reports a worker's real-world outcome back
            # (the same pre-existing gap pending_actions.status has always
            # had - update_pending_action_status has zero callers).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prepared_actions (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id             INTEGER REFERENCES claims(id),
                    action_type          TEXT NOT NULL,
                    proposed_parameters  TEXT NOT NULL,
                    evidence_refs        TEXT NOT NULL,
                    rationale            TEXT NOT NULL,
                    risk_class           TEXT NOT NULL CHECK (risk_class IN ('low','medium','high')),
                    required_approval    INTEGER NOT NULL DEFAULT 1,
                    policy_result        TEXT,
                    state                TEXT NOT NULL DEFAULT 'proposed' CHECK (state IN
                                            ('proposed','ready_for_approval','approved',
                                             'executing','succeeded','failed','uncertain',
                                             'rejected','expired','cancelled')),
                    idempotency_key      TEXT NOT NULL UNIQUE,
                    created_ts           REAL NOT NULL,
                    resolved_ts          REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prepared_actions_claim ON prepared_actions(claim_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prepared_actions_state ON prepared_actions(state, created_ts)")

            try:
                # Bumped once per raw_item at claim-materialization time (i.e. in
                # ingestion order, never in the item's own occurred_ts) - the
                # cursor synthesis staleness needed and occurred_ts structurally
                # can't provide (Section 9.5 - D9/D10's real root cause: a late-
                # arriving, old-timestamped item was correctly flagged stale at
                # the top level but invisible to the occurred_ts-keyed delta that
                # decides what's actually new to (re-)synthesize).
                conn.execute("ALTER TABLE issues ADD COLUMN claims_revision INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # already added by a prior init_workgraph() call

            # Full-text evidence index (Section 9.6) over the same body
            # text_extract.resolve_item_text() already resolves from local
            # files (never a live Outlook/Graph call) - safe to backfill over
            # the whole historical corpus. Feeds Evidence Assembly's
            # (Section 8.1) full-text candidate path and Project Deep-Dive's
            # (Section 8.4) in-corpus search, before either falls back to live
            # M365 search.
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
                    body, raw_item_id UNINDEXED, issue_id UNINDEXED,
                    tokenize='porter unicode61'
                )
            """)

            # --- Attachments: relates a real file on disk to a specific issue,
            # project, or chat conversation - not just a filename mentioned in a
            # message. The file itself lives under DOCUMENTS_DIR (see paths.py),
            # stored_path is relative to that root. -------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS attachments (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type  TEXT NOT NULL,   -- 'issue' | 'project' | 'chat'
                    entity_id    TEXT,            -- issue_id / project_id / worker name (chat uploads may be unscoped)
                    kind         TEXT NOT NULL DEFAULT 'upload',  -- 'reference' | 'output' | 'upload'
                    filename     TEXT NOT NULL,
                    stored_path  TEXT NOT NULL,   -- relative to DOCUMENTS_DIR
                    content_type TEXT,
                    size_bytes   INTEGER,
                    sha256       TEXT,
                    uploaded_by  TEXT,
                    uploaded_ts  REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_attach_entity ON attachments(entity_type, entity_id)")

            # --- Total Recall: a small, deterministic precedent store (see
            # workgraph_lessons.py). One row per (situation_key, outcome) -
            # repeats bump trust_score/hit_count on the SAME row rather than
            # growing a new one, so trust reflects a track record, not a count
            # of writes. No embeddings, no similarity model, no LLM call in
            # this module or anything that reads from it. -----------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lessons (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    situation_key    TEXT NOT NULL,
                    outcome          TEXT NOT NULL CHECK (outcome IN ('confirmed','rejected','resolved')),
                    statement        TEXT NOT NULL,
                    source_issue_id  TEXT NOT NULL REFERENCES issues(id),
                    trust_score      REAL NOT NULL DEFAULT 0.6,
                    hit_count        INTEGER NOT NULL DEFAULT 1,
                    created_ts       REAL NOT NULL,
                    last_applied_ts  REAL,
                    UNIQUE(situation_key, outcome)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_situation ON lessons(situation_key)")

            try:
                # Which lesson (if any) contributed to this issue's current
                # priority_score - resolved into the cockpit's `learned` badge
                # at read time (see workgraph_lessons.attach_learned). Set by
                # workgraph_nba.recompute_all alongside priority_score/nba_reason.
                conn.execute("ALTER TABLE issues ADD COLUMN lesson_id_cited INTEGER REFERENCES lessons(id)")
            except sqlite3.OperationalError:
                pass
            try:
                # Set by workgraph_nba.recompute_all() alongside priority_score/
                # nba_reason - lets workgraph_alerts.py (task #55) cheaply query
                # "which issues have an unmet Aristotle prerequisite right now"
                # without recomputing workgraph_aristotle.check_prerequisites()
                # a second time for every issue on every alert scan.
                conn.execute("ALTER TABLE issues ADD COLUMN has_unmet_prerequisite INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # already added by a prior init_workgraph() call

            # --- work_objects (design doc Section 12.1, 2026-08-03) - one real,
            # typed, nestable table replacing the issues/projects split, per
            # Marc's explicit "build it right, migrate don't wrap" instruction.
            # Everything above this point is untouched, real, load-bearing
            # setup for a brand-new install (or a no-op on an already-migrated
            # one, per the note further up) - by this point `issues`/
            # `projects` are guaranteed to be fully-formed real tables with
            # every column the rest of this function ever added. This block
            # migrates them ONCE, then every future call just ensures the
            # views/triggers exist.
            work_objects_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_objects'"
            ).fetchone() is not None

            conn.execute("""
                CREATE TABLE IF NOT EXISTS work_objects (
                    id                     TEXT PRIMARY KEY,
                    object_type            TEXT NOT NULL CHECK (object_type IN
                                              ('relationship','program','project','engagement',
                                               'case','request','recurring_responsibility')),
                    parent_id              TEXT REFERENCES work_objects(id),
                    title                  TEXT NOT NULL,
                    category               TEXT,
                    status                 TEXT NOT NULL CHECK (status IN
                                              ('active','waiting','blocked','done',
                                               'noise-archived','dismissed','archived')),
                    priority               TEXT CHECK (priority IN ('high','med','low')),
                    priority_score         REAL,
                    nba_action_kind        TEXT CHECK (nba_action_kind IN
                                              ('draft','review','approve','chase','wait','read','none')),
                    nba_reason             TEXT,
                    owner                  TEXT NOT NULL DEFAULT 'marc',
                    due                    TEXT,
                    confidence_tier        TEXT CHECK (confidence_tier IN ('H','M','L')),
                    lesson_id_cited        INTEGER REFERENCES lessons(id),
                    has_unmet_prerequisite INTEGER NOT NULL DEFAULT 0,
                    claims_revision        INTEGER NOT NULL DEFAULT 0,
                    last_deep_dive_ts      REAL,
                    last_deep_dive_note    TEXT,
                    opened_at              REAL NOT NULL,
                    updated_at             REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_work_objects_type_status ON work_objects(object_type, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_work_objects_parent ON work_objects(parent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_work_objects_priority ON work_objects(priority_score DESC)")

            if not work_objects_exists:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    # Real, reproducible race (same live-suite run that
                    # found the busy_timeout gap above): with busy_timeout
                    # now set, a process that loses this lock WAITS instead
                    # of failing instantly - but that means by the time it
                    # finally acquires the lock, another process may have
                    # ALREADY completed this exact migration (issues
                    # already renamed to issues_pre_workobjects, work_
                    # objects already populated). Without this re-check,
                    # re-running the INSERTs below would hit a PRIMARY KEY
                    # collision (sqlite3.IntegrityError, NOT caught by the
                    # OperationalError handler below - a hard crash, not a
                    # graceful skip) since the SAME issue ids would already
                    # be in work_objects. work_objects_exists (checked
                    # BEFORE this process even tried for the lock) is
                    # stale the moment any other process could have raced
                    # ahead of it, so it's not safe to trust here - only a
                    # check taken AFTER actually holding the write lock is.
                    issues_still_a_table = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='issues'"
                    ).fetchone() is not None
                    if not issues_still_a_table:
                        conn.execute("COMMIT")
                        raise _MigrationAlreadyDone()
                    # Real issue columns, driven by PRAGMA table_info rather than
                    # a hardcoded guess - same "never assume the column set"
                    # discipline as every other rename+rebuild migration in this
                    # file, since issues has grown columns via ALTER TABLE many
                    # times since its original CREATE TABLE.
                    issue_cols = {r["name"] for r in conn.execute("PRAGMA table_info(issues)").fetchall()}
                    conn.execute(f"""
                        INSERT INTO work_objects
                            (id, object_type, parent_id, title, category, status, priority,
                             priority_score, nba_action_kind, nba_reason, owner, due,
                             confidence_tier, lesson_id_cited, has_unmet_prerequisite,
                             claims_revision, opened_at, updated_at)
                        SELECT id, 'request', {"project_id" if "project_id" in issue_cols else "NULL"},
                               title, category, state, priority,
                               priority_score, nba_action_kind, nba_reason, owner, due,
                               confidence_tier,
                               {"lesson_id_cited" if "lesson_id_cited" in issue_cols else "NULL"},
                               {"has_unmet_prerequisite" if "has_unmet_prerequisite" in issue_cols else "0"},
                               {"claims_revision" if "claims_revision" in issue_cols else "0"},
                               opened_at, updated_at
                        FROM issues
                    """)
                    project_cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
                    conn.execute(f"""
                        INSERT INTO work_objects
                            (id, object_type, parent_id, title, category, status, owner,
                             opened_at, updated_at, last_deep_dive_ts, last_deep_dive_note)
                        SELECT id, 'project', NULL, name, category, status, 'marc',
                               opened_at, updated_at,
                               {"last_deep_dive_ts" if "last_deep_dive_ts" in project_cols else "NULL"},
                               {"last_deep_dive_note" if "last_deep_dive_note" in project_cols else "NULL"}
                        FROM projects
                    """)
                    conn.execute("ALTER TABLE issues RENAME TO issues_pre_workobjects")
                    conn.execute("ALTER TABLE projects RENAME TO projects_pre_workobjects")
                    conn.execute("COMMIT")
                except _MigrationAlreadyDone:
                    pass  # already committed above - nothing to roll back
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass

            # Design doc Section 12.8: provisional-vs-confirmed grouping +
            # exposure tracking, additive to the already-migrated
            # work_objects table. membership_state defaults 'provisional'
            # for every row (matches the schema's own DEFAULT - an auto-
            # merge/auto-link the deterministic matcher made with no human
            # confirmation yet); only a real human-confirm event
            # (workgraph_projects.confirm_suggestion) ever sets 'confirmed'.
            # exposure_state advances forward-only (see advance_work_object_
            # exposure_state below) from three real render points: the
            # Project Detail route (shown_in_project), upsert_synthesis
            # (used_in_summary), api_cockpit_action (used_for_action).
            try:
                conn.execute(
                    "ALTER TABLE work_objects ADD COLUMN membership_state TEXT NOT NULL DEFAULT 'provisional' "
                    "CHECK (membership_state IN ('provisional','confirmed'))"
                )
            except sqlite3.OperationalError:
                pass

            # Corrected-ordering redesign (2026-08-05, Marc's direct correction):
            # a "cluster" is the raw pass-1/pass-2 matching unit - never a real,
            # individually-tracked issue on its own. Deliberately NOT a new
            # object_type value (that would require rebuilding the live
            # work_objects table to change its existing object_type CHECK
            # constraint - SQLite enforces whatever CHECK was stored at
            # CREATE TABLE time, not whatever a later CREATE TABLE IF NOT
            # EXISTS statement says, and this table is the busiest, most
            # central one in the whole app with a live server currently
            # running against it - real, avoidable risk for zero benefit
            # over a plain boolean column, which needs only a safe ALTER
            # TABLE ADD COLUMN). is_raw_cluster=1 rows are still real
            # object_type='request' work_objects (same shape everything
            # already expects), just excluded from the `issues` view below -
            # invisible to the Inbox/NBA/checklist/evidence UI by
            # construction, no filter for any downstream caller to
            # remember, exactly the property the plan calls for.
            try:
                conn.execute(
                    "ALTER TABLE work_objects ADD COLUMN is_raw_cluster INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "ALTER TABLE work_objects ADD COLUMN exposure_state TEXT NOT NULL DEFAULT 'not_exposed' "
                    "CHECK (exposure_state IN ('not_exposed','shown_in_project','used_in_summary','used_for_action'))"
                )
            except sqlite3.OperationalError:
                pass

            # `issues`/`projects` as views over work_objects - every existing
            # caller across the whole codebase keeps working completely
            # unchanged (confirmed empirically: partial-column INSERT/UPDATE,
            # matching create_issue()/update_issue()'s own exact patterns,
            # both behave identically to a real table). INSTEAD OF triggers
            # make them fully read/write, not just read-only projections.
            # Corrected-ordering redesign (2026-08-05): the view itself must
            # be dropped and recreated (not CREATE VIEW IF NOT EXISTS,
            # which would silently skip re-applying the is_raw_cluster
            # filter on an already-migrated live install) - views carry no
            # data, so this is instant and safe, unlike a table rebuild.
            # The INSTEAD OF triggers below are untouched: every row ever
            # inserted THROUGH this view (i.e. every real create_issue call)
            # still lands with is_raw_cluster at its column default (0) -
            # only the separate, direct-to-work_objects cluster-creation
            # path (workgraph_store.create_cluster) ever sets it to 1.
            # Fixed 2026-08-05, real race caught by test_multiprocess_
            # concurrency.py's own stress test: DROP+CREATE isn't atomic
            # across separate connections/processes (each init_workgraph()
            # call opens its own autocommit connection - a view carries no
            # data, so there's no transaction wrapping the two statements
            # together) - two processes waking at once could both pass the
            # DROP (a no-op once the view is already gone) and then race on
            # the bare CREATE VIEW, the second one raising "view issues
            # already exists". The desired end state (a correctly-defined
            # `issues` view) is reached either way - whichever process's
            # CREATE actually won already put the SAME definition in place.
            try:
                conn.execute("DROP VIEW IF EXISTS issues")
                conn.execute("""
                    CREATE VIEW issues AS
                    SELECT id, title, category, status AS state, priority, priority_score,
                           nba_action_kind, nba_reason, owner, due, opened_at, updated_at,
                           confidence_tier, parent_id AS project_id, lesson_id_cited,
                           has_unmet_prerequisite, claims_revision
                    FROM work_objects WHERE object_type = 'request' AND is_raw_cluster = 0
                """)
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_issues_insert INSTEAD OF INSERT ON issues
                BEGIN
                    INSERT INTO work_objects
                        (id, object_type, parent_id, title, category, status, priority,
                         priority_score, nba_action_kind, nba_reason, owner, due,
                         opened_at, updated_at, confidence_tier, lesson_id_cited,
                         has_unmet_prerequisite, claims_revision)
                    VALUES
                        (NEW.id, 'request', NEW.project_id, NEW.title, NEW.category, NEW.state,
                         NEW.priority, NEW.priority_score, NEW.nba_action_kind, NEW.nba_reason,
                         NEW.owner, NEW.due, NEW.opened_at, NEW.updated_at, NEW.confidence_tier,
                         NEW.lesson_id_cited, COALESCE(NEW.has_unmet_prerequisite, 0),
                         COALESCE(NEW.claims_revision, 0));
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_issues_update INSTEAD OF UPDATE ON issues
                BEGIN
                    UPDATE work_objects SET
                        title = NEW.title, category = NEW.category, status = NEW.state,
                        priority = NEW.priority, priority_score = NEW.priority_score,
                        nba_action_kind = NEW.nba_action_kind, nba_reason = NEW.nba_reason,
                        owner = NEW.owner, due = NEW.due,
                        opened_at = NEW.opened_at, updated_at = NEW.updated_at,
                        confidence_tier = NEW.confidence_tier, parent_id = NEW.project_id,
                        lesson_id_cited = NEW.lesson_id_cited,
                        has_unmet_prerequisite = NEW.has_unmet_prerequisite,
                        claims_revision = NEW.claims_revision
                    WHERE id = OLD.id;
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_issues_delete INSTEAD OF DELETE ON issues
                BEGIN
                    DELETE FROM work_objects WHERE id = OLD.id;
                END
            """)

            conn.execute("""
                CREATE VIEW IF NOT EXISTS projects AS
                SELECT id, title AS name, category, status, opened_at, updated_at,
                       last_deep_dive_ts, last_deep_dive_note
                FROM work_objects WHERE object_type = 'project'
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_projects_insert INSTEAD OF INSERT ON projects
                BEGIN
                    INSERT INTO work_objects
                        (id, object_type, parent_id, title, category, status, owner,
                         opened_at, updated_at, last_deep_dive_ts, last_deep_dive_note)
                    VALUES
                        (NEW.id, 'project', NULL, NEW.name, NEW.category, NEW.status, 'marc',
                         NEW.opened_at, NEW.updated_at, NEW.last_deep_dive_ts, NEW.last_deep_dive_note);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_projects_update INSTEAD OF UPDATE ON projects
                BEGIN
                    UPDATE work_objects SET
                        title = NEW.name, category = NEW.category, status = NEW.status,
                        opened_at = NEW.opened_at, updated_at = NEW.updated_at,
                        last_deep_dive_ts = NEW.last_deep_dive_ts,
                        last_deep_dive_note = NEW.last_deep_dive_note
                    WHERE id = OLD.id;
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_projects_delete INSTEAD OF DELETE ON projects
                BEGIN
                    DELETE FROM work_objects WHERE id = OLD.id;
                END
            """)

            # --- evidence_units (design doc Section 12.2, 2026-08-03): removes
            # the exact structural constraint confirmed in evidence's own
            # schema - issue_id was a MANDATORY single FK, so one raw_item's
            # evidence could only ever belong to one work_object. Real data
            # checked before building this: 0 of 804 real raw_item_ids already
            # had more than one evidence row, and 0 were already linked to two
            # different issues - the constraint was never exercised, but was
            # still real and worth removing before it ever needed to be.
            # evidence_unit_links.evidence_unit_id/work_object_id is a genuine
            # many-to-many join - the same evidence can attach to more than
            # one work_object going forward (add_evidence's own contract is
            # unchanged: still always creates a new evidence_unit, same as it
            # always created a new evidence row - linking ONE unit to a
            # SECOND work_object is a new, separate capability, not automatic
            # dedup on write).
            evidence_units_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_units'"
            ).fetchone() is not None

            conn.execute("""
                CREATE TABLE IF NOT EXISTS evidence_units (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_item_id INTEGER,
                    type        TEXT NOT NULL CHECK (type IN ('email','teams','calendar','sharepoint','worker_action')),
                    summary     TEXT NOT NULL,
                    ts          REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_units_raw_item ON evidence_units(raw_item_id)")
            # WITHOUT ROWID, deliberately: a pure link table with a real
            # composite key needs no synthetic rowid of its own - and
            # confirmed empirically that giving it one would make
            # add_evidence()'s reliance on cursor.lastrowid unreliable, since
            # SQLite reverts last_insert_rowid() to its PRE-trigger value once
            # an INSTEAD OF trigger finishes (documented SQLite behavior,
            # verified directly rather than assumed) - a rowid-bearing second
            # insert inside the same trigger would be the last thing touched
            # and would clobber the caller's view of evidence_units' own new
            # id. add_evidence() itself was rewritten to insert into these two
            # real tables directly rather than through the `evidence` view,
            # sidestepping this entirely for the one caller that needs the id
            # back; the view+triggers below still exist for every other real
            # caller (list_evidence, the bulk issue_id reparent in
            # merge_issue_into, and any raw INSERT that doesn't need its id).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evidence_unit_links (
                    evidence_unit_id INTEGER NOT NULL REFERENCES evidence_units(id),
                    work_object_id   TEXT NOT NULL REFERENCES work_objects(id),
                    PRIMARY KEY (evidence_unit_id, work_object_id)
                ) WITHOUT ROWID
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_unit_links_wo ON evidence_unit_links(work_object_id)")

            if not evidence_units_exists:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("""
                        INSERT INTO evidence_units (id, raw_item_id, type, summary, ts)
                        SELECT id, raw_item_id, type, summary, ts FROM evidence
                    """)
                    conn.execute("""
                        INSERT INTO evidence_unit_links (evidence_unit_id, work_object_id)
                        SELECT id, issue_id FROM evidence
                    """)
                    conn.execute("ALTER TABLE evidence RENAME TO evidence_pre_evidenceunits")
                    conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass

            conn.execute("""
                CREATE VIEW IF NOT EXISTS evidence AS
                SELECT eu.id AS id, eul.work_object_id AS issue_id, eu.raw_item_id,
                       eu.type, eu.summary, eu.ts
                FROM evidence_units eu JOIN evidence_unit_links eul ON eul.evidence_unit_id = eu.id
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_evidence_insert INSTEAD OF INSERT ON evidence
                BEGIN
                    INSERT INTO evidence_units (raw_item_id, type, summary, ts)
                    VALUES (NEW.raw_item_id, NEW.type, NEW.summary, NEW.ts);
                    INSERT INTO evidence_unit_links (evidence_unit_id, work_object_id)
                    VALUES (last_insert_rowid(), NEW.issue_id);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_evidence_update INSTEAD OF UPDATE ON evidence
                BEGIN
                    UPDATE evidence_units SET
                        raw_item_id = NEW.raw_item_id, type = NEW.type,
                        summary = NEW.summary, ts = NEW.ts
                    WHERE id = OLD.id;
                    UPDATE evidence_unit_links SET work_object_id = NEW.issue_id
                    WHERE evidence_unit_id = OLD.id AND work_object_id = OLD.issue_id;
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_evidence_delete INSTEAD OF DELETE ON evidence
                BEGIN
                    DELETE FROM evidence_unit_links
                        WHERE evidence_unit_id = OLD.id AND work_object_id = OLD.issue_id;
                    DELETE FROM evidence_units WHERE id = OLD.id
                        AND NOT EXISTS (SELECT 1 FROM evidence_unit_links WHERE evidence_unit_id = OLD.id);
                END
            """)

            # Socrates-for-Jasper (#see workgraph_socrates.py): one row per TIER
            # consulted per question, not one row per question - this is what
            # makes "which tier actually answers this shape of question"
            # (workgraph_socrates.source_outcomes) a plain GROUP BY, same
            # audit+learning role as Theo's retrieval-log.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS socrates_retrieval_log (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    asked_ts     REAL NOT NULL,
                    asker        TEXT,
                    question     TEXT NOT NULL,
                    signature    TEXT NOT NULL,
                    tier         TEXT NOT NULL CHECK (tier IN ('recall','materialized','targeted-research','broad-research')),
                    band         TEXT NOT NULL CHECK (band IN ('none','low','medium','high')),
                    contributed  INTEGER NOT NULL DEFAULT 0,
                    outcome      TEXT NOT NULL CHECK (outcome IN ('answered','degraded','abstained'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_socrates_log_signature ON socrates_retrieval_log(signature)")

            # Known-automated-signal recognition (workgraph_signals.py): the
            # PATTERN that recognizes a signal type (Ariba PR approval, Adobe
            # Sign/DocuSign, ContractPodAI, etc.) is code; its TREATMENT
            # (noise|fyi|actionable|closure) is correctable HERE, live, without
            # a code change - same "a correction sticks" discipline as
            # correct_party_affiliation.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_treatment_overrides (
                    signal_type TEXT PRIMARY KEY,
                    treatment   TEXT NOT NULL CHECK (treatment IN ('noise','fyi','actionable','closure')),
                    reason      TEXT,
                    set_ts      REAL NOT NULL,
                    set_by      TEXT
                )
            """)

            try:
                # Which known-automated-signal type (if any) this item matched,
                # and the PR number extracted from it, when present - set by
                # workgraph_signals.classify_signal via workgraph_classify.py.
                conn.execute("ALTER TABLE raw_items ADD COLUMN signal_type TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE raw_items ADD COLUMN pr_number TEXT")
            except sqlite3.OperationalError:
                pass
            # Added 2026-07-30 (grouping/NBA redesign, Part A1/C): a new
            # reverse lookup (which issue already has this PR/PO number)
            # needs this - not indexed before since pr_number was only ever
            # read per-issue (get_raw_items_for_issue), never searched
            # across issues. Must come AFTER the ALTER TABLE above, not
            # alongside the other raw_items indices near CREATE TABLE - the
            # column doesn't exist yet at that point on a pre-existing DB.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_pr_number ON raw_items(pr_number)")
            try:
                # 2026-07-31 (meeting-grouping/related-project identity pass):
                # version-stripped identity for MATCHING only - pr_number above
                # keeps meaning "full string, for display/audit." Real bug this
                # fixes: PR1140347-V2 and PR1140347-V3 were two entirely
                # unrelated strings to every exact-match lookup, and the
                # disjoint-set veto in workgraph_projects.py treated that
                # mismatch as ACTIVELY CONTRADICTING evidence - worse than no
                # reference at all. Must come after the pr_number ALTER TABLE
                # above, same reasoning as idx_raw_pr_number below.
                conn.execute("ALTER TABLE raw_items ADD COLUMN pr_number_base TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_pr_number_base ON raw_items(pr_number_base)")
            try:
                # Task #36: raw candidate issue id extracted from a Jasper-
                # authored "Ref: JW-<id>" tag, if this item's text has one -
                # see workgraph_signals.jasper_ref_issue_id. NOT validated
                # against real issues here; that happens at link time
                # (workgraph_classify.cluster_and_link), same deferred-
                # validation split pr_number/pr_number_base already use.
                conn.execute("ALTER TABLE raw_items ADD COLUMN jasper_ref_issue_id TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_jasper_ref_issue_id ON raw_items(jasper_ref_issue_id)")
            try:
                # 2026-07-31 (meeting-grouping/related-project identity pass):
                # auditability for the calendar thread_key fix - a heuristic
                # synthetic series key needs to be diagnosable later, not
                # re-guessed. NULL for every non-calendar source. Values:
                # 'graph_series_master_id' | 'synthetic_calendar_series' |
                # 'stable_key_fallback'.
                conn.execute("ALTER TABLE raw_items ADD COLUMN thread_key_source TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                # Real Graph payload field (ev["isOrganizer"]), previously
                # discarded - 1/0/NULL. NULL is a real, legitimate "unknown"
                # (confirmed inconsistently present across capture calls) -
                # never silently defaulted to 0.
                conn.execute("ALTER TABLE raw_items ADD COLUMN is_organizer INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                # E7 (enhancement idea panel #7, 2026-08-03): generic per-
                # source JSON extras column, not a pile of source-specific
                # NULL-for-everyone-else columns. First real use: calendar's
                # location/isCancelled/webLink/showAs/importance/recurrence
                # fields - all present for free in the outlook_calendar_
                # search response (confirmed live this session) but
                # discarded until now. A JSON blob here (rather than one
                # column per field) keeps this table from growing a new
                # ALTER TABLE for every source that eventually wants its own
                # extra field.
                conn.execute("ALTER TABLE raw_items ADD COLUMN meta_json TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                # Outlook's own message identifier - the PS scan has always
                # emitted this, but until now it was only used transiently to
                # build stable_key/dedupe_key and never persisted. Needed to
                # open the exact source item later (task #43/#46) via
                # Outlook COM's GetItemFromID, which EntryID alone can do -
                # ConversationID (already in stable_key) only gets you the
                # thread, not this specific message.
                conn.execute("ALTER TABLE raw_items ADD COLUMN entry_id TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                # Real audit-trail gap found investigating a real incident: two
                # financial issues (marc-014 $53.7M, marc-185 $111.7M) were
                # flipped active->done with zero closing evidence, and there was
                # no way to tell whether that came from the automated
                # recompute_issue_state() rule or a manual/bulk status-change
                # click - issue_state_history had (issue_id, from_state,
                # to_state, changed_ts) and nothing else. NULL for every
                # historical row (including the automated-rule transitions
                # that predate this column) and for update_issue()'s own
                # internal callers that don't pass one - "unknown" is honest
                # there, not a guess.
                conn.execute("ALTER TABLE issue_state_history ADD COLUMN actor TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                # Real, currently-active bug found investigating the same
                # 2026-08-01 incident: get_items_pending_link() pulled the
                # oldest 500 unlinked rows on every single run, oldest-first,
                # and a permanently-skipped row (NOISE, or a standalone FYI
                # with no thread/reference match - see cluster_and_link's own
                # skip branches) never left that pool. Once the permanent-
                # skip backlog passed 500, EVERY run re-examined the same
                # doomed rows and never reached anything newer - confirmed
                # live: 70 raw_items ingested after the last successful link
                # were all classified but zero were linked, including real
                # ACTIONABLE-ASK items that should have opened issues
                # immediately. last_link_check_ts (stamped by cluster_and_link
                # on every row it examines-and-skips, never on one it
                # successfully links) lets get_items_pending_link put
                # never-yet-examined rows ahead of already-skipped ones,
                # regardless of which is chronologically older - a skipped
                # row can still be reconsidered later (a sibling landing on
                # the same thread_key can rescue it), just no longer at the
                # front of the queue forever.
                conn.execute("ALTER TABLE raw_items ADD COLUMN last_link_check_ts REAL")
            except sqlite3.OperationalError:
                pass
            try:
                # Task #54/#55 (2026-08-02, Marc's direct report on Teams
                # clutter): classify_item() has always computed a real H/M/L
                # confidence tier, but nothing ever persisted it - it was
                # thrown away the moment cluster_and_link finished reading
                # it. NULL for every row classified before this column
                # existed, and for any row a future classify_item() call
                # somehow skips - never guessed at retroactively.
                conn.execute("ALTER TABLE raw_items ADD COLUMN confidence TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                # Task #54/#55: a Teams ACTIONABLE-ASK/WAITING-ON-OTHERS item
                # with no thread/reference match now gets held aside instead
                # of always spawning a new Issue (see workgraph_classify.
                # cluster_and_link's own comment) - this is how a human
                # resolves one from the held-aside queue. NULL = not yet
                # reviewed (the normal, expected state for most rows).
                # 'tracked' = a real Issue was created from it manually.
                # 'dismissed' = reviewed and confirmed not worth tracking.
                # Deliberately a plain TEXT column with no CHECK constraint -
                # the projects.status/issues.state CHECK-widening migrations
                # this same session both needed a rename+rebuild dance to add
                # one new value; a bare column sidesteps that entirely for a
                # field with no fixed vocabulary to defend yet.
                conn.execute("ALTER TABLE raw_items ADD COLUMN held_aside_status TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                # Task #29 (2026-08-01): attachments were stored on disk but
                # never read by any extraction function - a real order-form
                # PDF or pricing XLSX sitting right there was structurally
                # invisible to the value/asks extraction that only ever
                # scanned email subject+body. Extracted once at absorb time
                # (attachment_extract.py, text-layer/cell-value only, no
                # OCR), cached here rather than re-extracted on every read.
                conn.execute("ALTER TABLE attachments ADD COLUMN extracted_text TEXT")
            except sqlite3.OperationalError:
                pass

            # Personal Response Learning (task #45) - deliberately its OWN
            # table, not folded into lessons/signal_treatment_overrides: this
            # is behavioral data about Marc himself, not classification
            # precedent, and needs to be independently purgeable (Settings'
            # "Forget what's been learned") without touching anything else.
            # Off by default - see config.get("personal_learning", ...) and
            # personal_patterns.py's module docstring for the full gate.
            # Aristotle (task #51) - a taught, not-inferred prerequisite/gate
            # rule: "a raw_item classified as trigger_signal_type shouldn't
            # be treated as ready to act on until requires_signal_type has
            # been seen for the same project/supplier". Rules are only ever
            # created by explicit Settings input (see server_lean.py's
            # /api/settings/prerequisite-rules) - nothing here is inferred
            # from patterns in the mail, matching workgraph_signals.py's own
            # "none of this is guessed" discipline for the signal types these
            # rules reference.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prerequisite_rules (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_signal_type   TEXT NOT NULL,
                    requires_signal_type  TEXT NOT NULL,
                    match_on              TEXT NOT NULL CHECK (match_on IN ('project','supplier')),
                    reason                TEXT,
                    active                INTEGER NOT NULL DEFAULT 1,
                    created_ts            REAL NOT NULL,
                    created_by            TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prereq_trigger ON prerequisite_rules(trigger_signal_type, active)")

            # Aristotle candidate-rule suggestions (task #52/#54) - a shared
            # propose-then-confirm queue for BOTH origins: deterministic
            # correlation detection (origin='detected', task #52) and chat-
            # taught explanations (origin='taught_via_chat', task #54).
            # trigger_signal_type/requires_signal_type/match_on are nullable:
            # a chat-taught explanation that couldn't be confidently
            # structured still gets logged (raw_explanation always present
            # for that origin) even with no structured fields yet - visible
            # and reviewable, never silently dropped.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_prerequisite_suggestions (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin                TEXT NOT NULL CHECK (origin IN ('detected','taught_via_chat')),
                    trigger_signal_type   TEXT,
                    requires_signal_type  TEXT,
                    match_on              TEXT CHECK (match_on IS NULL OR match_on IN ('project','supplier')),
                    reason                TEXT,
                    evidence              TEXT,
                    raw_explanation       TEXT,
                    proposed_by           TEXT,
                    status                TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','rejected')),
                    created_ts            REAL NOT NULL,
                    resolved_ts           REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prereq_suggestions_status ON pending_prerequisite_suggestions(status, created_ts DESC)")
            try:
                # task #62: which clarifying question (if any) this suggestion
                # is mid-conversation on - NULL means "not in a clarification
                # conversation" (the normal case: either fully structured
                # already, or nobody's chosen to walk through it). Lives on
                # the suggestion row itself rather than a separate table,
                # since the fields being clarified (trigger_signal_type/
                # requires_signal_type/match_on) already ARE this row's own
                # columns - clarification just fills them in one at a time.
                conn.execute(
                    "ALTER TABLE pending_prerequisite_suggestions ADD COLUMN clarify_stage TEXT "
                    "CHECK (clarify_stage IS NULL OR clarify_stage IN "
                    "('offered','ask_trigger','ask_requires','ask_match_on'))"
                )
            except sqlite3.OperationalError:
                pass  # already added by a prior init_workgraph() call

            conn.execute("""
                CREATE TABLE IF NOT EXISTS response_patterns (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_surface TEXT NOT NULL,   -- app_chat | sent_mail | sent_teams
                    pattern_key    TEXT NOT NULL,   -- normalized keyword/phrase, e.g. "ariba"
                    example_text   TEXT,            -- most recent matching text ("cited as precedent")
                    hit_count      INTEGER NOT NULL DEFAULT 0,
                    first_seen_ts  REAL NOT NULL,
                    last_seen_ts   REAL NOT NULL,
                    UNIQUE(source_surface, pattern_key)
                )
            """)
        finally:
            conn.close()


# --- raw_items --------------------------------------------------------------

def insert_raw_item(
    *,
    source: str,
    stable_key: str,
    thread_key: str,
    dedupe_key: str,
    occurred_ts: float,
    subject: Optional[str] = None,
    from_actor: Optional[str] = None,
    participants_json: str = "[]",
    body_preview: Optional[str] = None,
    raw_ref: Optional[str] = None,
    entry_id: Optional[str] = None,
    thread_key_source: Optional[str] = None,
    is_organizer: Optional[int] = None,
    meta_json: Optional[str] = None,
) -> Optional[int]:
    """Insert one raw item. Returns the new row id, or None if it was a duplicate
    (dedupe_key already present — first write wins, matching the reference
    append-only dedup semantics)."""
    with _lock:
        conn = _connect()
        try:
            try:
                cur = conn.execute(
                    """INSERT INTO raw_items
                       (source, stable_key, thread_key, dedupe_key, occurred_ts,
                        subject, from_actor, participants, body_preview, raw_ref, ingested_ts, entry_id,
                        thread_key_source, is_organizer, meta_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source, stable_key, thread_key, dedupe_key, occurred_ts,
                     subject, from_actor, participants_json, body_preview, raw_ref, time.time(), entry_id,
                     thread_key_source, is_organizer, meta_json),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None  # duplicate dedupe_key, first write wins
        finally:
            conn.close()


def set_raw_item_raw_ref(row_id: int, raw_ref: str) -> None:
    """raw_ref can only be computed once the row's id is known (the staged
    body files land under a per-id document dir - task #43), so it's set as
    a follow-up update rather than passed to insert_raw_item, same two-step
    shape _absorb_attachments already uses for the documents it registers."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE raw_items SET raw_ref = ? WHERE id = ?", (raw_ref, row_id))
        finally:
            conn.close()


def get_items_pending_link(limit: int = 500) -> list[dict]:
    """Classified but not yet linked to an Issue (issue_id IS NULL).

    Ordered never-checked-first (last_link_check_ts IS NULL sorts before a
    real timestamp), THEN oldest first within each of those two groups -
    fixed 2026-08-01, real incident: plain oldest-first let an ever-growing
    pool of permanently-skipped rows (see cluster_and_link's skip branches,
    which stamp this column) crowd out every row newer than whenever that
    pool first exceeded `limit`. A never-yet-examined row - even one from
    months ago that just got classified - always outranks an already-
    skipped row for this run's budget; only once every fresh row is caught
    up does the budget spill into re-examining old skips (harmless, and
    occasionally rescues one via a sibling landing on the same thread_key
    since the last check)."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM raw_items WHERE classified = 1 AND issue_id IS NULL "
                "ORDER BY (last_link_check_ts IS NOT NULL), occurred_ts ASC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def mark_link_checked(raw_item_id: int, ts: float) -> None:
    """Stamped by cluster_and_link() on a row it examined-and-skipped (NOISE,
    or a standalone FYI with no thread/reference match) - never on one it
    successfully linked (that row leaves the pending pool via issue_id
    becoming non-NULL regardless, so stamping it would be dead weight). See
    get_items_pending_link's own docstring for what this fixes."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE raw_items SET last_link_check_ts = ? WHERE id = ?", (ts, raw_item_id))
        finally:
            conn.close()


def oldest_never_checked_unlinked_ts() -> Optional[float]:
    """occurred_ts of the OLDEST raw_item that is classified, unlinked, AND
    has never been examined by cluster_and_link() at all (last_link_check_ts
    IS NULL) - or None if there isn't one. Added for health_check.py (task
    #30, 2026-08-01): get_items_pending_link's never-checked-first ordering
    (this same day's earlier fix) means a genuinely never-yet-examined item
    should never wait long - if one this old exists, cluster_and_link() has
    stopped running entirely (a scheduling failure, an exception, or
    something else), not a normal backlog effect. This is the exact real
    incident that motivated that fix, made checkable going forward."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT MIN(occurred_ts) FROM raw_items "
                "WHERE classified = 1 AND issue_id IS NULL AND last_link_check_ts IS NULL"
            ).fetchone()
        finally:
            conn.close()
    return row[0] if row and row[0] is not None else None


def get_unclassified_raw_items(limit: int = 200) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM raw_items WHERE classified = 0 ORDER BY occurred_ts ASC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_all_classified_raw_items() -> list[dict]:
    """Every already-classified raw_item - used by
    workgraph_classify.backfill_reclassify_signals to re-check historical
    items against a signal ruleset that didn't exist (or wasn't wired in) at
    the time they were first classified, without touching anything else."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM raw_items WHERE classified = 1").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def classify_raw_item(
    raw_item_id: int,
    *,
    item_class: str,
    direction: str,
    direction_inferred: bool,
    topic: str,
    topic_inferred: bool,
    sentiment: str,
    sentiment_inferred: bool,
    anomaly_flag: bool,
    signal_type: Optional[str] = None,
    pr_number: Optional[str] = None,
    pr_number_base: Optional[str] = None,
    jasper_ref_issue_id: Optional[str] = None,
    confidence: Optional[str] = None,
) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """UPDATE raw_items SET
                       classified = 1, item_class = ?, direction = ?, direction_inferred = ?,
                       topic = ?, topic_inferred = ?, sentiment = ?, sentiment_inferred = ?,
                       anomaly_flag = ?, signal_type = ?, pr_number = ?, pr_number_base = ?,
                       jasper_ref_issue_id = ?, confidence = ?
                   WHERE id = ?""",
                (item_class, direction, int(direction_inferred), topic, int(topic_inferred),
                 sentiment, int(sentiment_inferred), int(anomaly_flag), signal_type, pr_number,
                 pr_number_base, jasper_ref_issue_id, confidence, raw_item_id),
            )
        finally:
            conn.close()


def get_items_with_anomaly(limit: int = 500) -> list[dict]:
    """Anomaly-flagged items (off-channel cue), newest first — feeds the
    alerts scanner's anomaly kind."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM raw_items WHERE anomaly_flag = 1 ORDER BY occurred_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_high_priority_actionable_items(limit: int = 500) -> list[dict]:
    """Classified ACTIONABLE-ASK items whose linked issue is priority='high' —
    feeds the alerts scanner's high_priority_ask kind."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT ri.* FROM raw_items ri
                   JOIN issues i ON ri.issue_id = i.id
                   WHERE ri.item_class = 'ACTIONABLE-ASK' AND i.priority = 'high'
                   ORDER BY ri.occurred_ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_raw_item(id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM raw_items WHERE id = ?", (id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def get_raw_items_by_ids(ids: list[int]) -> dict[int, dict]:
    """Batched form of get_raw_item - one query for a whole evidence list
    (deep_links.attach_deep_links) rather than one query per row, same N+1
    fix already applied to list_evidence_for_issues/
    list_issue_state_history_for_issues this session."""
    if not ids:
        return {}
    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT * FROM raw_items WHERE id IN ({placeholders})", ids,
            ).fetchall()
        finally:
            conn.close()
    return {r["id"]: dict(r) for r in rows}


def list_held_aside_teams_items(limit: int = 200) -> list[dict]:
    """Task #54/#55: every Teams raw_item currently sitting unlinked and
    not-yet-reviewed - the real, previously-invisible pile cluster_and_link
    already produces (NOISE/unmatched-FYI-EVIDENCE, and now unmatched-
    ACTIONABLE-ASK/WAITING-ON-OTHERS too - see cluster_and_link's own
    comment). Newest first - a human reviewing this wants to see what just
    happened, not dig through months of backlog first. held_aside_status
    IS NULL excludes anything already reviewed (tracked or dismissed)."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM raw_items WHERE source = 'teams_chat' AND classified = 1 "
                "AND issue_id IS NULL AND held_aside_status IS NULL "
                "ORDER BY occurred_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def set_held_aside_status(raw_item_id: int, status: str) -> None:
    """'tracked' (a real Issue was created from it) or 'dismissed' (reviewed,
    confirmed not worth tracking) - see the column's own migration comment."""
    if status not in ("tracked", "dismissed"):
        raise ValueError(f"invalid held_aside_status: {status!r}")
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE raw_items SET held_aside_status = ? WHERE id = ?", (status, raw_item_id))
        finally:
            conn.close()


def link_raw_item_to_issue(raw_item_id: int, issue_id: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE raw_items SET issue_id = ? WHERE id = ?", (issue_id, raw_item_id))
        finally:
            conn.close()
    invalidate_work_object_signature(issue_id)


def project_id_for_conversation_id(conversation_id: str) -> Optional[str]:
    """Add-in "focus on the email I have open" (task #240): Office.js's
    Office.context.mailbox.item.conversationId is the same underlying
    Exchange conversation-thread GUID Outlook COM exposes as
    $item.ConversationID, which ingest/outlook_scan.ps1 already writes
    straight into raw_items.stable_key for every outlook_mail row (line
    ~281) - so a direct raw_items lookup is the ground-truth path here
    (the old thread_map table this used to defer to was dead code - zero
    real callers anywhere - removed 2026-08-07 during the DB audit).
    Picks the most recently occurring linked
    raw_item in case a long thread's items ended up split across issues
    (rare, but real) - most recent is the honest "what Marc is looking at
    right now" answer."""
    if not conversation_id:
        return None
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT issue_id FROM raw_items
                   WHERE stable_key = ? AND issue_id IS NOT NULL
                   ORDER BY occurred_ts DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    # Deliberately NOT get_issue()/the `issues` view here - that view
    # filters to object_type='request' AND is_raw_cluster=0 (confirmed
    # live: raw_items.issue_id can point at a still-raw cluster, e.g.
    # marc-031 -> proj-038, invisible to that view but a completely real,
    # already-established project association). work_objects.parent_id
    # is the ground truth for both a promoted issue and a raw cluster.
    with _lock:
        conn = _connect()
        try:
            wo = conn.execute(
                "SELECT parent_id FROM work_objects WHERE id = ?", (row["issue_id"],)
            ).fetchone()
        finally:
            conn.close()
    return wo["parent_id"] if wo else None


# --- issues ---------------------------------------------------------------

def create_issue(
    *,
    id: str,
    title: str,
    category: Optional[str] = None,
    state: str = "active",
    priority: str = "med",
    owner: str = "marc",
    due: Optional[str] = None,
    confidence_tier: Optional[str] = None,
) -> None:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO issues
                   (id, title, category, state, priority, owner, due, opened_at, updated_at, confidence_tier)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (id, title, category, state, priority, owner, due, now, now, confidence_tier),
            )
            conn.execute(
                "INSERT INTO issue_state_history (issue_id, from_state, to_state, changed_ts) VALUES (?, NULL, ?, ?)",
                (id, state, now),
            )
        finally:
            conn.close()


def create_cluster(*, id: str, title: str, category: Optional[str] = None) -> None:
    """Corrected-ordering redesign (2026-08-05): a cluster is the raw pass-1/
    pass-2 matching unit - the thing `workgraph_classify.cluster_and_link()`
    creates for a fresh, unlinked communication BEFORE any real project/
    issue judgment has happened. Writes directly to work_objects (bypassing
    the `issues` view/its INSTEAD OF triggers entirely) with is_raw_cluster=1,
    so it never appears through get_issue/list_issues/the Inbox/NBA/
    checklist - see the `issues` view's own definition for why.

    No issue_state_history row - that table tracks a real issue's tracked
    lifecycle (active/waiting/done), which isn't a meaningful concept for a
    raw cluster that hasn't been judged as anything yet. `state` still gets
    a real value ('active') purely because work_objects.status is NOT NULL,
    not because a cluster has a state lifecycle worth tracking."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO work_objects
                   (id, object_type, title, category, status, priority, owner,
                    opened_at, updated_at, is_raw_cluster)
                   VALUES (?, 'request', ?, ?, 'active', 'med', 'marc', ?, ?, 1)""",
                (id, title, category, now, now),
            )
        finally:
            conn.close()


def create_cluster_with_new_id(**kwargs: Any) -> str:
    """next_issue_id() + create_cluster(), safe against the same allocation
    race create_issue_with_new_id already guards against - clusters share
    the same id namespace as issues/projects (all work_objects), so the
    same collision-retry discipline applies."""
    for attempt in range(25):
        cluster_id = next_issue_id()
        try:
            create_cluster(id=cluster_id, **kwargs)
            return cluster_id
        except sqlite3.IntegrityError:
            time.sleep(random.uniform(0, 0.01) * (attempt + 1))
            continue
    raise RuntimeError("could not allocate a unique cluster id after 25 attempts")


def get_issue_or_cluster(id: str) -> Optional[dict]:
    """Corrected-ordering redesign (2026-08-05): reads an object_type=
    'request' work_object regardless of is_raw_cluster - for callers that
    genuinely don't care which kind they got (e.g. `cluster_and_link()`'s
    Jasper-ref-tag check: the tag names an exact id directly, and that id
    is equally valid whether it turned out to already be promoted to a
    real issue or is still an unpromoted cluster). Prefer get_issue/
    get_cluster instead when a caller DOES care which kind it's reading -
    this is deliberately the one place both are treated as interchangeable."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT id, title, category, status AS state, priority, priority_score,
                          nba_action_kind, nba_reason, owner, due, opened_at, updated_at,
                          confidence_tier, parent_id AS project_id, lesson_id_cited,
                          has_unmet_prerequisite, claims_revision
                   FROM work_objects WHERE id = ? AND object_type = 'request'""",
                (id,),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def get_cluster(id: str) -> Optional[dict]:
    """Read counterpart to create_cluster - bypasses the `issues` view (which
    now excludes is_raw_cluster=1 rows on purpose) and reads work_objects
    directly, returning the SAME column shape the `issues` view exposes
    (state/project_id aliases included) so this is a drop-in substitute for
    get_issue anywhere pass-1/pass-2 matching needs to read a cluster - e.g.
    _matched_data_points/scored_grouping_decision take a plain dict shaped
    like an issue and don't care whether it came from the view or here."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT id, title, category, status AS state, priority, priority_score,
                          nba_action_kind, nba_reason, owner, due, opened_at, updated_at,
                          confidence_tier, parent_id AS project_id, lesson_id_cited,
                          has_unmet_prerequisite, claims_revision
                   FROM work_objects WHERE id = ? AND is_raw_cluster = 1""",
                (id,),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_clusters(limit: int = 10000) -> list[dict]:
    """Cluster counterpart to list_issues - the candidate pool for pass-2
    matching (`workgraph_projects._candidate_pool`). Deliberately no
    `states` filter parameter the way list_issues has one - a cluster has
    no meaningful open/closed lifecycle of its own (see create_cluster's
    own docstring); every cluster not yet promoted into a project is
    equally in scope for matching."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT id, title, category, status AS state, priority, priority_score,
                          nba_action_kind, nba_reason, owner, due, opened_at, updated_at,
                          confidence_tier, parent_id AS project_id, lesson_id_cited,
                          has_unmet_prerequisite, claims_revision
                   FROM work_objects WHERE object_type = 'request' AND is_raw_cluster = 1
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def update_issue(id: str, *, touch_updated_at: bool = True, actor: Optional[str] = None, **fields: Any) -> None:
    """Generic field updater for issues (state, priority, nba_*, due, etc.).
    A state change is logged to issue_state_history in the same connection so
    the two writes can never drift (no separate txn to lose).

    touch_updated_at=False is for writers whose fields aren't "activity" -
    today only workgraph_nba.recompute_all()'s periodic NBA rescoring, which
    otherwise erased the very staleness signal it's supposed to measure:
    every tick would bump updated_at, so an issue that had gone quiet for
    10 days looked freshly touched again after the next recompute pass.

    actor - who/what caused a state change (e.g. "marc" for a manual click,
    "recompute_issue_state" for the automated rule). Added after a real
    incident (2026-08-01): two financial issues were flipped to 'done' with
    zero closing evidence and there was no way to tell whether the automated
    rule or a manual/bulk endpoint click did it - see issue_state_history's
    own ALTER TABLE comment. None is honest for callers that don't pass one
    yet, not a guess at who did it."""
    if not fields:
        return
    if touch_updated_at:
        fields["updated_at"] = time.time()
    changed_ts = fields.get("updated_at", time.time())
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [id]
    with _lock:
        conn = _connect()
        try:
            new_state = fields.get("state")
            if new_state is not None:
                row = conn.execute("SELECT state FROM issues WHERE id = ?", (id,)).fetchone()
                old_state = row["state"] if row else None
                if old_state != new_state:
                    conn.execute(
                        "INSERT INTO issue_state_history (issue_id, from_state, to_state, changed_ts, actor) VALUES (?, ?, ?, ?, ?)",
                        (id, old_state, new_state, changed_ts, actor),
                    )
            conn.execute(f"UPDATE issues SET {set_clause} WHERE id = ?", values)
        finally:
            conn.close()


def list_issue_state_history(issue_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM issue_state_history WHERE issue_id = ? ORDER BY changed_ts ASC", (issue_id,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def list_issue_state_history_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched form of list_issue_state_history - one query for N issues instead
    of N queries (fixed 2026-07-29: workgraph_alerts.refresh_alerts was calling
    the single-issue form inside a loop over up to 1000 issues on every
    periodic tick). Returns {issue_id: [rows...]}, missing ids simply absent
    (empty list), same as calling the single form on an issue with no history."""
    if not issue_ids:
        return {}
    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join("?" * len(issue_ids))
            rows = conn.execute(
                f"SELECT * FROM issue_state_history WHERE issue_id IN ({placeholders}) ORDER BY changed_ts ASC",
                issue_ids,
            ).fetchall()
        finally:
            conn.close()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["issue_id"], []).append(dict(r))
    return out


# --- membership_state / exposure_state (design doc Section 12.8) ----------
# Bypass the issues/projects views entirely (same reasoning as add_evidence,
# Section 12.2) - these two columns live on work_objects directly and don't
# need to be exposed through either view's existing column list, since no
# current caller reads/writes them through the issue/project JSON payload.

_EXPOSURE_STATE_RANK = {"not_exposed": 0, "shown_in_project": 1, "used_in_summary": 2, "used_for_action": 3}


def get_work_object_membership_exposure(work_object_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT membership_state, exposure_state FROM work_objects WHERE id = ?", (work_object_id,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def confirm_work_object_membership(work_object_id: str) -> None:
    """The real, single human-confirm event (workgraph_projects.
    confirm_suggestion) - membership_state only ever moves provisional ->
    confirmed, never the reverse (a confirmed grouping being un-confirmed
    isn't a real case this session found a producer for)."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE work_objects SET membership_state = 'confirmed' WHERE id = ?", (work_object_id,)
            )
        finally:
            conn.close()


def reset_work_object_membership_to_provisional(work_object_id: str) -> None:
    """Task #178's real producer for the reverse transition confirm_work_
    object_membership's own docstring said didn't exist yet: workgraph_
    projects.split_issue_from_project calls this on the issue being split
    off. Needed because membership_state is a column on the work_object's
    OWN row, not reset by assign_issue_to_project alone - without this, an
    issue split out of a wrongly-confirmed project and later auto-grouped
    into some OTHER, genuinely-unrelated project would keep showing
    'confirmed' from the OLD grouping, even though nobody has looked at
    the new one."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE work_objects SET membership_state = 'provisional' WHERE id = ?", (work_object_id,)
            )
        finally:
            conn.close()


def grandfather_existing_grouping_as_confirmed() -> dict:
    """Corrected pipeline Phase F (2026-08-05) - one-time migration of the
    live pre-existing corpus, same 'cold start = migration' principle as
    the identity_anchors/claims backfills earlier this session. Every real
    issue (object_type='request', is_raw_cluster=0 - a cluster is never a
    candidate here, it has no meaningful confirmed/provisional state of
    its own until it's actually promoted) that predates the provisional/
    confirmed distinction (v2.8, task #121) sits at membership_state's own
    schema DEFAULT 'provisional' whether or not a human ever actually
    reviewed its grouping - not because anyone judged it uncertain, but
    because the column didn't exist yet when most of them were created.

    No reinterpretation: this does NOT re-run any matching/grouping logic,
    does not touch project_id/parent_id, and does not re-derive anything
    about an issue's own tasks/evidence/NBA score - it only flips
    membership_state so ws.project_has_confirmed_grouping (Phase D's own
    trigger for curator's real-issue extraction) correctly recognizes an
    already-established project as eligible, instead of it looking exactly
    like a fresh, unreviewed guess forever.

    Idempotent - only ever touches a row still at 'provisional' (the
    schema default), so a second run against an already-migrated corpus is
    a real no-op, not just a harmless one. Returns {"confirmed": N} - the
    real count of rows this run actually changed."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """UPDATE work_objects SET membership_state = 'confirmed'
                   WHERE object_type = 'request' AND is_raw_cluster = 0 AND membership_state = 'provisional'"""
            )
            confirmed = cur.rowcount
        finally:
            conn.close()
    return {"confirmed": confirmed}


def advance_work_object_exposure_state(work_object_id: str, new_state: str) -> None:
    """Forward-only (design doc Section 12.8's own rule: 'once exposed,
    never silently moved again') - ranked not_exposed < shown_in_project <
    used_in_summary < used_for_action, a single atomic UPDATE that only
    takes effect if new_state actually outranks whatever's there now, so
    a later call with an EARLIER-ranked state (e.g. a project detail view
    after the issue was already used_for_action) is a safe no-op rather
    than a regression. Called from every real render/dispatch point that
    exposes a work_object to Marc: the Project Detail route
    (shown_in_project), upsert_synthesis (used_in_summary),
    api_cockpit_action (used_for_action)."""
    new_rank = _EXPOSURE_STATE_RANK[new_state]
    with _lock:
        conn = _connect()
        try:
            case_expr = " ".join(f"WHEN '{state}' THEN {rank}" for state, rank in _EXPOSURE_STATE_RANK.items())
            conn.execute(
                f"UPDATE work_objects SET exposure_state = ? "
                f"WHERE id = ? AND (CASE exposure_state {case_expr} END) < ?",
                (new_state, work_object_id, new_rank),
            )
        finally:
            conn.close()


def get_issue(id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def get_issues_by_ids(ids: list[str]) -> dict[str, dict]:
    """Batched form of get_issue - one query for a whole list of ids instead
    of one query per id. Fixed 2026-07-30 (hardening pass #3): workgraph_
    suppliers.list_suppliers() was calling get_issue() once per issue across
    every company (375 individual connections measured live, ~3-4.5s per
    call, freezing the single-worker server for that whole span). Same
    batched-query fix already applied to raw_items/extractions/state_history
    this session. Missing ids are simply absent from the result, same as
    get_issue returning None for them."""
    if not ids:
        return {}
    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT * FROM issues WHERE id IN ({placeholders})", ids).fetchall()
        finally:
            conn.close()
    return {r["id"]: dict(r) for r in rows}


def list_issue_ids() -> list[str]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT id FROM issues").fetchall()
        finally:
            conn.close()
    return [r["id"] for r in rows]


def list_issue_ids_missing_derived_title() -> list[str]:
    """Task #52 (2026-08-04): every issue with no real derived_title yet -
    either no synthesis row at all, or one whose derived_title is NULL/
    empty. Batched, one query - the real scope workgraph_classify.
    backfill_derived_titles needs to stay cheap enough to run every
    scheduled_refresh cycle (not just as a one-time manual pass) without
    redoing work for every already-titled issue every time."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("""
                SELECT i.id FROM issues i
                LEFT JOIN synthesis s ON s.entity_type = 'issue' AND s.entity_id = i.id
                WHERE s.entity_id IS NULL OR s.derived_title IS NULL OR s.derived_title = ''
            """).fetchall()
        finally:
            conn.close()
    return [r["id"] for r in rows]


def list_project_ids_missing_derived_title() -> list[str]:
    """Project counterpart to list_issue_ids_missing_derived_title above
    (task #167/#168, 2026-08-04) - live-DB check found 0 of 52 projects had
    ever gotten a derived_title, so every project's name stayed whatever
    workgraph_projects._project_name_for picked at creation, forever."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("""
                SELECT p.id FROM projects p
                LEFT JOIN synthesis s ON s.entity_type = 'project' AND s.entity_id = p.id
                WHERE s.entity_id IS NULL OR s.derived_title IS NULL OR s.derived_title = ''
            """).fetchall()
        finally:
            conn.close()
    return [r["id"] for r in rows]


def get_last_refresh_ts() -> Optional[float]:
    """Best-effort 'how fresh is what's on screen' - the most recent
    issues.updated_at across the whole graph. Simpler and more reliably
    populated than parsing the scheduled-refresh log (which is empty until
    a scheduled run actually completes), and reflects real data changes
    from any source - a scheduled run, a manual /api/cockpit/refresh, or a
    worker action - not just the scheduled cadence specifically."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT MAX(updated_at) m FROM issues").fetchone()
        finally:
            conn.close()
    return row["m"] if row else None


def list_standalone_issue_ids() -> list[str]:
    """Issue ids with no project_id yet - the "standalone Issue" half of the
    synthesis staleness scan (workgraph_synthesis.list_stale_entities); a
    Project's constituent issues are covered separately via
    list_issues_for_project."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT id FROM issues WHERE project_id IS NULL").fetchall()
        finally:
            conn.close()
    return [r["id"] for r in rows]


def list_issues(states: Optional[list[str]] = None, limit: int = 200) -> list[dict]:
    """`display_title` prefers Colleen's synthesized title (real judgment - "UneeQ
    pricing negotiation") over the raw `title` (mechanical, subject-line-derived) -
    the list view should show the good one without every caller re-deriving it.
    Also carries `preview` (synthesis summary, for an inbox-style 1-2 sentence
    description per row) and `external_companies` (comma-joined external party
    companies) so the list view can render a real inbox row without an N+1
    fetch per issue - both computed here via JOIN/subquery, not per-row calls."""
    sql = """SELECT issues.*, synthesis.derived_title AS synth_derived_title,
                    synthesis.summary AS synth_summary,
                    (SELECT GROUP_CONCAT(DISTINCT p.company) FROM issue_parties ip
                       JOIN parties p ON p.id = ip.party_id
                       WHERE ip.issue_id = issues.id AND p.affiliation = 'external' AND p.company IS NOT NULL
                    ) AS external_companies
             FROM issues
             LEFT JOIN synthesis ON synthesis.entity_type = 'issue' AND synthesis.entity_id = issues.id"""
    args: list[Any] = []
    if states:
        placeholders = ", ".join("?" for _ in states)
        sql += f" WHERE issues.state IN ({placeholders})"
        args.extend(states)
    # issues.id ASC as a final tie-break (fixed 2026-07-29): priority_score is a
    # rounded float and updated_at a wall-clock second - either can tie exactly
    # (two brand-new issues with identical inputs, or a batch backfill landing
    # several in the same second), and SQLite doesn't guarantee stable order
    # for tied sort keys. Without a fully-unique final column, the Morning
    # Queue's row order for tied issues could silently shuffle between
    # otherwise-identical runs (e.g. after a VACUUM or an index change).
    sql += " ORDER BY issues.priority_score DESC NULLS LAST, issues.updated_at DESC, issues.id ASC LIMIT ?"
    args.append(limit)
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["display_title"] = d.get("synth_derived_title") or d["title"]
        d["preview"] = d.get("synth_summary")
        out.append(d)
    return out


def list_issues_with_unmet_prerequisite() -> list[dict]:
    """Open issues where workgraph_nba.recompute_all() last found an active
    Aristotle warning (task #55) - cheap enough for workgraph_alerts.py's
    scan to call every tick, since it's a plain indexed-ish column read, not
    a recomputation of check_prerequisites() itself."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT * FROM issues WHERE has_unmet_prerequisite = 1
                   AND state NOT IN ('done','noise-archived','dismissed')"""
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def checklist_item_key(kind: str, raw_item_id: Any, text: str) -> str:
    """Deterministic fingerprint for a checklist row that has no real id of
    its own (see the checklist_dismissals table comment). Same (kind,
    raw_item_id, text) always produces the same key; a reworded/reordered
    re-extraction produces a different one (disclosed limitation, not a bug)."""
    norm_text = (text or "").strip().lower()
    digest = hashlib.sha1(norm_text.encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{raw_item_id}:{digest}"


def _set_checklist_item_status(*, issue_id: str, kind: str, raw_item_id: Any, text: str,
                                status: str, actor: Optional[str] = None) -> str:
    """Shared writer behind dismiss_checklist_item()/mark_checklist_item_done() -
    identical persistence, only the recorded status differs. Idempotent
    (re-recording the same item_key just refreshes status/dismissed_ts/actor,
    via INSERT OR REPLACE)."""
    item_key = checklist_item_key(kind, raw_item_id, text)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO checklist_dismissals
                   (issue_id, item_key, kind, text_snippet, dismissed_ts, actor, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (issue_id, item_key, kind, (text or "")[:280], time.time(), actor, status),
            )
        finally:
            conn.close()
    return item_key


def dismiss_checklist_item(*, issue_id: str, kind: str, raw_item_id: Any, text: str,
                            actor: Optional[str] = None) -> str:
    """Persist a checklist-item-level dismissal - "this was wrong/not needed,"
    never a completion (task #44)."""
    return _set_checklist_item_status(issue_id=issue_id, kind=kind, raw_item_id=raw_item_id,
                                       text=text, status="dismissed", actor=actor)


def mark_checklist_item_done(*, issue_id: str, kind: str, raw_item_id: Any, text: str,
                              actor: Optional[str] = None) -> str:
    """Persist a checklist-item-level completion - distinct outcome from
    dismiss, same mechanics (task #59)."""
    return _set_checklist_item_status(issue_id=issue_id, kind=kind, raw_item_id=raw_item_id,
                                       text=text, status="done", actor=actor)


def list_dismissed_checklist_keys(issue_id: str) -> set[str]:
    """All item_keys with ANY recorded status (dismissed OR done) for this
    issue - checked against a fresh checklist_item_key() computed for each
    row at read time, so a resolved item stops reappearing without the
    caller needing to store anything beyond the set membership check. Name
    kept from task #44 (dismiss-only at the time); scope widened task #59."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT item_key FROM checklist_dismissals WHERE issue_id = ?", (issue_id,)
            ).fetchall()
        finally:
            conn.close()
    return {r["item_key"] for r in rows}


def next_task_id(issue_id: str) -> str:
    """Per-issue counter, e.g. 'marc-014-t1' - avoids a global counter and
    keeps task ids readably scoped to their parent issue. Derived from MAX
    existing suffix per issue, not COUNT(*), for the same deletion-safety
    reason as next_issue_id()."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id FROM work_tasks WHERE issue_id = ?", (issue_id,)
            ).fetchall()
        finally:
            conn.close()
    max_n = 0
    prefix = f"{issue_id}-t"
    for r in rows:
        if r["id"].startswith(prefix):
            try:
                max_n = max(max_n, int(r["id"][len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{max_n + 1}"


def create_task(
    *, issue_id: str, label: str, state: str = "open",
    depends_on: Optional[list[str]] = None, due: Optional[str] = None,
    action: Optional[str] = None, owner: Optional[str] = None,
) -> str:
    """Confirmed race, 2026-07-29: next_task_id() computes-and-releases the
    lock, then the INSERT re-acquires it separately - a writer landing in
    that window gets the same id and the INSERT raises IntegrityError.
    Reproduced with 20 concurrent callers (1 succeeded, 19 raised). Retrying
    on that specific error with a freshly regenerated id closes the same-
    process race exactly the way insert_raw_item already handles its own
    dedupe collision, and is a real (if partial) backstop for a cross-process
    race too, since _lock only ever protected same-process callers anyway."""
    now = time.time()
    for attempt in range(25):
        task_id = next_task_id(issue_id)
        with _lock:
            conn = _connect()
            try:
                conn.execute(
                    """INSERT INTO work_tasks (id, issue_id, label, state, depends_on, due, action, owner, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task_id, issue_id, label, state, json.dumps(depends_on or []), due, action, owner, now, now),
                )
                return task_id
            except sqlite3.IntegrityError:
                pass  # fall through to the jittered retry below - `continue` here would skip it
            finally:
                conn.close()
        time.sleep(random.uniform(0, 0.01) * (attempt + 1))
    raise RuntimeError(f"could not allocate a unique task id for issue {issue_id!r} after 25 attempts")


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "depends_on" in fields:
        fields["depends_on"] = json.dumps(fields["depends_on"])
    fields["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [task_id]
    with _lock:
        conn = _connect()
        try:
            conn.execute(f"UPDATE work_tasks SET {set_clause} WHERE id = ?", values)
        finally:
            conn.close()


def correct_task_owner(task_id: str, *, owner: Optional[str], reason: Optional[str] = None) -> None:
    """A human correction (typically relayed by a worker after Marc says
    'that one's not mine') - logs the change to audit_log. Generalizing this
    into a standing ownership_rule is a SEPARATE, explicit call
    (create_ownership_rule) - correcting one task never silently rewrites
    the rule set on its own."""
    task = get_task(task_id)
    if task is None:
        return
    old_owner = task.get("owner")
    update_task(task_id, owner=owner)
    add_audit_entry(entity_type="task_owner", entity_id=task_id, field="owner",
                     old_value=old_owner, new_value=owner, reason=reason)


def list_tasks(issue_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM work_tasks WHERE issue_id = ? ORDER BY created_at ASC", (issue_id,)
            ).fetchall()
        finally:
            conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["depends_on"] = json.loads(d["depends_on"])
        except Exception:
            d["depends_on"] = []
        out.append(d)
    return out


def get_task(task_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM work_tasks WHERE id = ?", (task_id,)).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    d = dict(row)
    try:
        d["depends_on"] = json.loads(d["depends_on"])
    except Exception:
        d["depends_on"] = []
    return d


def next_issue_id() -> str:
    """Sequential id. The reference platform namespaces issue ids as
    '<username>/<NNN>' for cross-user uniqueness in a multi-user system; this
    is single-user (Marc only), so that rationale doesn't apply, and a literal
    '/' collides with URL path routing (FastAPI splits path segments on it) —
    hyphenated instead, not slashed.

    Derived from the MAX existing numeric suffix, not COUNT(*) — a count-based
    scheme collides the instant any issue is ever deleted (count drops, but
    the higher-numbered id already in use doesn't), which is exactly what
    happened during a real cleanup this session (IntegrityError on
    'UNIQUE constraint failed: issues.id'). Max-based is stable under deletion.

    Queries work_objects directly, not the `issues` view - the real
    uniqueness constraint is work_objects.id's PRIMARY KEY, which a cluster
    (is_raw_cluster=1, invisible through the `issues` view on purpose)
    still occupies. Scanning only the view would let this recompute the
    SAME already-taken 'marc-NNN' id forever once a cluster holds the
    current max - confirmed live (2026-08-05): create_issue_with_new_id
    exhausted all 25 collision-retries this way the moment one cluster
    existed, since every retry recomputed the identical, still-view-
    invisible max."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT id FROM work_objects WHERE id LIKE 'marc-%'").fetchall()
        finally:
            conn.close()
    max_n = 0
    for r in rows:
        try:
            max_n = max(max_n, int(r["id"].split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"marc-{max_n + 1:03d}"


def create_issue_with_new_id(**kwargs: Any) -> str:
    """next_issue_id() + create_issue(), but safe against the race between
    them. Confirmed exploitable 2026-07-29: next_issue_id() computes-and-
    releases the lock, then create_issue()'s INSERT re-acquires it separately
    - a writer landing in that window computes the same id and its INSERT
    raises IntegrityError. Reproduced with 20 concurrent callers (1
    succeeded, 19 raised). Retrying with a freshly regenerated id on that
    specific error closes the same-process race the same way
    insert_raw_item already handles its own dedupe collision. Accepts the
    same keyword arguments as create_issue() (everything except `id`, which
    this generates). Returns the actually-used id."""
    for attempt in range(25):
        issue_id = next_issue_id()
        try:
            create_issue(id=issue_id, **kwargs)
            return issue_id
        except sqlite3.IntegrityError:
            time.sleep(random.uniform(0, 0.01) * (attempt + 1))  # jitter - avoid a thundering-herd re-collision
            continue
    raise RuntimeError("could not allocate a unique issue id after 25 attempts")


# --- evidence ---------------------------------------------------------------

def add_evidence(*, issue_id: str, type: str, summary: str, raw_item_id: Optional[int] = None) -> int:
    """Inserts into evidence_units/evidence_unit_links directly rather than
    through the `evidence` view (Section 12.2) - confirmed empirically that
    SQLite reverts last_insert_rowid() to its pre-trigger value once an
    INSTEAD OF trigger finishes, so a caller that needs the new row's real
    id back (this one does - it's the contract every existing caller of
    this function already relies on) can't get it through the view's
    INSTEAD OF INSERT trigger. Same external contract as before: always
    creates a new row, returns its real id."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO evidence_units (raw_item_id, type, summary, ts) VALUES (?, ?, ?, ?)",
                (raw_item_id, type, summary, time.time()),
            )
            eu_id = cur.lastrowid
            conn.execute(
                "INSERT INTO evidence_unit_links (evidence_unit_id, work_object_id) VALUES (?, ?)",
                (eu_id, issue_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    invalidate_work_object_signature(issue_id)
    return eu_id


def get_raw_items_for_issue(issue_id: str) -> list[dict]:
    """All raw_items linked to this issue, oldest first - used to re-derive
    issue state from the FULL thread rather than whichever item happened to
    arrive first (see workgraph_classify.recompute_issue_state)."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM raw_items WHERE issue_id = ? ORDER BY occurred_ts ASC", (issue_id,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def list_raw_items_since(cutoff_ts: float) -> list[dict]:
    """Every raw_item (linked or not) occurring at/after cutoff_ts -
    workgraph_discovery.py's setup/monthly-sweep window reader. No index
    on occurred_ts - a real, deliberate call, not an oversight: at this
    corpus's actual scale (thousands, not millions, of rows) a full-table
    scan filtered by a single comparison is sub-millisecond in SQLite: the
    index would be premature for a table that doesn't need it yet, and
    every OTHER range-style read in this module (e.g. get_raw_items_for_
    issue above) already accepts the same tradeoff."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM raw_items WHERE occurred_ts >= ? ORDER BY occurred_ts ASC", (cutoff_ts,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def compute_reply_latency_for_issue(issue_id: str) -> dict:
    """Enhancement idea panel #1: a real back-and-forth activity signal
    from raw_items.direction/occurred_ts - both already captured at
    ingest, never read back out for this until now. ping_pong_count is
    how many times direction actually alternated (inbound->outbound or
    outbound->inbound) across the thread; avg_reply_latency_seconds is the
    mean gap between each such alternation (None if there were none - a
    one-sided thread, or too few directional items to alternate at all).
    'internal'/'unknown' direction values are excluded before looking for
    alternations - neither is a real side of a back-and-forth, and
    leaving them in would either falsely break a real streak or falsely
    count a same-side repeat as an alternation."""
    items = get_raw_items_for_issue(issue_id)
    directional = [i for i in items if i.get("direction") in ("inbound", "outbound")]
    ping_pong_count = 0
    latencies = []
    for prev, cur in zip(directional, directional[1:]):
        if prev["direction"] != cur["direction"]:
            ping_pong_count += 1
            latencies.append(cur["occurred_ts"] - prev["occurred_ts"])
    return {
        "ping_pong_count": ping_pong_count,
        "avg_reply_latency_seconds": (sum(latencies) / len(latencies)) if latencies else None,
    }


def list_calendar_meetings_for_issue(issue_id: str) -> list[dict]:
    """Enhancement idea panel #7: the calendar-source raw_items already
    linked to this issue, with their meta_json (location/isCancelled/
    webLink/showAs/importance/is_recurring, plus attendees_detailed/
    full_agenda_text when the lookahead-window enrichment ran - see
    ingest/normalize.py's _process_calendar) parsed back out. Oldest first,
    same convention as get_raw_items_for_issue - most issues will have at
    most one or two calendar raw_items, this isn't a hot path."""
    out = []
    for item in get_raw_items_for_issue(issue_id):
        if item.get("source") != "calendar":
            continue
        meta = json.loads(item["meta_json"]) if item.get("meta_json") else {}
        out.append({
            "raw_item_id": item["id"],
            "subject": item.get("subject"),
            "occurred_ts": item.get("occurred_ts"),
            "organizer": item.get("from_actor"),
            "is_organizer": item.get("is_organizer"),
            "participants": json.loads(item["participants"]) if item.get("participants") else [],
            **meta,
        })
    return out


def list_raw_items_by_thread_key(source: str, thread_key: str) -> list[dict]:
    """All raw_items sharing this (source, thread_key), oldest first,
    REGARDLESS of which issue each one currently belongs to (unlike get_
    raw_items_for_issue) - what workgraph_sessionize.py needs to see a
    container's full real history when sessionizing it, since today's
    flat thread_key-per-container model may already have split some of
    that history across more than one issue."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM raw_items WHERE source = ? AND thread_key = ? ORDER BY occurred_ts ASC",
                (source, thread_key),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_calendar_raw_items_for_remediation() -> list[dict]:
    """Every real calendar raw_item's subject/organizer/occurred_ts/
    issue_id - for remediate_calendar_series.py (step 7, meeting-grouping
    design pass) to re-derive what each occurrence's series identity
    SHOULD be under the step-3 fix, and find already-created Issues that
    are still fragmented across separate ids for the same real series."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT subject, from_actor, occurred_ts, issue_id FROM raw_items WHERE source = 'calendar'"
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def remediate_merge_issue_identity(winner_id: str, loser_id: str, *, reason_label: str) -> None:
    """One-off ISSUE-identity consolidation for remediate_calendar_series.py
    (step 7) - NOT the same operation as merge_issues_txn (which merges two
    issues into the same PROJECT, leaving both issue rows intact). This
    reassigns every real FK reference from the loser issue to the winner
    and archives the loser issue itself, because the loser was never a
    genuinely distinct Issue in the first place - it's a fragment of one
    real recurring series that should have shared one Issue identity from
    the start (see remediate_calendar_series.py's own module docstring).

    issue_parties is repointed row-by-row with an existing-link check first
    - its PK is (issue_id, party_id), and the SAME party can already be
    linked to both winner and loser (e.g. the same organizer on every
    occurrence). synthesis rows are deliberately left alone (orphaned but
    harmless, never hard-deleted - matches this codebase's own never-
    silently-drop convention).

    One all-or-nothing transaction, same BEGIN IMMEDIATE/COMMIT/ROLLBACK +
    bounded-retry pattern as merge_issues_txn - see that function's own
    docstring for why this repo uses that pattern instead of the module's
    public autocommit helpers for a multi-step write."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            for attempt in range(5):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError:
                    if attempt == 4:
                        raise
                    time.sleep(random.uniform(0, 0.02) * (attempt + 1))
            try:
                for table in ("raw_items", "evidence", "work_tasks", "issue_state_history",
                              "nba_choice_log"):
                    conn.execute(f"UPDATE {table} SET issue_id = ? WHERE issue_id = ?", (winner_id, loser_id))

                loser_parties = conn.execute(
                    "SELECT party_id, role FROM issue_parties WHERE issue_id = ?", (loser_id,)
                ).fetchall()
                for p in loser_parties:
                    existing = conn.execute(
                        "SELECT 1 FROM issue_parties WHERE issue_id = ? AND party_id = ?", (winner_id, p["party_id"])
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            "INSERT INTO issue_parties (issue_id, party_id, role) VALUES (?, ?, ?)",
                            (winner_id, p["party_id"], p["role"]),
                        )
                conn.execute("DELETE FROM issue_parties WHERE issue_id = ?", (loser_id,))

                conn.execute("UPDATE issues SET state = ?, updated_at = ? WHERE id = ?",
                             ("noise-archived", now, loser_id))
                conn.execute(
                    """INSERT INTO audit_log (entity_type, entity_id, field, old_value, new_value, changed_ts, reason)
                       VALUES ('issue', ?, 'issue_identity_merged_into', ?, ?, ?, ?)""",
                    (loser_id, loser_id, winner_id, now, reason_label),
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
        finally:
            conn.close()


def get_raw_items_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched form of get_raw_items_for_issue - one query for N issues
    instead of N queries. Fixed 2026-07-30 (hardening pass #3): workgraph_
    nba.value_amount_for_issue() was called once per open issue inside
    workgraph_suppliers.list_suppliers()'s per-company loop, the dominant
    contributor to that endpoint's measured 3-4.5s single-worker freeze.
    Missing ids are simply absent (empty list), same as the single form."""
    if not issue_ids:
        return {}
    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join("?" * len(issue_ids))
            rows = conn.execute(
                f"SELECT * FROM raw_items WHERE issue_id IN ({placeholders}) ORDER BY occurred_ts ASC",
                issue_ids,
            ).fetchall()
        finally:
            conn.close()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["issue_id"], []).append(dict(r))
    return out


def list_open_issue_ids_for_reference(pr_number_base: str) -> list[str]:
    """Every currently-open issue with at least one raw_item carrying this
    version-stripped reference identity (PR/PO base, e.g. "PR416079" - see
    workgraph_signals.reference_base). Grouping/NBA redesign Part A1/C: a
    real, structured identifier is a positive match signal on its own, not
    just a veto (see workgraph_projects._vetoed_by_reference_mismatch for
    the existing negative-only use of this same field). Ordered by
    updated_at DESC so a caller wanting "the" single best match (Part C)
    can just take the first result - a deterministic, stable choice, not
    an unordered pick.

    Matches on pr_number_base, not the full versioned pr_number (2026-07-31
    fix) - "PR416079-V32" and "PR416079-V33" are the SAME real requisition,
    and matching on the full string treated them as unrelated (or, worse,
    actively contradicting - see _vetoed_by_reference_mismatch)."""
    if not pr_number_base:
        return []
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT i.id FROM issues i
                   JOIN raw_items r ON r.issue_id = i.id
                   WHERE r.pr_number_base = ? AND i.state IN ('active','waiting','blocked')
                   GROUP BY i.id
                   ORDER BY i.updated_at DESC""",
                (pr_number_base,),
            ).fetchall()
        finally:
            conn.close()
    return [r["id"] for r in rows]


def list_open_work_objects_for_reference(pr_number_base: str) -> list[str]:
    """Corrected-ordering redesign (2026-08-05): cluster-aware sibling of
    list_open_issue_ids_for_reference, for workgraph_classify.cluster_and_
    link()'s own exact-reference auto-attach - a fresh unlinked item
    sharing a PR/PO with an EXISTING CLUSTER (not yet promoted to a real
    issue) needs to find that cluster too, not just already-promoted
    issues. Deliberately a SEPARATE function rather than making list_open_
    issue_ids_for_reference itself cluster-aware - that function's other
    two callers (workgraph_projects._shared_reference_id and its sibling)
    are pass-2 matching helpers that today only ever run against real
    issue ids (Phase C, promoting a cluster group into a project, isn't
    built yet) - widening its scope now would be a real, untested behavior
    change to code this redesign hasn't touched yet, for no benefit this
    function doesn't already cover for cluster_and_link's actual need.

    Queries work_objects directly (not the `issues` view) so it sees both
    real issues (is_raw_cluster=0) and not-yet-promoted clusters
    (is_raw_cluster=1) - status filter unchanged from the issue-only
    version (only 'active'/'waiting'/'blocked' are in scope, same
    'currently open' meaning for either kind of row)."""
    if not pr_number_base:
        return []
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT w.id FROM work_objects w
                   JOIN raw_items r ON r.issue_id = w.id
                   WHERE r.pr_number_base = ? AND w.object_type = 'request'
                         AND w.status IN ('active','waiting','blocked')
                   GROUP BY w.id
                   ORDER BY w.updated_at DESC""",
                (pr_number_base,),
            ).fetchall()
        finally:
            conn.close()
    return [r["id"] for r in rows]


def list_issues_for_reference_any_state(pr_number_base: str) -> list[dict]:
    """Same idx_raw_pr_number_base-indexed lookup as list_open_issue_ids_
    for_reference above, but across every state, not just active/waiting/
    blocked - needed by workgraph_projects.find_reference_id_collisions_
    for_issue (enhancement idea #2), which deliberately wants to surface a
    collision with a done/dismissed issue too (a merge that never
    happened is still worth seeing, whatever state either side ended up
    in). Real perf fix, 2026-08-03: that function used to fall back to a
    full ws.list_issues(states=None, limit=10000) Python-side scan plus a
    signature lookup per candidate - profiled live at ~1.5s per call, and
    cockpit.html's pccLoadIssues() calls the issue-detail route (which
    calls this) once per issue on every load, so a 345-issue board turned
    one page load into ~345 x 1.5s of largely serialized work. Returns
    {issue_id, title, project_id} per row - just enough for the caller to
    skip same-project pairs and display a title without a second lookup."""
    if not pr_number_base:
        return []
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT DISTINCT i.id AS issue_id, i.title AS title, i.project_id AS project_id
                   FROM issues i
                   JOIN raw_items r ON r.issue_id = i.id
                   WHERE r.pr_number_base = ?""",
                (pr_number_base,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def list_all_reference_base_id_pairs() -> list[dict]:
    """Enhancement idea panel #14 (Reference-ID cross-check worker
    capability): every distinct (pr_number_base, issue) pairing across the
    WHOLE board, in one query - the DB-wide sweep workgraph_alerts.run()
    needs to proactively flag a same-PR/PO collision, since Marc would
    otherwise only ever see one via find_reference_id_collisions_for_issue
    (panel #2) if he happened to already be looking at one of the two
    issues. Only touches raw_items rows that actually HAVE a reference
    (idx_raw_pr_number_base-backed), not a full table scan of every issue -
    same discipline as list_issues_for_reference_any_state above. Returns
    {ref, issue_id, title, project_id, state} per row; grouping/pairing/
    same-project filtering is workgraph_projects.find_all_reference_id_
    collisions' job, not this function's."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT DISTINCT r.pr_number_base AS ref, i.id AS issue_id,
                          i.title AS title, i.project_id AS project_id, i.state AS state
                   FROM raw_items r
                   JOIN issues i ON i.id = r.issue_id
                   WHERE r.pr_number_base IS NOT NULL AND r.pr_number_base != ''"""
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def list_evidence(issue_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT evidence.*, raw_items.thread_key AS thread_key,
                          raw_items.signal_type AS signal_type
                   FROM evidence
                   LEFT JOIN raw_items ON raw_items.id = evidence.raw_item_id
                   WHERE evidence.issue_id = ?
                   ORDER BY evidence.ts DESC""",
                (issue_id,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def list_evidence_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched form of list_evidence - see list_issue_state_history_for_issues
    for why (same N+1 call site, same fix)."""
    if not issue_ids:
        return {}
    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join("?" * len(issue_ids))
            rows = conn.execute(
                f"""SELECT evidence.*, raw_items.thread_key AS thread_key,
                           raw_items.signal_type AS signal_type
                    FROM evidence
                    LEFT JOIN raw_items ON raw_items.id = evidence.raw_item_id
                    WHERE evidence.issue_id IN ({placeholders})
                    ORDER BY evidence.ts DESC""",
                issue_ids,
            ).fetchall()
        finally:
            conn.close()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["issue_id"], []).append(dict(r))
    return out


# --- ingest_cursors ---------------------------------------------------------

def get_cursor(source: str, cursor_key: str) -> Optional[str]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT value FROM ingest_cursors WHERE source = ? AND cursor_key = ?",
                (source, cursor_key),
            ).fetchone()
        finally:
            conn.close()
    return row["value"] if row else None


def set_cursor(source: str, cursor_key: str, value: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO ingest_cursors (source, cursor_key, value, updated_ts)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source, cursor_key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts""",
                (source, cursor_key, value, time.time()),
            )
        finally:
            conn.close()


def claim_daily_run(source: str, today: str) -> bool:
    """Atomically claims 'source' for today's once-a-day run. Fixes a real
    cross-process race: retention/health_check/aristotle_detection/
    personal_learning's daily gates used to read the cursor, do the
    (sometimes slow, file-writing) work, THEN write the cursor - so two
    overlapping scheduled_refresh.py processes (a real failure mode given
    documented Outlook-COM hangs) could both pass the read and both do the
    work concurrently, including both calling backup.run_nightly_snapshot()
    at the same time onto the same snapshot filename. This claims the day
    as a single atomic UPSERT statement - the WHERE clause makes the
    conflict branch a no-op (0 rows changed) when the value already equals
    today, so SQLite's own statement atomicity guarantees at most one
    caller ever observes rowcount > 0 for a given (source, today) pair, no
    matter how many processes race on it. Callers must claim BEFORE doing
    the gated work, not after, or the race just moves."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO ingest_cursors (source, cursor_key, value, updated_ts)
                   VALUES (?, 'last_run_date', ?, ?)
                   ON CONFLICT(source, cursor_key) DO UPDATE SET
                       value = excluded.value, updated_ts = excluded.updated_ts
                   WHERE ingest_cursors.value IS NOT ?""",
                (source, today, time.time(), today),
            )
            return cur.rowcount > 0
        finally:
            conn.close()


# --- worker_status ---------------------------------------------------------

def set_worker_status(worker: str, *, state: str, current_task: Optional[str] = None, detail: Optional[str] = None) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO worker_status (worker, state, current_task, detail, updated_ts)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(worker) DO UPDATE SET
                       state = excluded.state, current_task = excluded.current_task,
                       detail = excluded.detail, updated_ts = excluded.updated_ts""",
                (worker, state, current_task, detail, time.time()),
            )
        finally:
            conn.close()


def get_all_worker_status() -> dict[str, dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM worker_status").fetchall()
        finally:
            conn.close()
    return {r["worker"]: dict(r) for r in rows}


# --- pending_actions ---------------------------------------------------------

def create_pending_action(*, issue_id: str, action_kind: str, worker: str, instructions: Optional[str], message_id: Optional[str]) -> int:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO pending_actions
                   (issue_id, action_kind, worker, instructions, status, message_id, requested_ts, updated_ts)
                   VALUES (?, ?, ?, ?, 'requested', ?, ?, ?)""",
                (issue_id, action_kind, worker, instructions, message_id, now, now),
            )
            return cur.lastrowid
        finally:
            conn.close()


def update_pending_action_status(id: int, status: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE pending_actions SET status = ?, updated_ts = ? WHERE id = ?",
                (status, time.time(), id),
            )
        finally:
            conn.close()


def list_pending_actions(issue_id: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM pending_actions"
    args: list[Any] = []
    if issue_id:
        sql += " WHERE issue_id = ?"
        args.append(issue_id)
    sql += " ORDER BY requested_ts DESC"
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# --- prepared_actions (design doc Section 12.4) -----------------------------

PREPARED_ACTION_TERMINAL_STATES = ("succeeded", "failed", "rejected", "expired", "cancelled")


def create_prepared_action(*, claim_id: Optional[int], action_type: str, proposed_parameters_json: str,
                            evidence_refs_json: str, rationale: str, risk_class: str,
                            idempotency_key: str, required_approval: int = 1,
                            state: str = "proposed") -> int:
    """Design doc Section 12.10 (prompt-injection boundary, a standing
    constraint): required_approval defaults 1 for every action_type - no
    code path in this codebase flips it to 0 based on anything evidence
    content itself says (e.g. a supplier's email cannot mark its own
    resulting action as pre-approved, no matter how it's worded). The one
    real caller (server_lean.py's api_cockpit_action) never passes this
    parameter at all, so it always stays the default."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO prepared_actions
                   (claim_id, action_type, proposed_parameters, evidence_refs, rationale, risk_class,
                    required_approval, state, idempotency_key, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (claim_id, action_type, proposed_parameters_json, evidence_refs_json, rationale, risk_class,
                 required_approval, state, idempotency_key, now),
            )
            return cur.lastrowid
        finally:
            conn.close()


def get_prepared_action(id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM prepared_actions WHERE id = ?", (id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def find_prepared_action_by_idempotency_key(idempotency_key: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM prepared_actions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def update_prepared_action_state(id: int, state: str, *, policy_result: Optional[str] = None) -> None:
    """resolved_ts is stamped once the state reaches a terminal one - never
    overwritten by a later call, mirroring resolve_project_suggestion's own
    'first resolution wins' convention."""
    now = time.time()
    resolved_ts = now if state in PREPARED_ACTION_TERMINAL_STATES else None
    with _lock:
        conn = _connect()
        try:
            if resolved_ts is not None:
                conn.execute(
                    "UPDATE prepared_actions SET state = ?, policy_result = ?, resolved_ts = ? WHERE id = ?",
                    (state, policy_result, resolved_ts, id),
                )
            else:
                conn.execute(
                    "UPDATE prepared_actions SET state = ?, policy_result = ? WHERE id = ?",
                    (state, policy_result, id),
                )
        finally:
            conn.close()


def list_prepared_actions_for_claim(claim_id: int) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM prepared_actions WHERE claim_id = ? ORDER BY created_ts DESC", (claim_id,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def expire_stale_prepared_actions(max_age_seconds: float, *, now: Optional[float] = None) -> int:
    """Bookkeeping sweep, same 'reversible status change, never a delete'
    convention as expire_stale_project_suggestions - a prepared_action
    stuck in a non-terminal state past max_age_seconds (nothing ever
    resolved it - see this table's own schema comment on the missing
    real-world-outcome resolver) gets marked 'expired' rather than left
    looking perpetually in-flight. NOT what blocks a live double-dispatch
    - api_cockpit_action's own inline idempotency-key check (a narrower,
    real-time window) does that regardless of whether this sweep has run
    yet. Returns the number of rows expired."""
    if now is None:
        now = time.time()
    cutoff = now - max_age_seconds
    with _lock:
        conn = _connect()
        try:
            all_states = ("proposed", "ready_for_approval", "approved", "executing", "uncertain")
            placeholders = ", ".join("?" for _ in all_states)
            cur = conn.execute(
                f"UPDATE prepared_actions SET state = 'expired', resolved_ts = ? "
                f"WHERE state IN ({placeholders}) AND created_ts < ?",
                (now, *all_states, cutoff),
            )
            return cur.rowcount
        finally:
            conn.close()


PREPARED_ACTION_STALE_AFTER_SECONDS = 3600  # a worker either dispatches or fails fast - an hour means abandoned/stuck


def run_prepared_action_expiry_daily_if_due(now: Optional[float] = None) -> Optional[int]:
    """Same once-a-day gate as run_suggestion_expiry_daily_if_due
    (workgraph_projects.py) - piggybacks scheduled_refresh.py's 5x/day
    cycle without sweeping 5x. Returns None on every call that isn't the
    day's first claim (a real checkable 'did not run' signal, matching
    the sibling gates' own convention), or the number of rows expired."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not claim_daily_run("prepared_action_expiry", today):
        return None
    return expire_stale_prepared_actions(PREPARED_ACTION_STALE_AFTER_SECONDS, now=now)


# --- nba_choice_log (Part E2, grouping/NBA redesign, 2026-07-30) --------

def create_nba_choice_log(*, issue_id: str, offered_json: str, scoring_inputs_json: str) -> int:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO nba_choice_log
                   (issue_id, offered_ts, offered_json, scoring_inputs_json, status)
                   VALUES (?, ?, ?, ?, 'offered')""",
                (issue_id, now, offered_json, scoring_inputs_json),
            )
            return cur.lastrowid
        finally:
            conn.close()


def get_most_recent_open_choice_log(issue_id: str) -> Optional[dict]:
    """The most recent still-'offered' (not yet chosen/ignored/expired) row
    for this issue, or None. Used both to avoid spamming a new log row on
    every repeat page view (only insert one if none is already open) and
    to resolve which row a real action taken against this issue should
    update."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM nba_choice_log WHERE issue_id = ? AND status = 'offered' "
                "ORDER BY offered_ts DESC LIMIT 1",
                (issue_id,),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def mark_choice_log_chosen(log_id: int, *, chosen_action_kind: Optional[str],
                            resulting_pending_action_id: Optional[int] = None,
                            chosen_note: Optional[str] = None) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """UPDATE nba_choice_log SET status = 'chosen', chosen_action_kind = ?, chosen_ts = ?,
                   resulting_pending_action_id = ?, chosen_note = ? WHERE id = ?""",
                (chosen_action_kind, time.time(), resulting_pending_action_id, chosen_note, log_id),
            )
        finally:
            conn.close()


def expire_stale_nba_choice_logs(older_than_days: float) -> int:
    """Phase 0 fix (D12, 2026-08-03): 'ignored'/'expired' were valid states
    in this table's own CHECK from the start, but nothing ever wrote them -
    an 'offered' row with no matching action just sat open forever. Resolves
    (not deletes) any still-'offered' row older than the cutoff to
    'expired', so the offered-vs-acted-on record stays honest and open_log
    lookups don't keep returning a stale offer that's no longer relevant.
    Returns the number of rows resolved."""
    cutoff = time.time() - older_than_days * 86400
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE nba_choice_log SET status = 'expired' WHERE status = 'offered' AND offered_ts < ?",
                (cutoff,),
            )
            return cur.rowcount
        finally:
            conn.close()


# --- alerts -------------------------------------------------------------

def create_alert(*, issue_id: Optional[str], kind: str, severity: str, summary: str, source_ref: Optional[str] = None) -> int:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO alerts (issue_id, kind, severity, summary, source_ref, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (issue_id, kind, severity, summary, source_ref, time.time()),
            )
            return cur.lastrowid
        finally:
            conn.close()


def list_alerts(dismissed: bool = False) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE dismissed = ? ORDER BY created_ts DESC",
                (int(dismissed),),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_alert(id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM alerts WHERE id = ?", (id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def dismiss_alert(id: int) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE alerts SET dismissed = 1, dismissed_ts = ? WHERE id = ?",
                (time.time(), id),
            )
        finally:
            conn.close()


# --- parties ------------------------------------------------------------

def get_party_by_email(email: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM parties WHERE primary_email = ?", (email.strip().lower(),)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_all_parties() -> list[dict]:
    """Every known party - personal-scale table (hundreds of rows, not
    millions), so a full fetch is the right tool for building an in-memory
    name/local-part index (see workgraph_parties._build_party_indexes)
    rather than a query per candidate name."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM parties").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def upsert_party(
    *, id: str, primary_email: str, display_name: Optional[str],
    affiliation: str, affiliation_confidence: str, affiliation_source: str,
    company: Optional[str],
) -> None:
    """Insert a new party, or (if one already exists for this email) just
    bump last_seen_ts and fill in a display_name if we didn't have one yet -
    never overwrites an existing affiliation, since that may already reflect
    a manual correction (see correct_party_affiliation)."""
    now = time.time()
    email = primary_email.strip().lower()
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT id, display_name FROM parties WHERE primary_email = ?", (email,)
            ).fetchone()
            if existing:
                new_name = existing["display_name"] or display_name
                conn.execute(
                    "UPDATE parties SET display_name = ?, last_seen_ts = ? WHERE primary_email = ?",
                    (new_name, now, email),
                )
            else:
                conn.execute(
                    """INSERT INTO parties
                       (id, primary_email, display_name, affiliation, affiliation_confidence,
                        affiliation_source, company, first_seen_ts, last_seen_ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (id, email, display_name, affiliation, affiliation_confidence,
                     affiliation_source, company, now, now),
                )
        finally:
            conn.close()


def clear_party_company(party_id: str, *, affiliation_source: Optional[str] = None) -> None:
    """Null out a party's guessed `company` - for a known-bad domain-derived
    guess (e.g. a machine-signal sender), NOT a human correction (that's
    correct_party_affiliation, which also flips affiliation_source to
    'manual_correction'). upsert_party's existing-row path only ever updates
    display_name, never company/affiliation_source, so this is the only way
    to fix a bad company already stored on an existing party row."""
    with _lock:
        conn = _connect()
        try:
            if affiliation_source is not None:
                conn.execute(
                    "UPDATE parties SET company = NULL, affiliation_source = ? WHERE id = ?",
                    (affiliation_source, party_id),
                )
            else:
                conn.execute("UPDATE parties SET company = NULL WHERE id = ?", (party_id,))
        finally:
            conn.close()


def correct_party_affiliation(party_id: str, *, affiliation: str, company: Optional[str] = None, reason: Optional[str] = None) -> None:
    """A human (or a worker relaying a human's correction) overrides a
    party's affiliation - sticks permanently at 'H' confidence /
    'manual_correction' source, never re-guessed by the domain heuristic
    again. Logged to audit_log so the correction history is visible."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT affiliation, company FROM parties WHERE id = ?", (party_id,)).fetchone()
            if row is None:
                return
            conn.execute(
                """UPDATE parties SET affiliation = ?, affiliation_confidence = 'H',
                       affiliation_source = 'manual_correction', company = COALESCE(?, company)
                   WHERE id = ?""",
                (affiliation, company, party_id),
            )
            conn.execute(
                """INSERT INTO audit_log (entity_type, entity_id, field, old_value, new_value, changed_ts, reason)
                   VALUES ('party', ?, 'affiliation', ?, ?, ?, ?)""",
                (party_id, row["affiliation"], affiliation, time.time(), reason),
            )
        finally:
            conn.close()


def list_parties(affiliation: Optional[str] = None) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            if affiliation:
                rows = conn.execute("SELECT * FROM parties WHERE affiliation = ? ORDER BY last_seen_ts DESC", (affiliation,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM parties ORDER BY last_seen_ts DESC").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def link_party_to_issue(issue_id: str, party_id: str, role: Optional[str] = None) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO issue_parties (issue_id, party_id, role) VALUES (?, ?, ?)",
                (issue_id, party_id, role),
            )
        finally:
            conn.close()
    invalidate_work_object_signature(issue_id)


def list_parties_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched sibling of list_parties_for_issue - one query across every
    issue instead of one per issue. Added 2026-08-02: api_project_detail's
    client (pjRenderProjectDetail) was calling GET /api/workgraph/issues/
    {id} once per member issue just to get each issue's own party chips -
    real, measured cost, since every request on this server currently blocks
    the single event loop on synchronous SQLite calls (confirmed live: 15
    genuinely-parallel curl requests to that endpoint took 9.3s combined,
    not ~1s as true parallelism would). Fixing that server-wide is a much
    bigger, riskier change than this task called for - this instead removes
    the NEED for those N requests at all, by having api_project_detail
    attach each issue's own parties directly onto it in the one response
    the page already fetches."""
    if not issue_ids:
        return {}
    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join("?" * len(issue_ids))
            rows = conn.execute(
                f"""SELECT p.*, ip.role, ip.issue_id AS issue_id FROM parties p
                    JOIN issue_parties ip ON ip.party_id = p.id
                    WHERE ip.issue_id IN ({placeholders})""",
                issue_ids,
            ).fetchall()
        finally:
            conn.close()
    out: dict[str, list[dict]] = {iid: [] for iid in issue_ids}
    for r in rows:
        d = dict(r)
        out[d["issue_id"]].append(d)
    return out


def list_parties_for_issue(issue_id: str) -> list[dict]:
    return list_parties_for_issues([issue_id]).get(issue_id, [])


def list_issues_for_party(party_id: str) -> list[str]:
    """All issue ids a party appears on - used by the project auto-grouper
    to find the shared-party strong signal."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT issue_id FROM issue_parties WHERE party_id = ?", (party_id,)).fetchall()
        finally:
            conn.close()
    return [r["issue_id"] for r in rows]


def list_issues_for_company(company: str) -> list[str]:
    """Every issue with an EXTERNAL party at this company, regardless of which
    specific person - broader than list_issues_for_party: catches the real
    case of two different contacts at the same supplier, each on a different
    thread about the same deal (list_issues_for_party alone would miss that,
    since it matches one exact person)."""
    if not company:
        return []
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT DISTINCT ip.issue_id FROM issue_parties ip
                   JOIN parties p ON p.id = ip.party_id
                   WHERE p.affiliation = 'external' AND p.company = ? COLLATE NOCASE""",
                (company,),
            ).fetchall()
        finally:
            conn.close()
    return [r["issue_id"] for r in rows]


def search_parties_by_name(query: str, limit: int = 25) -> list[dict]:
    """Fuzzy party lookup for the add-in's "focus on a supplier or person"
    capability (task #241) - list_issues_for_company above requires an
    EXACT company string, no use to a user typing a partial name/company
    into chat. Matches display_name OR company via a case-insensitive
    substring, ranked by last_seen_ts (most recently active contact first,
    same recency bias as list_parties)."""
    query = (query or "").strip()
    if not query:
        return []
    like = f"%{query}%"
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT * FROM parties
                   WHERE display_name LIKE ? COLLATE NOCASE
                      OR company LIKE ? COLLATE NOCASE
                   ORDER BY last_seen_ts DESC LIMIT ?""",
                (like, like, limit),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def project_ids_for_party_query(query: str) -> list[str]:
    """End-to-end resolve for "focus on <supplier/person>" (task #241):
    fuzzy party match -> every issue those parties are linked to -> the
    distinct project_ids those issues belong to. Order preserves first-seen
    project (most-recently-active party's project surfaces first).

    UNIONED with a direct raw_items fallback (2026-08-06, found live while
    building this): the `parties` table is populated only for real, promoted
    issues (workgraph_parties.run's own touched_real_issues scope), and a
    live audit found only 6 of 3478 real issues currently carry any linked
    raw_item at all - a separate, real gap in the corrected pipeline's
    extract_issue_from_project (now fixed going forward, but the historical
    corpus mostly predates that fix and has no evidence trail left to
    backfill it from). Relying on the parties table alone would make this
    feature return almost nothing for tonight's demo despite the raw
    from_actor/participants text for those senders sitting right there in
    raw_items, already linked to a real project via a cluster. This fallback
    is honest about being lower-precision (substring match against raw
    text, no affiliation/company canonicalization) - used only to fill gaps
    the party match didn't already cover, never to override it."""
    seen: list[str] = []

    parties = search_parties_by_name(query)
    issue_ids: list[str] = []
    for p in parties:
        issue_ids.extend(list_issues_for_party(p["id"]))
    if issue_ids:
        issues_by_id = get_issues_by_ids(list(dict.fromkeys(issue_ids)))
        for iid in issue_ids:
            pid = issues_by_id.get(iid, {}).get("project_id")
            if pid and pid not in seen:
                seen.append(pid)

    query = (query or "").strip()
    if query:
        like = f"%{query}%"
        with _lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    """SELECT DISTINCT wo.parent_id AS project_id
                       FROM raw_items ri
                       JOIN work_objects wo ON wo.id = ri.issue_id
                       WHERE wo.parent_id IS NOT NULL
                         AND (ri.from_actor LIKE ? COLLATE NOCASE
                              OR ri.participants LIKE ? COLLATE NOCASE)
                       ORDER BY ri.occurred_ts DESC""",
                    (like, like),
                ).fetchall()
            finally:
                conn.close()
        for r in rows:
            pid = r["project_id"]
            if pid not in seen:
                seen.append(pid)

    return seen


# --- prerequisite_rules (Aristotle, task #51) --------------------------------

def create_prerequisite_rule(*, trigger_signal_type: str, requires_signal_type: str,
                              match_on: str, reason: str, created_by: str) -> int:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO prerequisite_rules
                   (trigger_signal_type, requires_signal_type, match_on, reason, active, created_ts, created_by)
                   VALUES (?, ?, ?, ?, 1, ?, ?)""",
                (trigger_signal_type, requires_signal_type, match_on, reason, time.time(), created_by),
            )
            return cur.lastrowid
        finally:
            conn.close()


def list_prerequisite_rules(active_only: bool = False) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            sql = "SELECT * FROM prerequisite_rules"
            if active_only:
                sql += " WHERE active = 1"
            sql += " ORDER BY created_ts DESC"
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_active_prerequisite_rules_for_trigger(trigger_signal_type: str) -> list[dict]:
    if not trigger_signal_type:
        return []
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM prerequisite_rules WHERE active = 1 AND trigger_signal_type = ?",
                (trigger_signal_type,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def set_prerequisite_rule_active(rule_id: int, active: bool) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE prerequisite_rules SET active = ? WHERE id = ?", (1 if active else 0, rule_id))
        finally:
            conn.close()


def delete_prerequisite_rule(rule_id: int) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM prerequisite_rules WHERE id = ?", (rule_id,))
        finally:
            conn.close()


def list_distinct_signal_types_in_use() -> list[str]:
    """Every signal_type actually present on a real raw_item (not just the
    full catalog in workgraph_signals.known_signal_types()) - detection only
    needs to consider types that have actually occurred, task #52."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT signal_type FROM raw_items WHERE signal_type IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    return [r["signal_type"] for r in rows]


def get_raw_items_by_signal_type(signal_type: str) -> list[dict]:
    """id/issue_id/occurred_ts for every raw_item classified with this
    signal_type - detection support, task #52."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, issue_id, occurred_ts FROM raw_items WHERE signal_type = ?",
                (signal_type,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def count_raw_items_by_month_and_signal_type(since_ts: float) -> list[dict]:
    """Task #66 (signal trend view): one row per (month, signal_type) with
    a count, for every classified raw_item occurring at/after since_ts.
    Month bucketing is UTC (strftime with 'unixepoch' - occurred_ts is
    itself a UTC epoch, so this avoids the local-timezone-near-a-boundary
    drift already named/fixed elsewhere in this codebase, e.g. workgraph_
    nba._due_urgency). Pure aggregation, no LLM, no interpretation - the
    caller decides which months/types to show."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT strftime('%Y-%m', occurred_ts, 'unixepoch') AS month,
                          signal_type, COUNT(*) AS count
                   FROM raw_items
                   WHERE signal_type IS NOT NULL AND occurred_ts >= ?
                   GROUP BY month, signal_type
                   ORDER BY month ASC""",
                (since_ts,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# --- pending_prerequisite_suggestions (Aristotle, tasks #52/#54) ------------

def create_prerequisite_suggestion(*, origin: str, trigger_signal_type: Optional[str],
                                    requires_signal_type: Optional[str], match_on: Optional[str],
                                    reason: Optional[str], evidence: Optional[str],
                                    raw_explanation: Optional[str], proposed_by: Optional[str]) -> int:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO pending_prerequisite_suggestions
                   (origin, trigger_signal_type, requires_signal_type, match_on, reason,
                    evidence, raw_explanation, proposed_by, status, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (origin, trigger_signal_type, requires_signal_type, match_on, reason,
                 evidence, raw_explanation, proposed_by, time.time()),
            )
            return cur.lastrowid
        finally:
            conn.close()


def list_prerequisite_suggestions(status: Optional[str] = "pending") -> list[dict]:
    """status=None returns every suggestion regardless of status - used by
    detection to avoid re-proposing something already confirmed OR already
    rejected, not just pending ones."""
    with _lock:
        conn = _connect()
        try:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM pending_prerequisite_suggestions ORDER BY created_ts DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pending_prerequisite_suggestions WHERE status = ? ORDER BY created_ts DESC",
                    (status,),
                ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_prerequisite_suggestion(suggestion_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM pending_prerequisite_suggestions WHERE id = ?", (suggestion_id,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def resolve_prerequisite_suggestion(suggestion_id: int, status: str) -> None:
    if status not in ("confirmed", "rejected"):
        raise ValueError(f"invalid resolution status: {status!r}")
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE pending_prerequisite_suggestions SET status = ?, resolved_ts = ? WHERE id = ?",
                (status, time.time(), suggestion_id),
            )
        finally:
            conn.close()


def get_most_recent_pending_suggestion_by_asker(asker: str, since_ts: float) -> Optional[dict]:
    """The single most recent still-pending taught_via_chat suggestion from
    this asker, created after since_ts (a recency window) - task #54's
    confirm-in-chat mechanism resolves against this, not an open-ended
    search back through all history."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT * FROM pending_prerequisite_suggestions
                   WHERE status = 'pending' AND origin = 'taught_via_chat'
                   AND proposed_by = ? AND created_ts >= ?
                   ORDER BY created_ts DESC LIMIT 1""",
                (asker, since_ts),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def get_most_recent_clarifying_suggestion_by_asker(asker: str, since_ts: float) -> Optional[dict]:
    """Task #62: the suggestion this asker is CURRENTLY mid-clarification-
    conversation on, if any - distinct from get_most_recent_pending_
    suggestion_by_asker (which finds any pending suggestion, clarifying or
    not). A plain 'yes'/'no' reply while a clarification is active must
    route through the conversation, not accidentally be read as a confirm/
    reject answer for a DIFFERENT, already-fully-structured pending
    suggestion from the same asker."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT * FROM pending_prerequisite_suggestions
                   WHERE status = 'pending' AND origin = 'taught_via_chat'
                   AND proposed_by = ? AND created_ts >= ? AND clarify_stage IS NOT NULL
                   ORDER BY created_ts DESC LIMIT 1""",
                (asker, since_ts),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def set_suggestion_clarify_stage(suggestion_id: int, stage: Optional[str]) -> None:
    """stage=None ends the conversation (either fully structured now, or the
    asker declined/cancelled it) - the row then behaves exactly like any
    other pending suggestion again."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE pending_prerequisite_suggestions SET clarify_stage = ? WHERE id = ?",
                (stage, suggestion_id),
            )
        finally:
            conn.close()


def update_prerequisite_suggestion_structure(suggestion_id: int, *, trigger_signal_type: Optional[str] = None,
                                              requires_signal_type: Optional[str] = None,
                                              match_on: Optional[str] = None, reason: Optional[str] = None) -> None:
    """Fills in one structured field at a time as task #62's clarification
    conversation collects each answer - only the fields actually passed
    (non-None) are updated, so answering "what triggers this" doesn't wipe
    out an already-answered "what does it require"."""
    fields = {"trigger_signal_type": trigger_signal_type, "requires_signal_type": requires_signal_type,
              "match_on": match_on, "reason": reason}
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                f"UPDATE pending_prerequisite_suggestions SET {assignments} WHERE id = ?",
                (*fields.values(), suggestion_id),
            )
        finally:
            conn.close()


_TOPIC_KEY_STRIP = re.compile(r"^\s*(?:\[[^\]]{1,20}\]|re|fwd?|fw)\s*:?\s*", re.I)


def normalize_topic_key(subject: str) -> str:
    """Lowercased, prefix/tag-stripped subject core, so two differently
    prefixed/tagged subject lines about the same underlying topic reduce to
    the same key - e.g. 'MARC REVIEW REQUESTED: Veeva CRM press release' and
    '[EXTERNAL] Re: NICK/JONATHAN APPROVAL REQUESTED: Veeva CRM press release
    quote' both reduce toward a shared 'veeva crm press release' core. Kept
    separate from strip_subject_prefix (workgraph_classify.py), which is used
    to build the DISPLAY title and deliberately keeps an [EXTERNAL] tag - this
    one is for MATCHING only."""
    s = subject or ""
    prev = None
    while prev != s:
        prev = s
        s = _TOPIC_KEY_STRIP.sub("", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# --- projects -------------------------------------------------------------

def next_project_id() -> str:
    """Same max-existing-suffix scheme as next_issue_id() - stable under deletion."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT id FROM projects WHERE id LIKE 'proj-%'").fetchall()
        finally:
            conn.close()
    max_n = 0
    for r in rows:
        try:
            max_n = max(max_n, int(r["id"].split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"proj-{max_n + 1:03d}"


def create_project_with_new_id(**kwargs: Any) -> str:
    """next_project_id() + create_project(), race-safe - same fix and same
    rationale as create_issue_with_new_id(). Returns the actually-used id."""
    for attempt in range(25):
        project_id = next_project_id()
        try:
            create_project(id=project_id, **kwargs)
            return project_id
        except sqlite3.IntegrityError:
            time.sleep(random.uniform(0, 0.01) * (attempt + 1))
            continue
    raise RuntimeError("could not allocate a unique project id after 25 attempts")


def create_project(*, id: str, name: str, category: Optional[str] = None, status: str = "active") -> None:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO projects (id, name, category, status, opened_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (id, name, category, status, now, now),
            )
        finally:
            conn.close()


def get_project(id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def set_project_status(project_id: str, status: str) -> None:
    """Task #81 remediation: used to archive a project that a re-grouping
    fix leaves with zero member issues, rather than hard-deleting the row
    (no delete_project function exists, deliberately not added for a
    one-off cleanup - archiving is reversible, a hard delete of a project
    row with real history/synthesis attached is not).

    'dismissed' added task #62 - a real, distinct-from-'done' outcome at
    the project level, same reasoning as issues.state's task #44.

    'noise-archived' added (2026-08-06, Marc's direct request): a whole
    project can now be marked noise the same way an individual issue
    already could - the DB CHECK constraint on work_objects.status already
    permitted this value, this whitelist was just never extended to match."""
    if status not in ("active", "waiting", "done", "archived", "dismissed", "noise-archived"):
        raise ValueError(f"invalid project status: {status!r}")
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
                         (status, time.time(), project_id))
        finally:
            conn.close()


def list_projects(status: Optional[list[str]] = None) -> list[dict]:
    """`display_title` prefers the deterministic/curator-derived title over the
    raw `name` (task #167/#168, 2026-08-04) - list_issues (line ~2624) already
    did this; list_projects never got the same treatment, so a project's list
    row stayed the raw, boilerplate-heavy subject line _project_name_for fell
    back to at creation even after a better title existed in synthesis."""
    sql = """SELECT projects.*, synthesis.derived_title AS synth_derived_title
             FROM projects
             LEFT JOIN synthesis ON synthesis.entity_type = 'project' AND synthesis.entity_id = projects.id"""
    args: list[Any] = []
    if status:
        placeholders = ", ".join("?" for _ in status)
        sql += f" WHERE projects.status IN ({placeholders})"
        args.extend(status)
    sql += " ORDER BY projects.updated_at DESC"
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["display_title"] = d.get("synth_derived_title") or d["name"]
        out.append(d)
    return out


def mark_project_deep_dived(project_id: str, note: str) -> None:
    """The one place last_deep_dive_ts/note ever changes (design doc
    Section 10.4) - called by the Project Deep-Dive routine's completion
    POST when it finishes a wake, never inferred from the model's own
    prose."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE projects SET last_deep_dive_ts = ?, last_deep_dive_note = ? WHERE id = ?",
                (time.time(), note, project_id),
            )
        finally:
            conn.close()


def list_issues_for_project(project_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM issues WHERE project_id = ? ORDER BY priority_score DESC NULLS LAST", (project_id,)).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def list_clusters_for_project(project_id: str) -> list[dict]:
    """Cluster counterpart to list_issues_for_project - a Phase-C-promoted
    project's members are routinely clusters, not real issues, until Phase
    D (curator's content extraction) ever runs on it. Deliberately a
    SEPARATE function, not a widened list_issues_for_project: every
    existing caller of that one (the project-detail render route, deep-
    dive picker, aristotle gating, split-siblings) renders or acts on real
    issues specifically, and clusters must stay invisible to all of them by
    construction - only curator's own extraction step needs to see this."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT id, title, category, status AS state, priority, priority_score,
                          nba_action_kind, nba_reason, owner, due, opened_at, updated_at,
                          confidence_tier, parent_id AS project_id, lesson_id_cited,
                          has_unmet_prerequisite, claims_revision
                   FROM work_objects WHERE parent_id = ? AND object_type = 'request' AND is_raw_cluster = 1
                   ORDER BY id""",
                (project_id,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def project_has_confirmed_grouping(project_id: str) -> bool:
    """Corrected pipeline Phase D (2026-08-05): a project is only eligible
    for curator's real-issue extraction once at least one member's
    membership_state is 'confirmed' - either an exact-reference/precedent
    auto-merge (_merge_or_defer now marks both sides confirmed, see
    workgraph_projects's own docstring on why) or a real human/curator
    confirm (confirm_suggestion). membership_state lives on the MEMBER, not
    the project row itself (a project has no parent of its own for the
    column to mean anything) - this is the project-level proxy for "was
    this grouping actually reviewed/high-confidence, not just a raw
    provisional guess still open to correction." A still-provisional-only
    project keeps getting its synthesis narrative refreshed as usual (see
    list_stale_entities) - it just isn't handed to the extraction step
    yet, since extracting permanent real issues from a grouping that might
    still get split back apart would produce issues that are wrong too."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT 1 FROM work_objects
                   WHERE parent_id = ? AND object_type = 'request' AND membership_state = 'confirmed'
                   LIMIT 1""",
                (project_id,),
            ).fetchone()
        finally:
            conn.close()
    return row is not None


def assign_issue_to_project(issue_id: str, project_id: Optional[str], *, reason: Optional[str] = None) -> None:
    """The one place issue.project_id ever changes - covers auto-grouping,
    a worker splitting/merging on Marc's conversational correction, and
    manual reassignment alike. Logs the transition to audit_log.

    Corrected pipeline Phase C: raw work_objects read/write, not the
    `issues` view - issue_id may now be a cluster (is_raw_cluster=1),
    invisible to that view by construction (same class of bug as
    merge_issues_txn's own fix, same day)."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT parent_id FROM work_objects WHERE id = ?", (issue_id,)).fetchone()
            old_project_id = row["parent_id"] if row else None
            if old_project_id == project_id:
                return
            conn.execute("UPDATE work_objects SET parent_id = ?, updated_at = ? WHERE id = ?", (project_id, time.time(), issue_id))
            if project_id is not None:
                conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (time.time(), project_id))
            conn.execute(
                """INSERT INTO audit_log (entity_type, entity_id, field, old_value, new_value, changed_ts, reason)
                   VALUES ('project', ?, 'issue_membership', ?, ?, ?, ?)""",
                (project_id or old_project_id, old_project_id, project_id, time.time(), reason),
            )
        finally:
            conn.close()


def _allocate_project_id_on(conn: sqlite3.Connection) -> str:
    """Same max-existing-suffix scheme as next_project_id(), but queries the
    GIVEN connection instead of opening a new one - for use inside
    merge_issues_txn's own transaction, where BEGIN IMMEDIATE already holds
    SQLite's RESERVED lock for the whole call, making the classic
    SELECT-MAX-then-INSERT race impossible without needing the separate
    IntegrityError-retry loop next_project_id()'s own caller
    (create_project_with_new_id) needs outside a transaction."""
    rows = conn.execute("SELECT id FROM projects WHERE id LIKE 'proj-%'").fetchall()
    max_n = 0
    for r in rows:
        try:
            max_n = max(max_n, int(r["id"].split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"proj-{max_n + 1:03d}"


def would_collide_established_projects(issue_id_a: str, issue_id_b: str, *,
                                        _conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """Read-only check: would merging these two issues collide two ALREADY-
    established projects? Gates on how established the LOSING side is
    (2+ real members beyond the two triggering issues themselves), not on
    which signal triggered the merge - a durable rule that protects EVERY
    merge_issues_txn caller (reference-ID auto-merge, a confirmed
    suggestion, a precedent auto-resolve), not just today's known ones. A
    1-member loser (just the issue being merged, e.g. an earlier one-off
    merge_issues_txn call created a singleton project) is low-risk -
    nothing else gets uprooted - and stays automatic; a real 2+-member
    project has its own history/synthesis/attachments and must not be
    silently collapsed.

    Returns None when there's no collision (including the low-risk
    1-member-loser case), or {"winner_project_id", "loser_project_id",
    "loser_members"} (full member id list, for a side-by-side review)
    when it fires.

    _conn lets merge_issues_txn reuse its own already-open, already-locked
    connection instead of re-acquiring _lock (a plain non-reentrant
    threading.Lock - re-acquiring it from the same thread would deadlock).

    Corrected pipeline Phase C (2026-08-05): queries work_objects directly
    rather than through the `issues` view - issue_id_a/issue_id_b may now be
    clusters (is_raw_cluster=1), which the view excludes by construction
    (see the `issues` view's own definition). A raw work_objects query has
    no such filter and gives the identical column shape (parent_id is
    project_id) for either kind."""
    def _query(conn: sqlite3.Connection) -> Optional[dict]:
        row_a = conn.execute("SELECT parent_id FROM work_objects WHERE id = ?", (issue_id_a,)).fetchone()
        row_b = conn.execute("SELECT parent_id FROM work_objects WHERE id = ?", (issue_id_b,)).fetchone()
        project_a = row_a["parent_id"] if row_a else None
        project_b = row_b["parent_id"] if row_b else None
        if not (project_a and project_b and project_a != project_b):
            return None
        winner, loser = project_a, project_b
        members = conn.execute(
            "SELECT id FROM work_objects WHERE parent_id = ? AND object_type = 'request'", (loser,)
        ).fetchall()
        loser_members = [m["id"] for m in members if m["id"] not in (issue_id_a, issue_id_b)]
        if not loser_members:
            return None
        return {"winner_project_id": winner, "loser_project_id": loser, "loser_members": loser_members}

    if _conn is not None:
        return _query(_conn)
    with _lock:
        conn = _connect()
        try:
            return _query(conn)
        finally:
            conn.close()


def force_merge_projects(project_a: str, project_b: str, *, reason_label: str) -> str:
    """Actually collapses two established projects into one - the real
    execution of an explicitly-human-authorized 'merge_projects'
    reconciliation (see workgraph_projects._confirm_merge_projects_
    suggestion). project_a always wins - the caller already decided which
    side is "winner" before calling this (same tie-break merge_issues_txn's
    own collision branch used before this gate existed). One transaction,
    same BEGIN IMMEDIATE/COMMIT/ROLLBACK + bounded-retry pattern as
    merge_issues_txn - see that function's own docstring for why."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            for attempt in range(5):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError:
                    if attempt == 4:
                        raise
                    time.sleep(random.uniform(0, 0.02) * (attempt + 1))
            try:
                # Corrected pipeline Phase C: raw work_objects query/write,
                # not the `issues` view - a losing project's members may
                # now be clusters, invisible to that view by construction.
                members = conn.execute(
                    "SELECT id FROM work_objects WHERE parent_id = ? AND object_type = 'request'", (project_b,)
                ).fetchall()
                for member in members:
                    conn.execute(
                        "UPDATE work_objects SET parent_id = ?, updated_at = ? WHERE id = ?",
                        (project_a, now, member["id"]),
                    )
                    conn.execute(
                        """INSERT INTO audit_log (entity_type, entity_id, field, old_value, new_value, changed_ts, reason)
                           VALUES ('project', ?, 'issue_membership', ?, ?, ?, ?)""",
                        (project_a, project_b, project_a, now, f"{reason_label}: project {project_b} merged into {project_a}"),
                    )
                conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", ("archived", now, project_b))
                conn.execute("COMMIT")
                return project_a
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
        finally:
            conn.close()


def merge_issues_txn(issue_id_a: str, issue_id_b: str, *, reason_label: str,
                      new_project_name: str, new_project_category: Optional[str]) -> dict:
    """The real merge, as one all-or-nothing transaction. workgraph_projects.
    merge_issues() is a thin wrapper around this.

    Replaces what used to be a sequence of independent autocommit
    connections/statements (get_issue, list_issues_for_project,
    assign_issue_to_project x2-3, set_project_status, create_project_with_
    new_id) - a crash partway through (e.g. after reassigning some but not
    all of a losing project's members, or after archiving the loser but
    before reassigning issue_a/issue_b themselves) left the DB in a
    partially-merged, inconsistent state with no recovery path. This talks
    to ONE connection directly rather than calling back into other ws.*
    helpers - _lock (module-level, above) is a plain non-reentrant
    threading.Lock, so calling back into another ws.* function that itself
    acquires _lock from inside this function's own `with _lock:` block
    would deadlock the calling thread against itself.

    Mirrors the alerts-table migration's own BEGIN IMMEDIATE/COMMIT/ROLLBACK
    pattern in init_workgraph() above (same file) - that migration exists
    for the identical reason (a multi-step write that must be all-or-
    nothing, in the same multi-process WAL-mode environment). The bounded
    retry on BEGIN IMMEDIATE mirrors create_issue_with_new_id's/
    create_task's own IntegrityError-retry idiom - _connect() sets no
    busy_timeout, so a concurrent writer holding the lock raises
    immediately rather than waiting.

    2026-07-31 (step 5, mandatory reconciliation): checked FIRST, before
    ever opening a write transaction - if this merge would collide two
    ALREADY-established projects (see would_collide_established_projects),
    refuses to auto-collapse. Returns {"status": "merged", "project_id":
    ...} or {"status": "deferred", "winner_project_id": ...,
    "loser_project_id": ...} - every caller must check "status" now, not
    assume a bare project_id. (2026-08-07: this used to also persist a
    'merge_projects' pending_project_suggestions row and return its id -
    dropped along with that whole retired review queue, since nothing ever
    read the id back; workgraph_pipeline2.process_new_item's own "try the
    next candidate" handling of a deferred result needs nothing more than
    the status itself.)"""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            collision = would_collide_established_projects(issue_id_a, issue_id_b, _conn=conn)
            if collision is not None:
                return {"status": "deferred",
                        "winner_project_id": collision["winner_project_id"],
                        "loser_project_id": collision["loser_project_id"]}

            for attempt in range(5):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError:
                    if attempt == 4:
                        raise
                    time.sleep(random.uniform(0, 0.02) * (attempt + 1))
            try:
                # Corrected pipeline Phase C (2026-08-05): raw work_objects
                # reads/writes throughout this transaction, not the `issues`
                # view - issue_id_a/issue_id_b (and any project's members)
                # may now be clusters (is_raw_cluster=1), which the view
                # excludes by construction. work_objects.parent_id is the
                # same underlying column the view calls project_id.
                row_a = conn.execute("SELECT parent_id FROM work_objects WHERE id = ?", (issue_id_a,)).fetchone()
                row_b = conn.execute("SELECT parent_id FROM work_objects WHERE id = ?", (issue_id_b,)).fetchone()
                project_a = row_a["parent_id"] if row_a else None
                project_b = row_b["parent_id"] if row_b else None

                if project_a and project_b and project_a != project_b:
                    winner, loser = project_a, project_b
                    members = conn.execute(
                        "SELECT id FROM work_objects WHERE parent_id = ? AND object_type = 'request'", (loser,)
                    ).fetchall()
                    for member in members:
                        member_id = member["id"]
                        if member_id in (issue_id_a, issue_id_b):
                            continue  # reassigned explicitly below either way
                        conn.execute(
                            "UPDATE work_objects SET parent_id = ?, updated_at = ? WHERE id = ?",
                            (winner, now, member_id),
                        )
                        conn.execute(
                            """INSERT INTO audit_log (entity_type, entity_id, field, old_value, new_value, changed_ts, reason)
                               VALUES ('project', ?, 'issue_membership', ?, ?, ?, ?)""",
                            (winner, loser, winner, now, f"{reason_label}: project {loser} merged into {winner}"),
                        )
                    conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", ("archived", now, loser))
                    project_id = winner
                elif project_a:
                    project_id = project_a
                elif project_b:
                    project_id = project_b
                else:
                    project_id = _allocate_project_id_on(conn)
                    conn.execute(
                        "INSERT INTO projects (id, name, category, status, opened_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (project_id, new_project_name, new_project_category, "active", now, now),
                    )

                for iid, other in ((issue_id_a, issue_id_b), (issue_id_b, issue_id_a)):
                    row = conn.execute("SELECT parent_id FROM work_objects WHERE id = ?", (iid,)).fetchone()
                    old_project_id = row["parent_id"] if row else None
                    if old_project_id == project_id:
                        continue
                    conn.execute("UPDATE work_objects SET parent_id = ?, updated_at = ? WHERE id = ?", (project_id, now, iid))
                    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
                    conn.execute(
                        """INSERT INTO audit_log (entity_type, entity_id, field, old_value, new_value, changed_ts, reason)
                           VALUES ('project', ?, 'issue_membership', ?, ?, ?, ?)""",
                        (project_id, old_project_id, project_id, now, f"{reason_label} with {other}"),
                    )

                conn.execute("COMMIT")
                return {"status": "merged", "project_id": project_id}
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
        finally:
            conn.close()


def merge_issue_into(loser_id: str, winner_id: str, *, reason: str, actor: str) -> dict:
    """Real ISSUE-level merge (2026-08-03) - distinct from merge_issues_txn
    above, which only ever joins PROJECT membership and is a no-op for two
    issues already in the same project (the exact shape the identity_
    anchors backfill's reference-conflict report surfaced: 14 real pairs,
    all already co-located). Moves raw_items/evidence/issue_parties/
    identity_anchors/source_containers/checklist_dismissals from loser to
    winner as one all-or-nothing transaction (same BEGIN IMMEDIATE/COMMIT/
    ROLLBACK discipline as merge_issues_txn, for the identical reason - a
    crash partway through a multi-table move must not leave orphaned rows).

    Never deletes the loser - it's set to state='dismissed' (issue_state_
    history logs the transition with `actor`, same as any other state
    change) and left in place, pointing nowhere special but fully intact,
    so nothing is lost and a wrong call is a data-preserving mistake, not
    a destructive one. Both issues get a real, visible evidence row
    recording the merge; audit_log gets a matching entry (same convention
    merge_issues_txn's own project-merge path already uses).

    Exclusive identity_anchors already held by the winner are left with
    the winner (never duplicated into a constraint violation); the loser's
    matching ones are superseded, not deleted. Non-conflicting anchors move
    over normally.

    Returns {"status": "merged", "winner_id", "loser_id", "raw_items_moved",
    "evidence_moved"} or {"status": "not_found"} if either id doesn't
    resolve to a real issue - never raises for that, same as this file's
    other lookup-then-act functions."""
    if loser_id == winner_id:
        raise ValueError("cannot merge an issue into itself")
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            loser = conn.execute("SELECT * FROM issues WHERE id = ?", (loser_id,)).fetchone()
            winner = conn.execute("SELECT * FROM issues WHERE id = ?", (winner_id,)).fetchone()
            if loser is None or winner is None:
                return {"status": "not_found"}

            for attempt in range(5):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError:
                    if attempt == 4:
                        raise
                    time.sleep(random.uniform(0, 0.02) * (attempt + 1))
            try:
                raw_items_moved = conn.execute(
                    "UPDATE raw_items SET issue_id = ? WHERE issue_id = ?", (winner_id, loser_id)
                ).rowcount
                # Section 12.2: `evidence` is now a view over evidence_units/
                # evidence_unit_links (an INSTEAD OF trigger does the real
                # work) - confirmed empirically that cursor.rowcount for an
                # UPDATE against a view reports 0 regardless of how many rows
                # the trigger actually touched (the outer statement never
                # directly changes a real table itself). Count first, since
                # we already know every one of this issue's evidence rows is
                # about to move unconditionally - no need to trust rowcount
                # for a number we can compute directly.
                evidence_moved = conn.execute(
                    "SELECT COUNT(*) FROM evidence WHERE issue_id = ?", (loser_id,)
                ).fetchone()[0]
                conn.execute(
                    "UPDATE evidence SET issue_id = ? WHERE issue_id = ?", (winner_id, loser_id)
                )
                conn.execute(
                    """INSERT OR IGNORE INTO issue_parties (issue_id, party_id, role)
                       SELECT ?, party_id, role FROM issue_parties WHERE issue_id = ?""",
                    (winner_id, loser_id),
                )
                conn.execute("DELETE FROM issue_parties WHERE issue_id = ?", (loser_id,))
                conn.execute(
                    """UPDATE identity_anchors SET issue_id = ?
                       WHERE issue_id = ? AND status = 'active'
                       AND NOT (exclusive = 1 AND EXISTS (
                           SELECT 1 FROM identity_anchors w
                           WHERE w.issue_id = ? AND w.exclusive = 1 AND w.status = 'active'
                             AND w.anchor_type = identity_anchors.anchor_type
                             AND w.normalized_value = identity_anchors.normalized_value))""",
                    (winner_id, loser_id, winner_id),
                )
                conn.execute(
                    "UPDATE identity_anchors SET status = 'superseded' WHERE issue_id = ? AND status = 'active'",
                    (loser_id,),
                )
                conn.execute("UPDATE source_containers SET issue_id = ? WHERE issue_id = ?", (winner_id, loser_id))
                conn.execute(
                    """INSERT OR IGNORE INTO checklist_dismissals
                       (issue_id, item_key, kind, text_snippet, dismissed_ts, actor, status)
                       SELECT ?, item_key, kind, text_snippet, dismissed_ts, actor, status
                       FROM checklist_dismissals WHERE issue_id = ?""",
                    (winner_id, loser_id),
                )
                conn.execute("DELETE FROM checklist_dismissals WHERE issue_id = ?", (loser_id,))

                conn.execute(
                    "INSERT INTO issue_state_history (issue_id, from_state, to_state, changed_ts, actor) VALUES (?, ?, 'dismissed', ?, ?)",
                    (loser_id, loser["state"], now, actor),
                )
                conn.execute("UPDATE issues SET state = 'dismissed', updated_at = ? WHERE id = ?", (now, loser_id))
                conn.execute(
                    "INSERT INTO evidence (issue_id, raw_item_id, type, summary, ts) VALUES (?, NULL, 'worker_action', ?, ?)",
                    (winner_id, f"Merged {loser_id} into this issue: {reason}", now),
                )
                conn.execute(
                    "INSERT INTO evidence (issue_id, raw_item_id, type, summary, ts) VALUES (?, NULL, 'worker_action', ?, ?)",
                    (loser_id, f"Merged into {winner_id}: {reason}", now),
                )
                conn.execute(
                    """INSERT INTO audit_log (entity_type, entity_id, field, old_value, new_value, changed_ts, reason)
                       VALUES ('issue', ?, 'merged_into', ?, ?, ?, ?)""",
                    (loser_id, loser_id, winner_id, now, reason),
                )
                conn.execute("COMMIT")
                result = {"status": "merged", "winner_id": winner_id, "loser_id": loser_id,
                          "raw_items_moved": raw_items_moved, "evidence_moved": evidence_moved}
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
        finally:
            conn.close()
    # The winner absorbed the loser's parties/raw_items/containers above -
    # both cached signatures (Section 12.7) are now stale, not just the
    # winner's (the loser keeps its issue row, per this function's own
    # "never deletes the loser" contract, so a stale cached signature for
    # it would otherwise persist indefinitely).
    invalidate_work_object_signature(winner_id)
    invalidate_work_object_signature(loser_id)
    return result


def create_project_link(*, from_project_id: str, to_project_id: str, link_type: str,
                         reason: str, created_by: Optional[str] = None) -> int:
    """Idempotent - reuses an existing link with the same (from, to, type)
    rather than creating a duplicate, same check-then-insert shape as
    create_project_suggestion. Direction is stored in creation order for
    symmetric types (e.g. 'related') and is meaningful for directional
    types (e.g. 'enables') - callers decide which project is from/to."""
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                """SELECT id FROM project_links
                   WHERE from_project_id = ? AND to_project_id = ? AND link_type = ?""",
                (from_project_id, to_project_id, link_type),
            ).fetchone()
            if existing:
                return existing["id"]
            cur = conn.execute(
                """INSERT INTO project_links (from_project_id, to_project_id, link_type, reason, created_ts, created_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (from_project_id, to_project_id, link_type, reason, time.time(), created_by),
            )
            return cur.lastrowid
        finally:
            conn.close()


def list_project_links_for_project(project_id: str) -> list[dict]:
    """Every link touching this project, either direction - a project
    detail view shouldn't have to know whether it was the 'from' or 'to'
    side when the link was created."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT * FROM project_links WHERE from_project_id = ? OR to_project_id = ?
                   ORDER BY created_ts DESC""",
                (project_id, project_id),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# --- source_containers / identity_anchors (identity formalization v0, ------
# --- 2026-08-03) -------------------------------------------------------

def upsert_source_container(*, id: str, source: str, container_type: str, exact_key: str,
                             key_quality: str, issue_id: Optional[str] = None) -> None:
    """Idempotent - re-running the backfill (or a real ingest hit on an
    already-known container) just no-ops rather than raising on the UNIQUE
    (source, container_type, exact_key) constraint."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO source_containers (id, source, container_type, exact_key, key_quality, issue_id, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, container_type, exact_key) DO UPDATE SET issue_id = excluded.issue_id""",
                (id, source, container_type, exact_key, key_quality, issue_id, time.time()),
            )
        finally:
            conn.close()


def source_container_lookup(*, source: str, container_type: str, exact_key: str) -> Optional[dict]:
    """Task #184 Phase B (2026-08-05): the read-by-identity counterpart
    upsert_source_container's own UNIQUE(source, container_type, exact_key)
    key was always meant to support - list_source_containers only ever
    supported the reverse direction (by issue_id). This is what lets the
    live clustering path check "does a container for this exact key
    already exist" BEFORE an issue is created, the same role the older
    flat-string-key thread_map model played before this replaced it
    (thread_map itself removed 2026-08-07, dead code). Returns the full
    row (including issue_id, possibly None if
    a container was ever recorded with no issue attached yet) or None if
    no such container exists at all."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM source_containers WHERE source = ? AND container_type = ? AND exact_key = ?",
                (source, container_type, exact_key),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_source_containers(issue_id: Optional[str] = None) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            if issue_id is not None:
                rows = conn.execute("SELECT * FROM source_containers WHERE issue_id = ?", (issue_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM source_containers").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def create_identity_anchor(*, anchor_type: str, normalized_value: str, anchor_strength: str,
                            exclusive: bool, issue_id: str, created_by: str = "backfill",
                            reason_json: str = "{}", now: Optional[float] = None) -> Optional[int]:
    """Returns the new row's id, or None if either (a) this exact
    (anchor_type, normalized_value, issue_id) is already recorded and
    active - a dedupe no-op, safe to call repeatedly (e.g. on every backfill
    re-run) - or (b) an EXCLUSIVE anchor with this (anchor_type,
    normalized_value) is already active on a DIFFERENT issue (the
    idx_identity_anchor_exclusive constraint) - a real, expected outcome
    during backfill (a pre-existing fragmentation case, e.g. the same PR
    number legitimately or erroneously touching two issues today), never an
    error the caller needs to handle specially."""
    now = now if now is not None else time.time()
    with _lock:
        conn = _connect()
        try:
            same_issue = conn.execute(
                """SELECT 1 FROM identity_anchors
                   WHERE anchor_type = ? AND normalized_value = ? AND issue_id = ? AND status = 'active'""",
                (anchor_type, normalized_value, issue_id),
            ).fetchone()
            if same_issue is not None:
                return None  # already recorded for this issue, nothing new to insert
            if exclusive:
                held_elsewhere = conn.execute(
                    """SELECT 1 FROM identity_anchors
                       WHERE anchor_type = ? AND normalized_value = ? AND exclusive = 1 AND status = 'active'""",
                    (anchor_type, normalized_value),
                ).fetchone()
                if held_elsewhere is not None:
                    return None
            cur = conn.execute(
                """INSERT INTO identity_anchors
                   (anchor_type, normalized_value, anchor_strength, exclusive, issue_id, status,
                    first_seen_ts, last_seen_ts, created_by, reason_json)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                (anchor_type, normalized_value, anchor_strength, int(exclusive), issue_id, now, now, created_by, reason_json),
            )
            return cur.lastrowid
        finally:
            conn.close()


def upsert_source_session(*, id: str, source_container_id: str, session_sequence: int,
                           started_ts: float, ended_ts: Optional[float], boundary_reason: str) -> None:
    """Idempotent, same pattern as upsert_source_container - re-running the
    backfill just refreshes ended_ts/boundary_reason for an already-known
    session rather than raising on the UNIQUE constraint."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO source_sessions (id, source_container_id, session_sequence, started_ts, ended_ts, boundary_reason)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_container_id, session_sequence)
                   DO UPDATE SET ended_ts = excluded.ended_ts, boundary_reason = excluded.boundary_reason""",
                (id, source_container_id, session_sequence, started_ts, ended_ts, boundary_reason),
            )
        finally:
            conn.close()


def list_source_sessions(source_container_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM source_sessions WHERE source_container_id = ? ORDER BY session_sequence",
                (source_container_id,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def list_identity_anchors_for_issues(issue_ids: list, status: str = "active") -> dict:
    """Batched sibling of list_identity_anchors - one query for N issues
    instead of N queries, same shape as list_parties_for_issues/list_
    evidence_for_issues elsewhere in this file. Returns {issue_id: [rows]},
    with every requested issue_id present (empty list if it has none)."""
    result = {iid: [] for iid in issue_ids}
    if not issue_ids:
        return result
    placeholders = ",".join("?" for _ in issue_ids)
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM identity_anchors WHERE status = ? AND issue_id IN ({placeholders})",
                (status, *issue_ids),
            ).fetchall()
        finally:
            conn.close()
    for r in rows:
        result[r["issue_id"]].append(dict(r))
    return result


def list_identity_anchors(issue_id: Optional[str] = None, status: str = "active") -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            if issue_id is not None:
                rows = conn.execute(
                    "SELECT * FROM identity_anchors WHERE issue_id = ? AND status = ?", (issue_id, status)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM identity_anchors WHERE status = ?", (status,)).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def upsert_work_object_relationship(*, a_id: str, b_id: str, relationship_type: str,
                                     match_count: int, matched_signals: list,
                                     now: Optional[float] = None) -> int:
    """Task #184 Phase D (2026-08-05). Order-normalizes (a_id, b_id) into
    (from_id, to_id) with from_id < to_id lexicographically, so the SAME
    real pair - detected from either direction, on any future pass - is
    always the same row, never a duplicate depending on which side a
    caller happened to iterate from first.

    Never silently overwrites an already-resolved decision: if a row for
    this exact pair already exists with relationship_type 'confirmed' or
    'rejected', this is a no-op that returns the EXISTING row's id
    unchanged - a fresh candidate/bridge detection on a later pass must
    never re-litigate a real human/curator judgment already recorded
    here. Otherwise upserts match_count/matched_signals_json/
    relationship_type (a later pass can genuinely find MORE matched
    points than an earlier one, as more content/extraction accumulates)."""
    now = now if now is not None else time.time()
    from_id, to_id = (a_id, b_id) if a_id < b_id else (b_id, a_id)
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT id, relationship_type FROM work_object_relationships WHERE from_id = ? AND to_id = ?",
                (from_id, to_id),
            ).fetchone()
            if existing and existing["relationship_type"] in ("confirmed", "rejected"):
                return existing["id"]
            cur = conn.execute(
                """INSERT INTO work_object_relationships
                   (from_id, to_id, relationship_type, match_count, matched_signals_json, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(from_id, to_id) DO UPDATE SET
                       relationship_type = excluded.relationship_type,
                       match_count = excluded.match_count,
                       matched_signals_json = excluded.matched_signals_json""",
                (from_id, to_id, relationship_type, match_count, json.dumps(matched_signals), now),
            )
            return cur.lastrowid if cur.lastrowid else existing["id"]
        finally:
            conn.close()


def get_work_object_relationship_by_id(relationship_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM work_object_relationships WHERE id = ?", (relationship_id,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def get_work_object_relationship(a_id: str, b_id: str) -> Optional[dict]:
    """Order-normalized read counterpart to upsert_work_object_relationship -
    the "have I already decided this pair" check Phase C/H need before
    treating a pair as fresh."""
    from_id, to_id = (a_id, b_id) if a_id < b_id else (b_id, a_id)
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM work_object_relationships WHERE from_id = ? AND to_id = ?",
                (from_id, to_id),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_pending_work_object_relationships(limit: int = 1000) -> list[dict]:
    """Curator's Phase F review queue - 'candidate'/'bridge' rows only,
    ranked by match_count descending (the more matched data points, the
    higher-priority the real content review) - the same "review the
    highest-match_count candidates first" ordering Marc asked for."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT * FROM work_object_relationships
                   WHERE relationship_type IN ('candidate','bridge')
                   ORDER BY match_count DESC, created_ts ASC LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def resolve_work_object_relationship(relationship_id: int, status: str, *, now: Optional[float] = None) -> None:
    """status must be 'confirmed' or 'rejected' - curator's real judgment
    (Phase F), never a mechanical re-classification. A 'genuinely unsure'
    verdict is NOT resolved here at all - the row is simply left as
    'candidate'/'bridge' for curator's own next pass, per Marc's explicit
    correction that grouping ambiguity is never surfaced as a decision
    for him to make."""
    if status not in ("confirmed", "rejected"):
        raise ValueError(f"resolve_work_object_relationship: invalid status {status!r}")
    now = now if now is not None else time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE work_object_relationships SET relationship_type = ?, resolved_ts = ? WHERE id = ?",
                (status, now, relationship_id),
            )
        finally:
            conn.close()


def list_work_object_relationships_for(work_object_id: str, relationship_types: Optional[tuple] = None) -> list[dict]:
    """Every relationship (either direction) touching one work_object -
    Phase H's per-item incremental check ('what does this group already
    relate to, before matching it against the whole corpus again') and
    Phase F's bridge-sibling lookup (every other pending row sharing the
    same from_id, so a bridge's several candidate rows are reviewed
    together, never one at a time)."""
    with _lock:
        conn = _connect()
        try:
            if relationship_types:
                placeholders = ",".join("?" for _ in relationship_types)
                rows = conn.execute(
                    f"""SELECT * FROM work_object_relationships
                        WHERE (from_id = ? OR to_id = ?) AND relationship_type IN ({placeholders})""",
                    (work_object_id, work_object_id, *relationship_types),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM work_object_relationships WHERE from_id = ? OR to_id = ?",
                    (work_object_id, work_object_id),
                ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def create_capability_suggestion(*, origin: str, observation: str, suggestion: str,
                                  rationale: Optional[str] = None) -> int:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO capability_suggestions (origin, observation, suggestion, rationale, created_ts)
                   VALUES (?, ?, ?, ?, ?)""",
                (origin, observation, suggestion, rationale, time.time()),
            )
            return cur.lastrowid
        finally:
            conn.close()


def list_capability_suggestions(status: str = "pending") -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM capability_suggestions WHERE status = ? ORDER BY created_ts DESC", (status,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_capability_suggestion(id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM capability_suggestions WHERE id = ?", (id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def resolve_capability_suggestion(id: int, status: str, *, resolution_note: Optional[str] = None) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE capability_suggestions SET status = ?, resolved_ts = ?, resolution_note = ? WHERE id = ?",
                (status, time.time(), resolution_note, id),
            )
        finally:
            conn.close()


# --- lessons (Total Recall) -------------------------------------------------
# Pure storage - the situation_key construction, trust-score arithmetic and
# write/read-time validation all live in workgraph_lessons.py. This module
# only persists rows.

def upsert_lesson(*, situation_key: str, outcome: str, statement: str, source_issue_id: str,
                   default_trust: float, bump: float, ceiling: float) -> dict:
    """Insert a new lesson row for (situation_key, outcome), or bump
    trust_score/hit_count on the existing one for a repeat of the same
    outcome. The read-then-write happens in one connection/lock, same shape
    as create_project_suggestion's dedupe-check-then-insert - a race between
    two near-simultaneous writers for the exact same situation_key is a rare,
    low-stakes edge case (worst case: one trust bump lost), not something
    this personal-scale tool needs stronger guarantees against."""
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT * FROM lessons WHERE situation_key = ? AND outcome = ?",
                (situation_key, outcome),
            ).fetchone()
            now = time.time()
            if existing:
                new_trust = round(min(ceiling, existing["trust_score"] + bump), 6)
                conn.execute(
                    """UPDATE lessons SET trust_score = ?, hit_count = hit_count + 1,
                       statement = ?, last_applied_ts = ? WHERE id = ?""",
                    (new_trust, statement, now, existing["id"]),
                )
                row_id = existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO lessons (situation_key, outcome, statement, source_issue_id,
                       trust_score, hit_count, created_ts, last_applied_ts)
                       VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                    (situation_key, outcome, statement, source_issue_id, default_trust, now, now),
                )
                row_id = cur.lastrowid
            row = conn.execute("SELECT * FROM lessons WHERE id = ?", (row_id,)).fetchone()
        finally:
            conn.close()
    return dict(row)


def penalize_lesson(situation_key: str, outcome: str, *, penalty: float, floor: float) -> None:
    """No-ops if no lesson exists yet for this (situation_key, outcome) -
    used when the OPPOSITE outcome just landed for the same situation, i.e.
    this lesson was just contradicted."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE lessons SET trust_score = ROUND(MAX(?, trust_score - ?), 6) "
                "WHERE situation_key = ? AND outcome = ?",
                (floor, penalty, situation_key, outcome),
            )
        finally:
            conn.close()


def get_lesson(id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM lessons WHERE id = ?", (id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def get_lesson_by_situation(situation_key: str, outcome: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM lessons WHERE situation_key = ? AND outcome = ?", (situation_key, outcome)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_known_categories() -> list[str]:
    """Distinct, non-empty issue categories actually in use - read live rather
    than hardcoded, so it can't drift from what curator/classify actually
    assign. Used by workgraph_socrates to spot a category mentioned in a
    free-text question."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT category FROM issues WHERE category IS NOT NULL AND category <> ''"
            ).fetchall()
        finally:
            conn.close()
    return [r["category"] for r in rows]


def list_known_companies() -> list[str]:
    """Distinct external-party company names on record. Same live-read
    rationale as list_known_categories - used by workgraph_socrates to spot a
    supplier mentioned in a free-text question."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT company FROM parties WHERE affiliation = 'external' AND company IS NOT NULL AND company <> ''"
            ).fetchall()
        finally:
            conn.close()
    return [r["company"] for r in rows]


def append_socrates_log(*, asked_ts: float, asker: Optional[str], question: str, signature: str,
                         tier: str, band: str, contributed: bool, outcome: str) -> int:
    """One row per TIER consulted for one question (a single answer() call
    logs several rows, one per tier it checked) - see the table comment in
    init_workgraph for why."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO socrates_retrieval_log
                   (asked_ts, asker, question, signature, tier, band, contributed, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (asked_ts, asker, question, signature, tier, band, 1 if contributed else 0, outcome),
            )
            return cur.lastrowid
        finally:
            conn.close()


def count_socrates_log_before(cutoff_ts: float) -> int:
    """Retention support (added 2026-07-29) - report-only count, used by
    retention.py to say what WOULD be deleted before enforcement is enabled."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM socrates_retrieval_log WHERE asked_ts < ?", (cutoff_ts,)).fetchone()
            return row["n"]
        finally:
            conn.close()


def delete_old_socrates_log(cutoff_ts: float) -> int:
    """Retention support (added 2026-07-29, see retention.py) - this is
    diagnostic/tuning data (what Socrates retrieved and whether it helped),
    not a business record; socrates_source_outcomes aggregates recent history,
    not all-time, so pruning old rows doesn't change what that projection
    means. Returns rows deleted."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM socrates_retrieval_log WHERE asked_ts < ?", (cutoff_ts,))
            return cur.rowcount
        finally:
            conn.close()


def get_teams_messages_from_actor_since(actor: str, since_ts: float) -> list[dict]:
    """Personal Response Learning Phase 3 (task #50) support: raw_items rows
    already ingested as Teams messages (source='teams_chat') whose from_actor
    matches `actor` case-insensitively, newer than since_ts. No new
    ingestion - Teams messages are already captured both directions by the
    existing pipeline (ingest/normalize.py's _process_teams_chat); this just
    identifies which of them are Marc's own.

    Known limitation: from_actor can be either a display name or an email
    depending on what Graph returned for that specific message - an exact,
    case-insensitive match against config.manager.id (his display name)
    catches the common case but would miss a message where Graph reported
    his email address instead."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT id, occurred_ts, subject, body_preview FROM raw_items
                   WHERE source = 'teams_chat' AND occurred_ts > ?
                   AND LOWER(from_actor) = LOWER(?)
                   ORDER BY occurred_ts ASC""",
                (since_ts, actor),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_socrates_log_since(since_ts: float) -> list[dict]:
    """Distinct (asked_ts, asker, question) rows strictly after since_ts - one
    row PER TIER is logged per real question (see append_socrates_log's own
    comment), so this collapses that back to one row per actual question for
    personal_patterns.py's mining pass, which only cares about the question
    text/timing, not which tier answered it."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT DISTINCT asked_ts, asker, question FROM socrates_retrieval_log
                   WHERE asked_ts > ? ORDER BY asked_ts ASC""",
                (since_ts,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# --- response_patterns (Personal Response Learning, task #45) ---------------

def upsert_response_pattern(source_surface: str, pattern_key: str, example_text: str, ts: float) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO response_patterns
                   (source_surface, pattern_key, example_text, hit_count, first_seen_ts, last_seen_ts)
                   VALUES (?, ?, ?, 1, ?, ?)
                   ON CONFLICT(source_surface, pattern_key) DO UPDATE SET
                     hit_count = hit_count + 1,
                     example_text = excluded.example_text,
                     last_seen_ts = excluded.last_seen_ts""",
                (source_surface, pattern_key, example_text, ts, ts),
            )
        finally:
            conn.close()


def list_response_patterns(source_surface: Optional[str] = None) -> list[dict]:
    """Fixed 2026-07-30 (adversarial review round #2): `ORDER BY hit_count
    DESC` alone has no tie-break, so two patterns tied on hit_count (very
    plausible at real scale) could come back in either order across runs -
    the same non-determinism already fixed 3x elsewhere this session
    (workgraph_projects._project_name_for, workgraph_suppliers.
    attach_supplier_precedent, workgraph_lessons._first_external_company).
    personal_patterns.citation_for_text picks the FIRST match in this list,
    so an unstable tie could flip which pattern gets cited as precedent.
    first_seen_ts ascending is the same stable tie-break used everywhere
    else in this codebase."""
    with _lock:
        conn = _connect()
        try:
            if source_surface:
                rows = conn.execute(
                    "SELECT * FROM response_patterns WHERE source_surface = ? "
                    "ORDER BY hit_count DESC, first_seen_ts ASC",
                    (source_surface,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM response_patterns ORDER BY hit_count DESC, first_seen_ts ASC"
                ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def clear_response_patterns() -> int:
    """Settings' "Forget what's been learned" button - clears every surface's
    accumulated patterns at once (v1 has no per-surface forget; the whole
    table is small and this is meant to be a full, legible reset)."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM response_patterns")
            return cur.rowcount
        finally:
            conn.close()


def socrates_source_outcomes(signature: str) -> list[dict]:
    """Per-tier contribution tally for a question signature - the learned-
    routing projection (mirrors Theo's retrieval-log.sourceOutcomes). Ranked
    by contribution rate so a caller can check the tier that has actually
    answered this shape of question before, first."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT tier,
                          COUNT(*) AS consulted,
                          SUM(contributed) AS contributed
                   FROM socrates_retrieval_log
                   WHERE signature = ?
                   GROUP BY tier""",
                (signature,),
            ).fetchall()
        finally:
            conn.close()
    return [
        {"tier": r["tier"], "consulted": r["consulted"], "contributed": r["contributed"] or 0}
        for r in rows
    ]


def get_signal_treatment(signal_type: str, default: str) -> str:
    """Live override lookup for a known-automated-signal type's TREATMENT
    (noise|fyi|actionable|closure). The signal type's PATTERN is code
    (workgraph_signals.py); its treatment is data, correctable here without a
    code change - e.g. Marc telling a worker "mark X as noise" persists as a
    call to set_signal_treatment below, no deployment needed. Falls back to
    the caller's hardcoded default when nothing has ever been set."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT treatment FROM signal_treatment_overrides WHERE signal_type = ?", (signal_type,)
            ).fetchone()
        finally:
            conn.close()
    return row["treatment"] if row else default


def set_signal_treatment(signal_type: str, treatment: str, *, reason: Optional[str] = None,
                          set_by: Optional[str] = None) -> None:
    """Persist a correction to a signal type's treatment - sticks until
    changed again, never re-guessed back to the code default afterward."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO signal_treatment_overrides (signal_type, treatment, reason, set_ts, set_by)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(signal_type) DO UPDATE SET
                     treatment = excluded.treatment, reason = excluded.reason,
                     set_ts = excluded.set_ts, set_by = excluded.set_by""",
                (signal_type, treatment, reason, now, set_by),
            )
        finally:
            conn.close()


def list_signal_treatments() -> list[dict]:
    """All live overrides currently in effect (an audit/settings view)."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM signal_treatment_overrides ORDER BY signal_type").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def find_active_signal_overrides_for_issue(issue_id: str) -> list[dict]:
    """Enhancement idea panel #4: show when this issue's classification
    reflects Marc's own override (signal_treatment_overrides), not the
    code default - real transparency into a correction that already
    existed but was previously only visible in Settings' own audit view
    (list_signal_treatments), never on the issue it actually affected.
    Empty list when none of this issue's raw_items matched a known
    signal_type, or none of those types has a live override."""
    items = get_raw_items_for_issue(issue_id)
    signal_types = {i["signal_type"] for i in items if i.get("signal_type")}
    if not signal_types:
        return []
    overrides = {o["signal_type"]: o for o in list_signal_treatments()}
    return [overrides[st] for st in signal_types if st in overrides]


# --- ownership_rules --------------------------------------------------------

def create_ownership_rule(*, match_field: str, match_value: str, default_owner: str, created_reason: Optional[str] = None) -> int:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO ownership_rules (match_field, match_value, default_owner, created_ts, created_reason)
                   VALUES (?, ?, ?, ?, ?)""",
                (match_field, match_value, default_owner, time.time(), created_reason),
            )
            return cur.lastrowid
        finally:
            conn.close()


def list_ownership_rules() -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM ownership_rules ORDER BY created_ts DESC").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def delete_ownership_rule(id: int) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM ownership_rules WHERE id = ?", (id,))
        finally:
            conn.close()


def find_owner_for(*, category: Optional[str] = None, party_company: Optional[str] = None, sender_domain: Optional[str] = None, topic: Optional[str] = None) -> Optional[str]:
    """Deterministic rule lookup - the FIRST matching rule wins (most
    recently created rules are checked first, so a newer correction takes
    precedence over an older, more general one). Returns None (genuinely
    unknown) rather than guessing 'marc' when nothing matches."""
    candidates = [("category", category), ("party_company", party_company),
                  ("sender_domain", sender_domain), ("topic", topic)]
    with _lock:
        conn = _connect()
        try:
            for field, value in candidates:
                if not value:
                    continue
                row = conn.execute(
                    "SELECT default_owner FROM ownership_rules WHERE match_field = ? AND match_value = ? ORDER BY created_ts DESC LIMIT 1",
                    (field, value),
                ).fetchone()
                if row:
                    return row["default_owner"]
        finally:
            conn.close()
    return None


# --- audit_log ---------------------------------------------------------

def add_audit_entry(*, entity_type: str, entity_id: str, field: str, old_value: Optional[str], new_value: Optional[str], reason: Optional[str] = None) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO audit_log (entity_type, entity_id, field, old_value, new_value, changed_ts, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (entity_type, entity_id, field, old_value, new_value, time.time(), reason),
            )
        finally:
            conn.close()


def list_audit_log(entity_type: str, entity_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE entity_type = ? AND entity_id = ? ORDER BY changed_ts ASC",
                (entity_type, entity_id),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# --- raw_item_extractions ---------------------------------------------------
# Real-LLM-judgment layer, additional to classify_raw_item()'s deterministic
# fields above. Computed ONCE per raw_item and stored permanently - curator's
# synthesis routine never re-analyzes a raw_item it already extracted (see
# SYNTHESIS_ROUTINE.md). create_extraction upserts (ON CONFLICT DO UPDATE)
# only so a worker can correct a bad extraction; the "never recomputed" rule
# is a routine-level discipline, not a DB constraint.

def canonical_json_hash(extracted_json: str) -> str:
    """Deterministic hash of the extraction's REAL content, not its byte
    representation - re-parses and re-serializes with sorted keys and no
    whitespace variance first, so two calls that produced logically
    identical JSON (different key order, different indent) still hash
    identically. Used both to detect a real correction (content_hash
    changing) and as the guard against re-reconciling an unchanged
    extraction (materialized_hash == content_hash)."""
    try:
        parsed = json.loads(extracted_json)
    except (TypeError, ValueError):
        parsed = extracted_json
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_extraction(raw_item_id: int, extracted_json: str) -> None:
    content_hash = canonical_json_hash(extracted_json)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO raw_item_extractions (raw_item_id, extracted_json, extracted_ts, content_hash)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(raw_item_id) DO UPDATE SET
                       extracted_json = excluded.extracted_json, extracted_ts = excluded.extracted_ts,
                       content_hash = excluded.content_hash""",
                (raw_item_id, extracted_json, time.time(), content_hash),
            )
        finally:
            conn.close()


def set_extraction_content_hash(raw_item_id: int, content_hash: str) -> None:
    """Backfill-only setter (2026-08-04) - create_extraction computes
    content_hash automatically for every NEW/re-extracted row; this is
    for workgraph_claims_backfill's one-time pass over extraction rows
    written before this feature existed."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE raw_item_extractions SET content_hash = ? WHERE raw_item_id = ?",
                (content_hash, raw_item_id),
            )
        finally:
            conn.close()


def mark_extraction_materialized(raw_item_id: int, content_hash: Optional[str]) -> None:
    """Records that the claims table has been reconciled against THIS
    extraction content - the counterpart materialize_claims_for_raw_item
    checks (materialized_hash == content_hash) before deciding a
    correction needs reconciling at all."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE raw_item_extractions SET materialized_hash = ? WHERE raw_item_id = ?",
                (content_hash, raw_item_id),
            )
        finally:
            conn.close()


def get_extraction(raw_item_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM raw_item_extractions WHERE raw_item_id = ?", (raw_item_id,)
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    d = dict(row)
    try:
        d["extracted_json"] = json.loads(d["extracted_json"])
    except Exception:
        d["extracted_json"] = {}
    return d


def list_extractions_for_issue(issue_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT rie.* FROM raw_item_extractions rie
                   JOIN raw_items ri ON ri.id = rie.raw_item_id
                   WHERE ri.issue_id = ?
                   ORDER BY rie.extracted_ts ASC""",
                (issue_id,),
            ).fetchall()
        finally:
            conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["extracted_json"] = json.loads(d["extracted_json"])
        except Exception:
            d["extracted_json"] = {}
        out.append(d)
    return out


def list_extractions_for_issues(issue_ids: list[str]) -> dict[str, list[dict]]:
    """Batched form of list_extractions_for_issue - task #64 (Deadline Radar)
    needs this across every open issue at once, not one issue at a time -
    same N+1 fix already applied to list_evidence_for_issues/
    get_raw_items_by_ids."""
    if not issue_ids:
        return {}
    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join("?" * len(issue_ids))
            rows = conn.execute(
                f"""SELECT rie.*, ri.issue_id AS issue_id FROM raw_item_extractions rie
                    JOIN raw_items ri ON ri.id = rie.raw_item_id
                    WHERE ri.issue_id IN ({placeholders})
                    ORDER BY rie.extracted_ts ASC""",
                issue_ids,
            ).fetchall()
        finally:
            conn.close()
    out: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        try:
            d["extracted_json"] = json.loads(d["extracted_json"])
        except Exception:
            d["extracted_json"] = {}
        out.setdefault(d["issue_id"], []).append(d)
    return out


def list_all_open_claims() -> list[dict]:
    """Every OPEN claim across the whole DB, oldest first - the backfill
    driver for workgraph_claims_backfill.backfill_canonical_keys_and_merge_
    duplicates (2026-08-04), not a hot-path query. Same "one-time backfill
    scan, N+1-tolerant" shape as list_raw_item_ids_with_extractions below."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM claims WHERE status = 'open' ORDER BY first_seen_ts ASC"
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def reconcile_extraction_claims(*, issue_id: str, raw_item_id: int, to_insert: list[dict],
                                 to_supersede: list[int], new_materialized_hash: Optional[str]) -> list[int]:
    """Atomically applies a corrected-extraction reconciliation (2026-08-04,
    architecture-review follow-up P1): inserts every new claim spec in
    `to_insert` (each a dict with claim_type/text/owner/date_kind/
    canonical_key), marks every id in `to_supersede` as status='superseded'
    (no superseded_by - no reliable 1:1 link between an old removed claim
    and a specific new one, so this deliberately doesn't guess a pairing),
    logs a real claim_event for each change, bumps claims_revision ONCE for
    the whole reconciliation (not once per claim), and updates the
    extraction's materialized_hash - ALL in one transaction. A prior
    reconciliation attempt that crashed mid-way must never leave the claims
    table half-corrected while materialized_hash still reads as stale (which
    would make a retry re-apply the same half-done changes again) - see the
    workgraph_claims_backfill test that verifies a forced failure rolls back
    completely, not partially. Returns the new claim ids inserted."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            inserted_ids = []
            for spec in to_insert:
                cur = conn.execute(
                    """INSERT INTO claims
                       (issue_id, raw_item_id, claim_type, text, author, author_basis,
                        owner, date_kind, status, first_seen_ts, last_seen_ts, canonical_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                    (issue_id, raw_item_id, spec["claim_type"], spec["text"], spec["author"],
                     spec["author_basis"], spec.get("owner"), spec.get("date_kind"), now, now,
                     spec.get("canonical_key")),
                )
                claim_id = cur.lastrowid
                inserted_ids.append(claim_id)
                conn.execute(
                    """INSERT INTO claim_events (claim_id, event_type, ts, actor, note, raw_item_id)
                       VALUES (?, 'create', ?, 'curator', ?, ?)""",
                    (claim_id, now, "added by a corrected extraction", raw_item_id),
                )
            for claim_id in to_supersede:
                conn.execute(
                    "UPDATE claims SET status = 'superseded', last_seen_ts = ? WHERE id = ?",
                    (now, claim_id),
                )
                conn.execute(
                    """INSERT INTO claim_events (claim_id, event_type, ts, actor, note, raw_item_id)
                       VALUES (?, 'dismiss', ?, 'curator', ?, ?)""",
                    (claim_id, now, "removed by a corrected extraction, not completed real-world work", raw_item_id),
                )
            if to_insert or to_supersede:
                # Corrected pipeline (2026-08-05): work_objects directly, not
                # the `issues` view - issue_id is now routinely a cluster
                # (raw_items attach to clusters first, Phase B), and the
                # view's UPDATE trigger silently matches zero rows for one,
                # so a cluster's claims_revision never advanced at all.
                conn.execute(
                    "UPDATE work_objects SET claims_revision = claims_revision + 1 WHERE id = ?", (issue_id,)
                )
            conn.execute(
                "UPDATE raw_item_extractions SET materialized_hash = ? WHERE raw_item_id = ?",
                (new_materialized_hash, raw_item_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    return inserted_ids


def list_raw_item_ids_with_extractions() -> list[int]:
    """Every raw_item that has a real extraction, oldest first - the backfill
    driver for workgraph_claims_backfill.backfill_claims (Section 9.9 step 3),
    not a hot-path query."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT raw_item_id FROM raw_item_extractions ORDER BY raw_item_id ASC"
            ).fetchall()
        finally:
            conn.close()
    return [r["raw_item_id"] for r in rows]


def list_all_raw_item_ids() -> list[int]:
    """Every raw_item, extracted or not - the FTS backfill (Section 9.6)
    indexes full text regardless of whether extraction has run, since
    evidence search is useful before/without extraction too."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT id FROM raw_items ORDER BY id ASC").fetchall()
        finally:
            conn.close()
    return [r["id"] for r in rows]


# --- claims (design doc Section 9 / Phase 3) --------------------------
# Materialized, typed, deduped, actor-attributed rows over the ask/decision/
# commitment/date fields raw_item_extractions already carries. See
# workgraph_claims.py for the materialization logic that calls these -
# this layer is pure persistence, same split as every other table here.

def has_claims_for_raw_item(raw_item_id: int) -> bool:
    """Idempotency guard - materialize_claims_for_raw_item is safe to call
    more than once per raw_item (mirrors the never-re-extract discipline
    raw_item_extractions itself already relies on)."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM claims WHERE raw_item_id = ? LIMIT 1", (raw_item_id,)
            ).fetchone()
        finally:
            conn.close()
    return row is not None


def find_open_claim_by_text(issue_id: str, claim_type: str, text: str) -> Optional[dict]:
    """Exact-text match among this issue's OPEN claims of the same type -
    used both for repeat_signals-driven ask dedup (Section 9.3) and as a
    same-raw_item idempotency fallback. Exact match only; no fuzzy text
    comparison anywhere in this module, deliberately - the real dedup
    signal is curator's own repeat_signals judgment, not a text-similarity
    guess."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT * FROM claims WHERE issue_id = ? AND claim_type = ?
                   AND status = 'open' AND text = ? ORDER BY id DESC LIMIT 1""",
                (issue_id, claim_type, text),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def find_open_claim_by_canonical_key(issue_id: str, claim_type: str, canonical_key: str) -> Optional[dict]:
    """Fallback dedup for canonical claim deduplication (2026-08-04) - the
    same "exact match among this issue's OPEN claims of the same type"
    shape as find_open_claim_by_text above, just keyed on canonical_key
    (structured-reference-preferred, conservative-normalization-fallback -
    see workgraph_claims.canonical_key_for_claim) instead of raw text.
    Called only when the byte-exact text match already failed - see
    materialize_claims_for_raw_item's own docstring for why both checks
    run, in that order."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT * FROM claims WHERE issue_id = ? AND claim_type = ?
                   AND status = 'open' AND canonical_key = ? ORDER BY id DESC LIMIT 1""",
                (issue_id, claim_type, canonical_key),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def set_claim_canonical_key(claim_id: int, canonical_key: str) -> None:
    """Backfill-only setter (2026-08-04) - insert_claim sets canonical_key
    at creation time for every NEW claim; this updates an EXISTING row
    for workgraph_claims_backfill.backfill_canonical_keys_and_merge_
    duplicates, which computes canonical_key for claims that predate this
    feature. Deliberately not a general-purpose claim editor - no other
    caller should ever change a claim's canonical_key after the fact."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE claims SET canonical_key = ? WHERE id = ?", (canonical_key, claim_id))
        finally:
            conn.close()


def insert_claim(
    *, issue_id: str, raw_item_id: int, claim_type: str, text: str,
    author: str, author_basis: str, owner: Optional[str] = None,
    date_kind: Optional[str] = None, ts: Optional[float] = None,
    canonical_key: Optional[str] = None,
) -> int:
    now = ts if ts is not None else time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """INSERT INTO claims
                   (issue_id, raw_item_id, claim_type, text, author, author_basis,
                    owner, date_kind, status, first_seen_ts, last_seen_ts, canonical_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                (issue_id, raw_item_id, claim_type, text, author, author_basis,
                 owner, date_kind, now, now, canonical_key),
            )
            claim_id = cur.lastrowid
            # Corrected pipeline (2026-08-05): work_objects, not the `issues`
            # view - issue_id may be a cluster (see materialize_claims_for_
            # raw_item's own fix, same day, same root cause).
            conn.execute(
                "UPDATE work_objects SET claims_revision = claims_revision + 1 WHERE id = ?",
                (issue_id,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    return claim_id


def touch_claim(
    claim_id: int, *, ts: Optional[float] = None,
    escalated: Optional[bool] = None, escalation_note: Optional[str] = None,
) -> None:
    """Updates an existing OPEN claim's last_seen_ts (and escalation state,
    if curator's repeat_signals flagged one) instead of inserting a second
    row for the same restated ask - Section 9.3's dedup rule. Bumps
    claims_revision too: a touch is still new information worth a
    re-synthesis check, even though no new row was created."""
    now = ts if ts is not None else time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if escalated is None:
                conn.execute(
                    "UPDATE claims SET last_seen_ts = ? WHERE id = ?", (now, claim_id)
                )
            else:
                conn.execute(
                    """UPDATE claims SET last_seen_ts = ?, escalated = ?, escalation_note = ?
                       WHERE id = ?""",
                    (now, int(escalated), escalation_note, claim_id),
                )
            row = conn.execute("SELECT issue_id FROM claims WHERE id = ?", (claim_id,)).fetchone()
            if row:
                # Corrected pipeline (2026-08-05): work_objects, not `issues`.
                conn.execute(
                    "UPDATE work_objects SET claims_revision = claims_revision + 1 WHERE id = ?",
                    (row["issue_id"],),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def reassign_claim(claim_id: int, new_issue_id: str) -> None:
    """Corrected pipeline Phase D (2026-08-05): the real primitive behind
    curator's issue-extraction step (workgraph_projects.
    extract_issue_from_project) - a claim's issue_id is a plain FK, not
    tied through the `issues` view, so moving it to a newly-created real
    issue is a direct UPDATE, same as insert_claim/touch_claim/update_
    claim_status already write it. Bumps the NEW owner's claims_revision
    (not the old owner's - the old owner, a cluster or issue, may still
    hold other claims of its own that didn't move, and its own revision
    already reflects those correctly)."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE claims SET issue_id = ? WHERE id = ?", (new_issue_id, claim_id))
            conn.execute(
                "UPDATE work_objects SET claims_revision = claims_revision + 1 WHERE id = ?",
                (new_issue_id,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def update_claim_status(claim_id: int, status: str, *, actor: str, superseded_by: Optional[int] = None) -> None:
    """The one place claims.status ever changes after creation (design doc
    Section 12.3) - closes a real gap confirmed by reading the code: nothing
    before this ever moved a claim out of 'open'. Bumps claims_revision same
    as insert_claim/touch_claim - a status change is real new information a
    synthesis re-check should see."""
    if status not in ("open", "done", "superseded", "dismissed"):
        raise ValueError(f"invalid claim status: {status!r}")
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE claims SET status = ?, superseded_by = ?, last_seen_ts = ? WHERE id = ?",
                (status, superseded_by, now, claim_id),
            )
            row = conn.execute("SELECT issue_id FROM claims WHERE id = ?", (claim_id,)).fetchone()
            if row:
                # Corrected pipeline (2026-08-05): work_objects, not `issues`.
                conn.execute(
                    "UPDATE work_objects SET claims_revision = claims_revision + 1 WHERE id = ?",
                    (row["issue_id"],),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def log_claim_event(claim_id: int, event_type: str, *, actor: str, note: Optional[str] = None,
                     ts: Optional[float] = None, raw_item_id: Optional[int] = None) -> int:
    """Design doc Section 12.3's right-sized event log - 5 real event types
    (create/escalate/acknowledge/complete/dismiss), each with a real,
    deterministic producer in workgraph_claims.py, not the Blueprint's full
    14-type taxonomy (no current producer for the other 9 - a named,
    deliberate gap, not silently dropped).

    raw_item_id (2026-08-04, provenance fix): claims.raw_item_id is fixed
    at creation and never updated, so a claim touched by several later
    repeat messages had no record of which raw_item caused each individual
    touch. Every real call site now passes the raw_item that triggered
    this specific event - optional only for the handful of callers (tests,
    mostly) with no real raw_item context."""
    now = ts if ts is not None else time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO claim_events (claim_id, event_type, ts, actor, note, raw_item_id) VALUES (?, ?, ?, ?, ?, ?)",
                (claim_id, event_type, now, actor, note, raw_item_id),
            )
            return cur.lastrowid
        finally:
            conn.close()


def list_claim_events_for_claim(claim_id: int) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM claim_events WHERE claim_id = ? ORDER BY ts ASC", (claim_id,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def create_claim_edge(from_claim_id: int, to_claim_id: int, edge_type: str, *, actor: str) -> int:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO claim_edges (from_claim_id, to_claim_id, edge_type, created_ts, created_by)
                   VALUES (?, ?, ?, ?, ?)""",
                (from_claim_id, to_claim_id, edge_type, time.time(), actor),
            )
            return cur.lastrowid
        finally:
            conn.close()


def list_claim_edges_for_claim(claim_id: int) -> list[dict]:
    """Every edge touching this claim, either direction - same "detail panel
    shouldn't care which side it's on" reasoning as list_project_links_for_
    project."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM claim_edges WHERE from_claim_id = ? OR to_claim_id = ?",
                (claim_id, claim_id),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def create_identity_constraint(constraint_type: str, subject_a: str, subject_b: Optional[str],
                                reason: str, *, actor: str) -> int:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO identity_constraints
                   (constraint_type, subject_a, subject_b, reason, created_ts, created_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (constraint_type, subject_a, subject_b, reason, time.time(), actor),
            )
            return cur.lastrowid
        finally:
            conn.close()


def find_identity_constraint(constraint_type: str, subject_a: str,
                              subject_b: Optional[str] = None) -> Optional[dict]:
    """Checks both orderings of the pair (a,b)/(b,a) - same "a relationship
    has no inherent direction" reasoning as list_claim_edges_for_claim/
    list_project_links_for_project."""
    with _lock:
        conn = _connect()
        try:
            if subject_b is None:
                row = conn.execute(
                    "SELECT * FROM identity_constraints WHERE constraint_type = ? AND subject_a = ?",
                    (constraint_type, subject_a),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM identity_constraints WHERE constraint_type = ? AND
                       ((subject_a = ? AND subject_b = ?) OR (subject_a = ? AND subject_b = ?))""",
                    (constraint_type, subject_a, subject_b, subject_b, subject_a),
                ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_identity_constraints_for_subject(subject_id: str) -> list[dict]:
    """Every constraint touching this subject, either side, any type - the
    building block compute_work_object_signature (Section 12.7) uses for
    cannot_link_ids, since a work_object's own cannot_merge/cannot_link
    history isn't scoped to one other specific pair the way find_identity_
    constraint's lookup is."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM identity_constraints WHERE subject_a = ? OR subject_b = ?",
                (subject_id, subject_id),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_work_object_signature(work_object_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM work_object_signatures WHERE work_object_id = ?", (work_object_id,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def upsert_work_object_signature(work_object_id: str, *, definitive_ids_json: str, accepted_lineages_json: str,
                                  containers_json: str, external_orgs_json: str, participant_roles_json: str,
                                  active_period_start: Optional[float], active_period_end: Optional[float],
                                  positive_vocabulary_json: Optional[str], negative_vocabulary_json: Optional[str],
                                  cannot_link_ids_json: str, schema_version: int = 0) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO work_object_signatures
                   (work_object_id, definitive_ids, accepted_lineages, containers, external_orgs,
                    participant_roles, active_period_start, active_period_end, positive_vocabulary,
                    negative_vocabulary, cannot_link_ids, updated_ts, schema_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(work_object_id) DO UPDATE SET
                       definitive_ids = excluded.definitive_ids, accepted_lineages = excluded.accepted_lineages,
                       containers = excluded.containers, external_orgs = excluded.external_orgs,
                       participant_roles = excluded.participant_roles,
                       active_period_start = excluded.active_period_start,
                       active_period_end = excluded.active_period_end,
                       positive_vocabulary = excluded.positive_vocabulary,
                       negative_vocabulary = excluded.negative_vocabulary,
                       cannot_link_ids = excluded.cannot_link_ids, updated_ts = excluded.updated_ts,
                       schema_version = excluded.schema_version""",
                (work_object_id, definitive_ids_json, accepted_lineages_json, containers_json, external_orgs_json,
                 participant_roles_json, active_period_start, active_period_end, positive_vocabulary_json,
                 negative_vocabulary_json, cannot_link_ids_json, time.time(), schema_version),
            )
        finally:
            conn.close()


def invalidate_work_object_signature(work_object_id: str) -> None:
    """Best-effort cache invalidation - called from every real write site
    that changes what's IN a signature (see the table's own schema comment
    in init_workgraph). Never raises for an id with no cached row - that's
    the normal case for a work_object never scored yet."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM work_object_signatures WHERE work_object_id = ?", (work_object_id,))
        finally:
            conn.close()


def create_data_point_definition(
    *, id: str, name: str, description: Optional[str], point_type: str,
    deterministic_rule: Optional[str], discovered_from: Optional[str], status: str = "proposed",
) -> None:
    """PERSONALIZED_DATA_POINT_DISCOVERY.md section 4. Always starts 'proposed'
    unless the caller explicitly fast-tracks (task #217 - Marc's own already-
    proven procurement fields become initial 'confirmed' rows directly,
    the one legitimate bypass of the propose-then-confirm gate, since
    re-discovering something already validated across real use is pure
    cost with no signal)."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO data_point_definitions
                   (id, name, description, point_type, deterministic_rule, status,
                    trust_score, discovered_from, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?, 0.6, ?, ?)""",
                (id, name, description, point_type, deterministic_rule, status,
                 discovered_from, time.time()),
            )
        finally:
            conn.close()


def list_data_point_definitions(status: Optional[str] = None) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM data_point_definitions WHERE status = ? ORDER BY created_ts", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM data_point_definitions ORDER BY created_ts").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_data_point_definition(definition_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM data_point_definitions WHERE id = ?", (definition_id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def confirm_data_point_definition(definition_id: str, *, confirmed_by: str) -> None:
    """The one real trust gate (design doc section 2.4) - nothing in the
    extraction pipeline (task #215/#216) reads a 'proposed' row at all,
    only 'confirmed' ones. Never called automatically."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE data_point_definitions SET status = 'confirmed', confirmed_ts = ?, confirmed_by = ? WHERE id = ?",
                (time.time(), confirmed_by, definition_id),
            )
        finally:
            conn.close()


def reject_data_point_definition(definition_id: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE data_point_definitions SET status = 'rejected' WHERE id = ?", (definition_id,))
        finally:
            conn.close()


# --- contractpodai_requests (task #265) -------------------------------------

def upsert_contractpodai_request(fields: dict, *, raw_item_id: Optional[int], issue_id: Optional[str]) -> None:
    """Partial upsert, same discipline as upsert_party: COALESCE keeps
    whatever's already there when a later template doesn't carry a given
    field, only filling gaps or refreshing raw_item_id/issue_id/
    last_seen_ts - never overwrites a real value with a later NULL just
    because a different ContractPodAI template didn't mention that field."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO contractpodai_requests
                   (request_id, contractpod_url, sourcing_lead, functional_area, s2p_action,
                    supplier_name, priority, primary_assignee, additional_assignees, reviewer,
                    requester, agreement_title, raw_item_id, issue_id, first_seen_ts, last_seen_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET
                     contractpod_url = COALESCE(contractpodai_requests.contractpod_url, excluded.contractpod_url),
                     sourcing_lead = COALESCE(contractpodai_requests.sourcing_lead, excluded.sourcing_lead),
                     functional_area = COALESCE(contractpodai_requests.functional_area, excluded.functional_area),
                     s2p_action = COALESCE(contractpodai_requests.s2p_action, excluded.s2p_action),
                     supplier_name = COALESCE(contractpodai_requests.supplier_name, excluded.supplier_name),
                     priority = COALESCE(contractpodai_requests.priority, excluded.priority),
                     primary_assignee = COALESCE(contractpodai_requests.primary_assignee, excluded.primary_assignee),
                     additional_assignees = COALESCE(contractpodai_requests.additional_assignees, excluded.additional_assignees),
                     reviewer = COALESCE(contractpodai_requests.reviewer, excluded.reviewer),
                     requester = COALESCE(contractpodai_requests.requester, excluded.requester),
                     agreement_title = COALESCE(contractpodai_requests.agreement_title, excluded.agreement_title),
                     raw_item_id = excluded.raw_item_id,
                     issue_id = COALESCE(excluded.issue_id, contractpodai_requests.issue_id),
                     last_seen_ts = excluded.last_seen_ts""",
                (fields["request_id"], fields.get("contractpod_url"), fields.get("sourcing_lead"),
                 fields.get("functional_area"), fields.get("s2p_action"), fields.get("supplier_name"),
                 fields.get("priority"), fields.get("primary_assignee"), fields.get("additional_assignees"),
                 fields.get("reviewer"), fields.get("requester"), fields.get("agreement_title"),
                 raw_item_id, issue_id, now, now),
            )
        finally:
            conn.close()


def get_contractpodai_request(request_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM contractpodai_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_contractpodai_requests_for_issue(issue_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM contractpodai_requests WHERE issue_id = ? ORDER BY last_seen_ts DESC", (issue_id,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def upsert_ariba_requisition(fields: dict, *, raw_item_id: Optional[int], issue_id: Optional[str]) -> None:
    """Partial upsert, same COALESCE discipline as upsert_contractpodai_
    request/upsert_party - a later sighting of the same PR# (e.g. a
    reminder/escalation subject that only restates the PR# without the
    full descriptor) never blanks out a field an earlier sighting already
    populated."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO ariba_requisitions
                   (pr_number, requester, descriptor, amount, raw_item_id, issue_id,
                    first_seen_ts, last_seen_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pr_number) DO UPDATE SET
                     requester = COALESCE(ariba_requisitions.requester, excluded.requester),
                     descriptor = COALESCE(ariba_requisitions.descriptor, excluded.descriptor),
                     amount = COALESCE(ariba_requisitions.amount, excluded.amount),
                     raw_item_id = excluded.raw_item_id,
                     issue_id = COALESCE(excluded.issue_id, ariba_requisitions.issue_id),
                     last_seen_ts = excluded.last_seen_ts""",
                (fields.get("pr_number"), fields.get("requester"), fields.get("descriptor"),
                 fields.get("amount"), raw_item_id, issue_id, now, now),
            )
        finally:
            conn.close()


def get_ariba_requisition(pr_number: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM ariba_requisitions WHERE pr_number = ?", (pr_number,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_ariba_requisitions_for_issue(issue_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM ariba_requisitions WHERE issue_id = ? ORDER BY last_seen_ts DESC", (issue_id,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def adjust_data_point_trust(definition_id: str, delta: float, *, floor: float = 0.1, ceiling: float = 0.9) -> None:
    """Same bounded bump/penalty shape as workgraph_lessons.py's trust
    arithmetic (never a hard cliff) - the actual DECISION of when/how much
    to adjust belongs to whatever calls this (the retrofitted pipeline,
    task #215/#216), not this raw store function."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT trust_score FROM data_point_definitions WHERE id = ?", (definition_id,)).fetchone()
            if row is None:
                return
            new_score = max(floor, min(ceiling, row["trust_score"] + delta))
            conn.execute("UPDATE data_point_definitions SET trust_score = ? WHERE id = ?", (new_score, definition_id))
        finally:
            conn.close()


def touch_data_point_last_matched(definition_id: str) -> None:
    """Section 3's staleness signal - a confirmed data point that stops
    getting touched for a long stretch is what eventually surfaces as
    'hasn't come up in months, still relevant?' (the job-change case)."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE data_point_definitions SET last_matched_ts = ? WHERE id = ?", (time.time(), definition_id))
        finally:
            conn.close()


def record_data_point_value(*, definition_id: str, work_object_id: str, value: str, extraction_source: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO data_point_values (definition_id, work_object_id, value, extraction_source, extracted_ts)
                   VALUES (?, ?, ?, ?, ?)""",
                (definition_id, work_object_id, value, extraction_source, time.time()),
            )
        finally:
            conn.close()
    touch_data_point_last_matched(definition_id)


def list_data_point_values_for_work_object(work_object_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM data_point_values WHERE work_object_id = ? ORDER BY extracted_ts", (work_object_id,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def observe_candidate_pattern(pattern_signature: str, *, is_new_thread: bool) -> dict:
    """Section 3's continuous, cheap (no LLM cost) tracker. Plain counting -
    increments occurrence_count always, distinct_thread_count only when the
    caller has already determined this is a genuinely new thread/sender for
    this pattern (that judgment belongs to the caller, which has the real
    raw_item context - this function just counts). Returns the row so the
    caller can check it against the real significance bar (5 occurrences,
    2+ distinct threads, 60-day window - Marc's own numbers) and decide
    whether to promote it to a real LLM-drafted proposal."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT * FROM candidate_pattern_observations WHERE pattern_signature = ?", (pattern_signature,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO candidate_pattern_observations
                       (pattern_signature, occurrence_count, distinct_thread_count, first_seen_ts, last_seen_ts)
                       VALUES (?, 1, ?, ?, ?)""",
                    (pattern_signature, 1 if is_new_thread else 0, now, now),
                )
            else:
                conn.execute(
                    """UPDATE candidate_pattern_observations
                       SET occurrence_count = occurrence_count + 1,
                           distinct_thread_count = distinct_thread_count + ?,
                           last_seen_ts = ?
                       WHERE pattern_signature = ?""",
                    (1 if is_new_thread else 0, now, pattern_signature),
                )
            row = conn.execute(
                "SELECT * FROM candidate_pattern_observations WHERE pattern_signature = ?", (pattern_signature,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row)


def get_pattern_signature_for_definition(definition_id: str) -> Optional[str]:
    """Reverse of mark_candidate_pattern_promoted - the retrofitted
    pipeline (workgraph_discovery.matched_discovered_points, #216) needs
    the ORIGINATING pattern signature back from a confirmed definition to
    know what to actually re-check for a match (a sender domain, a
    labeled-field name) - the LLM's own free-text deterministic_rule
    description isn't mechanically executable, but the signature that
    surfaced it always is."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT pattern_signature FROM candidate_pattern_observations WHERE promoted_to_definition_id = ?",
                (definition_id,),
            ).fetchone()
        finally:
            conn.close()
    return row["pattern_signature"] if row else None


def mark_candidate_pattern_promoted(pattern_signature: str, definition_id: str) -> None:
    """Once a candidate pattern crosses the significance bar and a real
    proposal is drafted from it, tag the observation row so the same
    pattern doesn't get proposed again on its next occurrence while the
    first proposal is still pending review."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE candidate_pattern_observations SET promoted_to_definition_id = ? WHERE pattern_signature = ?",
                (definition_id, pattern_signature),
            )
        finally:
            conn.close()


_ASSISTANT_SESSION_ID = "default"


def get_assistant_session_id() -> Optional[str]:
    """Task #232 - the currently persisted live-assistant Claude session,
    or None if no conversation has happened yet (or it was explicitly
    reset). workgraph_assistant.ask() falls back to minting a fresh
    session when this is None or a --resume against it fails."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT session_id FROM assistant_sessions WHERE id = ?", (_ASSISTANT_SESSION_ID,)
            ).fetchone()
        finally:
            conn.close()
    return row["session_id"] if row else None


def set_assistant_session_id(session_id: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO assistant_sessions (id, session_id, updated_ts) VALUES (?, ?, ?)",
                (_ASSISTANT_SESSION_ID, session_id, time.time()),
            )
        finally:
            conn.close()


def clear_assistant_session_id() -> None:
    """Explicit 'start a new conversation' - drops the persisted pointer
    without deleting anything from claude -p's own session log (that log
    is Claude Code's own concern, not this database's)."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM assistant_sessions WHERE id = ?", (_ASSISTANT_SESSION_ID,))
        finally:
            conn.close()


def get_candidate_pattern_observation(pattern_signature: str) -> Optional[dict]:
    """Pure read, no side effect - unlike observe_candidate_pattern (which
    always increments), this is what a caller uses to check a signature's
    CURRENT state (e.g. against crosses_significance_bar) without
    incorrectly counting a second, phantom occurrence just for looking."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM candidate_pattern_observations WHERE pattern_signature = ?", (pattern_signature,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_candidate_pattern_observations() -> list[dict]:
    """Every tracked pattern observation - workgraph_discovery.run_
    monthly_sweep's re-check pass over whatever the continuous per-item
    hook has accumulated since the last sweep (or ever, for a pattern the
    hook missed)."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM candidate_pattern_observations").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def record_pattern_observation_thread(pattern_signature: str, thread_key: str) -> bool:
    """Returns True the FIRST time this (signature, thread_key) pair is seen,
    False on every later repeat - the caller (workgraph_discovery.py) passes
    that straight through as observe_candidate_pattern's is_new_thread, so
    distinct_thread_count only grows on a genuinely new thread, never a
    resend/forward of the same one."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO pattern_observation_threads (pattern_signature, thread_key) VALUES (?, ?)",
                (pattern_signature, thread_key),
            )
            return cur.rowcount > 0
        finally:
            conn.close()


def get_claim(claim_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def bump_claims_revision(issue_id: str) -> None:
    """Fixed 2026-08-04 (architecture-review follow-up, P1): insert_claim/
    touch_claim/update_claim_status each already bump claims_revision
    inline as part of their own write - but a genuinely new key_fact
    (curator's extracted_json.key_facts field, surfaced read-only by
    workgraph_key_facts.py) was never materialized as a claim at all, so
    an issue/project could accumulate real new material information while
    its synthesis marker stayed byte-for-byte unchanged. Callers that have
    no claim row of their own to attach the bump to (workgraph_claims.
    materialize_claims_for_raw_item, for a key-facts-only extraction) call
    this directly instead of duplicating the inline UPDATE.

    Corrected pipeline (2026-08-05): work_objects directly, not the
    `issues` view - issue_id is now routinely a cluster (Phase B), and the
    view's UPDATE trigger silently no-ops for one (zero matching rows)."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE work_objects SET claims_revision = claims_revision + 1 WHERE id = ?",
                (issue_id,),
            )
        finally:
            conn.close()


def get_claims_revision(issue_id: str) -> int:
    """Corrected pipeline (2026-08-05): reads work_objects directly, not
    the `issues` view - issue_id may be a cluster."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT claims_revision FROM work_objects WHERE id = ?", (issue_id,)
            ).fetchone()
        finally:
            conn.close()
    return row["claims_revision"] if row else 0


def get_project_claims_fingerprint(project_id: str) -> int:
    """Project-level revision is derived, not stored (Section 9.5) -
    computed at synthesis-check time rather than kept as a second counter
    in sync with its members.

    Fixed 2026-08-04 (architecture-review follow-up, P1): the previous
    version returned bare MAX(claims_revision) across member issues, which
    has two real aggregation gaps a live audit confirmed: (1) a NON-max
    member's revision changing (e.g. member A sits at rev 10, member B goes
    2 -> 3) leaves the MAX unchanged, so the project reads falsely fresh
    even though real new claims activity happened; (2) adding or removing a
    member doesn't touch any existing member's claims_revision at all, so
    membership changes never invalidate synthesis either. A deterministic
    hash (zlib.crc32, not Python's salted hash()) of the sorted (issue_id,
    claims_revision) sequence across every member closes both gaps - any
    member's revision changing, or the member SET itself changing, changes
    the encoded string and therefore the fingerprint. Kept as a plain int
    (not a hex string) so compute_evidence_marker's "rev:{n}" format and
    list_stale_entities' _parse_rev sort key both keep working unchanged -
    only what's fed into the aggregation changed, not the marker shape.

    Corrected pipeline Phase D (2026-08-05): queries work_objects directly,
    not the `issues` view - a project's members are routinely clusters now
    (Phase C promotes a cluster group by reparenting under a real project,
    same as it's always worked for real issues), and the view excludes
    them by construction. Without this, a project made up entirely of
    clusters (the common case right after Phase C promotion, before any
    real issue has been extracted from it) would read a permanently empty
    member set here - crc32("") forever - so its synthesis would never be
    flagged stale again after the first pass, no matter how much real
    claims activity accumulated on its clusters."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, claims_revision FROM work_objects WHERE parent_id = ? AND object_type = 'request' ORDER BY id",
                (project_id,),
            ).fetchall()
        finally:
            conn.close()
    encoded = "|".join(f"{r['id']}:{r['claims_revision']}" for r in rows)
    return zlib.crc32(encoded.encode("utf-8"))


def list_open_claims_for_issue(issue_id: str, claim_type: Optional[str] = None) -> list[dict]:
    return list_open_claims_for_issues([issue_id], claim_type=claim_type).get(issue_id, [])


def list_claims_for_issue(issue_id: str) -> list[dict]:
    """ALL claims regardless of status - unlike list_open_claims_for_issue,
    needed by the three-tier timeline (Section 12.9), which must show a
    completed/dismissed claim's own history too, not just what's still
    open."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM claims WHERE issue_id = ? ORDER BY first_seen_ts ASC", (issue_id,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def list_claims_for_raw_item(raw_item_id: int) -> list[dict]:
    """Every claim THIS raw_item ever produced, any status - the diff base
    for corrected-extraction reconciliation (2026-08-04): comparing the
    still-OPEN ones against a re-extraction's new content is how
    workgraph_claims.materialize_claims_for_raw_item tells apart an
    unchanged claim, a real correction, and a genuine addition. A claim
    already resolved by a real human action (done/dismissed) before the
    correction landed is deliberately excluded from that comparison by
    the caller, not by this function - it stays untouched either way."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM claims WHERE raw_item_id = ? ORDER BY first_seen_ts ASC", (raw_item_id,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# --- three-tier timeline (design doc Section 12.9) -------------------------
# Read-time views over evidence_units/claims/claim_events/artifact_versions/
# prepared_actions/issue_state_history/audit_log - NOT a new stored table,
# per the design's own framing.

_MILESTONE_CLAIM_EVENT_TYPES = ("create", "complete", "dismiss")


def list_complete_timeline_for_issue(issue_id: str) -> list[dict]:
    """The 'complete event timeline' - every evidence_unit + every claim
    event for this issue, unified and chronological (today's issue_state_
    history plus everything else, as the design puts it)."""
    entries = []
    for e in list_evidence(issue_id):
        entries.append({"ts": e["ts"], "tier": "evidence", "kind": e["type"], "detail": e["summary"]})
    for c in list_claims_for_issue(issue_id):
        for ev in list_claim_events_for_claim(c["id"]):
            entries.append({
                "ts": ev["ts"], "tier": "claim_event", "kind": ev["event_type"], "detail": c["text"],
                "claim_type": c["claim_type"], "claim_id": c["id"], "actor": ev["actor"],
            })
    entries.sort(key=lambda e: e["ts"])
    return entries


def list_milestone_timeline_for_issue(issue_id: str) -> list[dict]:
    """The deterministically-filtered milestone view. Real producers wired:
    claim create/complete/dismiss events (ask/commitment/decision/date),
    artifact_versions (a version was produced - v2.6), prepared_actions
    reaching a terminal state (v2.7), issue_state_history transitions to
    'blocked'/'done', and audit_log 'merged_into' entries (a work_object
    merge). 'approval received' and 'work_object split' have no current
    producer in this codebase - named gaps, not silently dropped, same
    discipline as everywhere else in this doc."""
    entries = []
    for c in list_claims_for_issue(issue_id):
        for ev in list_claim_events_for_claim(c["id"]):
            if ev["event_type"] in _MILESTONE_CLAIM_EVENT_TYPES:
                entries.append({
                    "ts": ev["ts"], "kind": f"{c['claim_type']}_{ev['event_type']}", "detail": c["text"],
                    "claim_id": c["id"], "actor": ev["actor"],
                })
    with _lock:
        conn = _connect()
        try:
            for r in conn.execute(
                """SELECT av.created_ts AS ts, al.title AS title FROM artifact_versions av
                   JOIN artifact_lineages al ON al.id = av.lineage_id
                   WHERE al.work_object_id = ?""", (issue_id,),
            ).fetchall():
                entries.append({"ts": r["ts"], "kind": "artifact_version_produced", "detail": r["title"]})
            for r in conn.execute(
                """SELECT resolved_ts AS ts, state, action_type FROM prepared_actions
                   WHERE claim_id IN (SELECT id FROM claims WHERE issue_id = ?) AND resolved_ts IS NOT NULL""",
                (issue_id,),
            ).fetchall():
                entries.append({"ts": r["ts"], "kind": f"prepared_action_{r['state']}", "detail": r["action_type"]})
            for r in conn.execute(
                """SELECT changed_ts AS ts, to_state FROM issue_state_history
                   WHERE issue_id = ? AND to_state IN ('blocked','done')""", (issue_id,),
            ).fetchall():
                entries.append({"ts": r["ts"], "kind": f"issue_{r['to_state']}", "detail": None})
            for r in conn.execute(
                """SELECT changed_ts AS ts, new_value FROM audit_log
                   WHERE entity_type = 'issue' AND entity_id = ? AND field = 'merged_into'""", (issue_id,),
            ).fetchall():
                entries.append({"ts": r["ts"], "kind": "work_object_merged", "detail": f"merged into {r['new_value']}"})
        finally:
            conn.close()
    entries.sort(key=lambda e: e["ts"])
    return entries


def list_activity_stream_for_issue(issue_id: str) -> list[dict]:
    """The complement tier (routine comms) - an evidence_unit whose
    raw_item already produced a milestone-tier claim event is excluded,
    since that specific communication already has its own dedicated
    milestone entry. Never the default view, per the design's own
    framing - a UI concern, not enforced here."""
    milestone_claim_ids = {e["claim_id"] for e in list_milestone_timeline_for_issue(issue_id) if e.get("claim_id")}
    milestone_raw_item_ids = set()
    for cid in milestone_claim_ids:
        claim = get_claim(cid)
        if claim and claim.get("raw_item_id"):
            milestone_raw_item_ids.add(claim["raw_item_id"])
    return [e for e in list_evidence(issue_id) if e.get("raw_item_id") not in milestone_raw_item_ids]


def list_open_claims_for_issues(issue_ids: list[str], claim_type: Optional[str] = None) -> dict[str, list[dict]]:
    if not issue_ids:
        return {}
    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join("?" * len(issue_ids))
            params: list = list(issue_ids)
            type_clause = ""
            if claim_type is not None:
                type_clause = " AND claim_type = ?"
                params.append(claim_type)
            rows = conn.execute(
                f"""SELECT * FROM claims WHERE issue_id IN ({placeholders})
                    AND status = 'open'{type_clause}
                    ORDER BY last_seen_ts DESC""",
                params,
            ).fetchall()
        finally:
            conn.close()
    out: dict[str, list[dict]] = {iid: [] for iid in issue_ids}
    for r in rows:
        out.setdefault(r["issue_id"], []).append(dict(r))
    return out


def list_issue_ids_by_state(states: list[str]) -> list[str]:
    """Lightweight id-only counterpart to list_issues (which joins
    synthesis/parties for display and defaults to limit=200) - for a
    batched sweep like workgraph_reconcile.detect_issue_closed_with_open_
    claims_contradictions that needs every matching issue id, not a
    display page of the first 200."""
    if not states:
        return []
    with _lock:
        conn = _connect()
        try:
            placeholders = ",".join("?" * len(states))
            rows = conn.execute(
                f"SELECT id FROM issues WHERE state IN ({placeholders})", states,
            ).fetchall()
        finally:
            conn.close()
    return [r["id"] for r in rows]


def list_open_claims_with_canonical_key_and_project() -> list[dict]:
    """Enhancement idea panel #17 (Duplicate/conflicting-ask detector
    across project, worker capability): every open claim that HAS a
    canonical_key, joined to its issue's project_id and state, in one
    query - the DB-wide batched read workgraph_claims.find_duplicate_or_
    conflicting_asks_across_project needs to group by (project_id,
    claim_type, canonical_key) without an N+1 per-issue lookup. Claims on
    issues with no project (project_id IS NULL) are excluded at the SQL
    level - a canonical_key can only collide ACROSS issues once those
    issues are members of the same project; this is the direct
    complement of list_all_reference_base_id_pairs' own same-project
    EXCLUSION for panel #14 (there, same-project is not a collision
    worth flagging; here, same-project is the ONLY case worth flagging -
    a real ask/commitment/decision tracked once per issue by design, so
    two issues sharing one carries real signal only once they're grouped
    into the same underlying piece of work)."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("""
                SELECT c.id, c.issue_id, c.claim_type, c.canonical_key, c.text,
                       i.project_id, i.state
                FROM claims c
                JOIN issues i ON i.id = c.issue_id
                WHERE c.status = 'open' AND c.canonical_key IS NOT NULL
                      AND i.project_id IS NOT NULL
            """).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# --- pending_claim_suggestions (2026-08-04, task #155) ----------------------

def create_claim_suggestion(*, claim_id: int, suggestion_kind: str, evidence_type: str,
                             evidence_note: Optional[str] = None, raw_item_id: Optional[int] = None) -> int:
    """Dedupe-then-insert, same shape as create_project_suggestion: a
    PENDING suggestion already on record for this exact (claim_id,
    evidence_type) pair is reused (its id returned) rather than
    duplicated - re-running a sweep (the daily backfill, or reprocessing
    a raw_item) never grows a second pending row asking the same
    question twice. Unlike create_project_suggestion there is no veto
    concept here (no identity_constraints equivalent for claims yet), so
    this never returns None."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                """SELECT id FROM pending_claim_suggestions
                   WHERE status = 'pending' AND claim_id = ? AND evidence_type = ?""",
                (claim_id, evidence_type),
            ).fetchone()
            if existing:
                return existing["id"]
            cur = conn.execute(
                """INSERT INTO pending_claim_suggestions
                   (claim_id, suggestion_kind, evidence_type, evidence_note, raw_item_id, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (claim_id, suggestion_kind, evidence_type, evidence_note, raw_item_id, now),
            )
            return cur.lastrowid
        finally:
            conn.close()


def get_claim_suggestion(suggestion_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM pending_claim_suggestions WHERE id = ?", (suggestion_id,),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_pending_claim_suggestions(issue_id: Optional[str] = None) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            if issue_id is not None:
                rows = conn.execute(
                    """SELECT s.* FROM pending_claim_suggestions s
                       JOIN claims c ON c.id = s.claim_id
                       WHERE s.status = 'pending' AND c.issue_id = ?
                       ORDER BY s.created_ts ASC""",
                    (issue_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pending_claim_suggestions WHERE status = 'pending' ORDER BY created_ts ASC",
                ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def resolve_claim_suggestion(suggestion_id: int, status: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE pending_claim_suggestions SET status = ?, resolved_ts = ? WHERE id = ?",
                (status, time.time(), suggestion_id),
            )
        finally:
            conn.close()


def index_evidence_fts(raw_item_id: int, issue_id: Optional[str], body: str) -> None:
    """Idempotent (delete-then-insert, keyed on raw_item_id) - safe to call
    every time a raw_item's text is resolved, not just on first backfill."""
    if not body or not body.strip():
        return
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM evidence_fts WHERE raw_item_id = ?", (raw_item_id,))
            conn.execute(
                "INSERT INTO evidence_fts (body, raw_item_id, issue_id) VALUES (?, ?, ?)",
                (body, raw_item_id, issue_id),
            )
        finally:
            conn.close()


def _fts5_safe_query(query: str) -> str:
    """Turn free-typed user text into an FTS5-safe MATCH expression.

    FTS5 treats characters like ``-`` (column filter / NOT), ``"``, ``*``,
    ``(``/``)``, ``:`` as query-syntax operators, not literal text. A raw
    term like ``PR-1189827`` (an ordinary PO/PR number, not a special
    query) throws a real ``sqlite3.OperationalError`` today. Strip each
    token to plain alphanumerics and AND them together as quoted phrases,
    which is always valid FTS5 syntax and still matches term-contains
    queries (order/adjacency no longer required).
    """
    tokens = re.findall(r"[\w]+", query)
    if not tokens:
        return '""'
    return " AND ".join(f'"{t}"' for t in tokens)


def search_evidence_fts(query: str, issue_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    query = _fts5_safe_query(query)
    with _lock:
        conn = _connect()
        try:
            if issue_id:
                rows = conn.execute(
                    """SELECT raw_item_id, issue_id, snippet(evidence_fts, 0, '[', ']', '...', 12) AS snippet
                       FROM evidence_fts WHERE evidence_fts MATCH ? AND issue_id = ?
                       ORDER BY rank LIMIT ?""",
                    (query, issue_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT raw_item_id, issue_id, snippet(evidence_fts, 0, '[', ']', '...', 12) AS snippet
                       FROM evidence_fts WHERE evidence_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (query, limit),
                ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


# --- synthesis ---------------------------------------------------------
# Per-entity (Project, or a standalone Issue) narrative synthesis - see
# workgraph_synthesis.py for the deterministic staleness check that decides
# WHEN this needs recomputing. Writing it is always a worker's call (real LLM
# judgment); this module only persists the result.

def get_synthesis(entity_type: str, entity_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM synthesis WHERE entity_type = ? AND entity_id = ?",
                (entity_type, entity_id),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    d = dict(row)
    try:
        d["next_steps"] = json.loads(d["next_steps"]) if d["next_steps"] else []
    except Exception:
        d["next_steps"] = []
    try:
        d["suggested_actions"] = json.loads(d["suggested_actions"]) if d["suggested_actions"] else []
    except Exception:
        d["suggested_actions"] = []
    try:
        d["estimated_completion"] = json.loads(d["estimated_completion"]) if d["estimated_completion"] else None
    except Exception:
        d["estimated_completion"] = None
    return d


def upsert_synthesis(
    *, entity_type: str, entity_id: str, summary: Optional[str],
    next_steps_json: str, suggested_actions_json: str, synthesized_from_marker: str,
    derived_title: Optional[str] = None, estimated_completion_json: Optional[str] = None,
) -> None:
    """The one place synthesis rows are ever written. synthesized_from_marker
    is always computed by the caller from workgraph_synthesis.compute_evidence_marker
    right before this call - server_lean.py's route does this itself rather
    than trusting a worker-supplied marker."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO synthesis
                   (entity_type, entity_id, summary, next_steps, suggested_actions, synthesized_at,
                    synthesized_from_marker, derived_title, estimated_completion)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                       summary = excluded.summary, next_steps = excluded.next_steps,
                       suggested_actions = excluded.suggested_actions,
                       synthesized_at = excluded.synthesized_at,
                       synthesized_from_marker = excluded.synthesized_from_marker,
                       derived_title = COALESCE(excluded.derived_title, synthesis.derived_title),
                       estimated_completion = excluded.estimated_completion""",
                (entity_type, entity_id, summary, next_steps_json, suggested_actions_json, now,
                 synthesized_from_marker, derived_title, estimated_completion_json),
            )
        finally:
            conn.close()
    # Design doc Section 12.8: entity_id is a work_object id for both real
    # entity_type values ('issue'/'project', the only ones server_lean.py's
    # route accepts) - this work_object's own content was just used in a
    # real summary shown to Marc.
    if entity_type in ("issue", "project"):
        advance_work_object_exposure_state(entity_id, "used_in_summary")


def touch_synthesis_marker(entity_type: str, entity_id: str, marker: str) -> None:
    """Advances synthesized_from_marker WITHOUT rewriting the narrative -
    used when new evidence arrived since the last synthesis but wasn't
    material enough to justify waking curator for a re-synthesis pass (see
    workgraph_synthesis.py's materiality filter). No-ops if there's no
    existing synthesis row yet - a never-synthesized entity is always
    material on its first pass (see list_stale_entities)."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE synthesis SET synthesized_from_marker = ? WHERE entity_type = ? AND entity_id = ?",
                (marker, entity_type, entity_id),
            )
        finally:
            conn.close()


def set_derived_title(entity_type: str, entity_id: str, derived_title: str) -> None:
    """Sets ONLY derived_title, leaving any existing summary/next_steps/
    suggested_actions alone - used by the deterministic title generator
    (workgraph_classify.compute_deterministic_title), which runs independently
    of and typically well before curator's LLM synthesis pass ever touches
    this row. If curator later writes a real synthesis without its own title,
    upsert_synthesis's COALESCE keeps this one rather than blanking it."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO synthesis (entity_type, entity_id, derived_title, synthesized_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(entity_type, entity_id) DO UPDATE SET derived_title = excluded.derived_title""",
                (entity_type, entity_id, derived_title, time.time()),
            )
        finally:
            conn.close()


# --- attachments -------------------------------------------------------------

def create_attachment(
    *, entity_type: str, entity_id: Optional[str], kind: str, filename: str,
    stored_path: str, content_type: Optional[str], size_bytes: int,
    sha256_hex: Optional[str], uploaded_by: str, extracted_text: Optional[str] = None,
) -> int:
    """Design doc Section 12.5: the moment a NEW attachment's hash matches
    one or more already-stored attachments, all of them get linked into
    one artifact_lineages/artifact_versions record - the real, live
    producer, alongside backfill_artifact_lineages (below) for duplicate
    groups that already existed before this was built. This is every real
    attachment-creation call site's single choke point (the manual
    /api/attachments upload route AND outlook_com_ingest.py's ingest-
    absorb function both call this directly), so wiring it here - rather
    than duplicating the check in each caller - covers both for free."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO attachments
                   (entity_type, entity_id, kind, filename, stored_path, content_type, size_bytes, sha256, uploaded_by, uploaded_ts, extracted_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (entity_type, entity_id, kind, filename, stored_path, content_type, size_bytes, sha256_hex, uploaded_by, time.time(), extracted_text),
            )
            new_id = cur.lastrowid
        finally:
            conn.close()
    if sha256_hex:
        group = list_attachments_by_hash(sha256_hex)
        if len(group) >= 2:  # a genuinely unique hash never gets a speculative lineage of its own
            _ensure_artifact_versions(group)  # also invalidates the owning signature - see its own docstring
    return new_id


def list_attachments_missing_extracted_text(extensions: tuple[str, ...]) -> list[dict]:
    """Enhancement idea panel #7 (E6): every attachment whose filename
    matches one of these extensions (case-insensitive) but has no
    extracted_text yet - the real backfill target for a file type that
    only just got a real extractor (e.g. .docx, attachment_extract.py).
    NULL/empty extracted_text both count as 'missing' - a prior attempt
    that legitimately found nothing (a scanned-image PDF) is
    indistinguishable from 'never tried' at the column level, which is
    fine here: re-running extraction on either is cheap and idempotent."""
    with _lock:
        conn = _connect()
        try:
            placeholders = " OR ".join("LOWER(filename) LIKE ?" for _ in extensions)
            params = [f"%{ext.lower()}" for ext in extensions]
            rows = conn.execute(
                f"SELECT * FROM attachments WHERE (extracted_text IS NULL OR extracted_text = '') "
                f"AND ({placeholders})",
                params,
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def update_attachment_extracted_text(attachment_id: int, extracted_text: Optional[str]) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE attachments SET extracted_text = ? WHERE id = ?", (extracted_text, attachment_id)
            )
        finally:
            conn.close()


def find_attachment_by_hash(sha256_hex: str) -> Optional[dict]:
    """The first already-stored attachment (any entity) with this exact
    content hash, or None. Task #29 (2026-08-01): the same real document
    forwarded across several emails used to get stored as N separate
    byte-identical copies and N separate extraction passes - sha256 was
    always captured but never checked against what's already there. A
    caller that finds a match should reuse its stored_path/extracted_text
    rather than copying the file or re-extracting again."""
    if not sha256_hex:
        return None
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE sha256 = ? LIMIT 1", (sha256_hex,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_attachments_by_hash(sha256_hex: str) -> list[dict]:
    """Every attachment (any entity) sharing this exact content hash,
    oldest first - unlike find_attachment_by_hash (LIMIT 1, for upload-
    time dedup), this is the full group compute_work_object_signature-
    style lineage logic needs to see."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM attachments WHERE sha256 = ? ORDER BY uploaded_ts ASC", (sha256_hex,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def _work_object_id_for_attachment(attachment: dict) -> Optional[str]:
    """Resolves the work_object (issue) an attachment belongs to, if any -
    direct for entity_type='issue', a lookup for entity_type='raw_item'
    (email attachments are scoped to the raw_item at ingest time, before
    classification has assigned an issue - see list_attachments_for_
    issue's own docstring). 'project'/'chat'-scoped or unlinked raw_item
    attachments have no single owning work_object - None, not guessed."""
    if attachment.get("entity_type") == "issue":
        return attachment.get("entity_id")
    if attachment.get("entity_type") == "raw_item" and attachment.get("entity_id"):
        with _lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT issue_id FROM raw_items WHERE id = ?", (int(attachment["entity_id"]),)
                ).fetchone()
            finally:
                conn.close()
        return row["issue_id"] if row else None
    return None


def list_artifact_lineage_ids_for_work_object(work_object_id: str) -> list[str]:
    """The real, now-populatable content for work_object_signatures.
    accepted_lineages (Section 12.7) - a gap that stayed an honest `[]`
    until artifact_lineages (this section) existed to answer it."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id FROM artifact_lineages WHERE work_object_id = ?", (work_object_id,)
            ).fetchall()
        finally:
            conn.close()
    return [r["id"] for r in rows]


def get_artifact_lineage(lineage_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM artifact_lineages WHERE id = ?", (lineage_id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def create_artifact_lineage(*, id: str, work_object_id: Optional[str], title: str,
                             created_ts: Optional[float] = None) -> str:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO artifact_lineages (id, work_object_id, title, created_ts) VALUES (?, ?, ?, ?)",
                (id, work_object_id, title, created_ts if created_ts is not None else time.time()),
            )
        finally:
            conn.close()
    return id


def create_artifact_version(*, lineage_id: str, attachment_id: int, sha256: str,
                             document_role: str = "other", derived_from_id: Optional[int] = None,
                             created_ts: Optional[float] = None) -> int:
    """document_role/derived_from_id default to 'other'/None - design doc
    Section 12.5: nothing today can tell a redline from an identical copy
    from content alone (no redline-detection producer exists yet), so a
    role beyond the default is only ever set by a FUTURE real producer
    (curator's synthesis routine, or the claudeskills redline output if
    task #112 is ever built), never guessed here."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO artifact_versions (lineage_id, attachment_id, document_role, derived_from_id, sha256, created_ts)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (lineage_id, attachment_id, document_role, derived_from_id, sha256,
                 created_ts if created_ts is not None else time.time()),
            )
            return cur.lastrowid
        finally:
            conn.close()


def find_artifact_version_by_attachment(attachment_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM artifact_versions WHERE attachment_id = ? LIMIT 1", (attachment_id,)
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_artifact_versions_for_lineage(lineage_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM artifact_versions WHERE lineage_id = ? ORDER BY created_ts ASC", (lineage_id,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def list_other_occurrences_for_attachment(attachment_id: int) -> list[dict]:
    """Design doc Section 12.5's own motivating signal - 'this document
    also appears on N other threads' - v2.6 built the real data
    (artifact_lineages/artifact_versions) and the live linking producer,
    but never a caller that actually surfaces it. This is that consumer:
    one entry per OTHER attachment sharing this one's lineage (empty if
    this attachment has no confirmed duplicate at all), each tagged with
    the owning work_object's title where resolvable (None for a 'project'/
    'chat'-scoped attachment, or a 'raw_item' never linked to an issue -
    same honest non-guess as _work_object_id_for_attachment itself)."""
    version = find_artifact_version_by_attachment(attachment_id)
    if version is None:
        return []
    out = []
    for v in list_artifact_versions_for_lineage(version["lineage_id"]):
        if v["attachment_id"] == attachment_id:
            continue
        att = get_attachment(v["attachment_id"])
        if att is None:
            continue
        work_object_id = _work_object_id_for_attachment(att)
        title = None
        if work_object_id:
            wo = get_issue(work_object_id)
            title = wo["title"] if wo else None
            if title is None:
                wo = get_project(work_object_id)
                title = wo["name"] if wo else None
        out.append({
            "attachment_id": att["id"], "filename": att["filename"], "uploaded_ts": att["uploaded_ts"],
            "work_object_id": work_object_id, "work_object_title": title,
        })
    out.sort(key=lambda o: o["uploaded_ts"])
    return out


def _ensure_artifact_versions(attachments: list[dict]) -> str:
    """Given 2+ attachments confirmed to share one sha256, ensures every
    one of them has an artifact_version row in the SAME lineage - reuses
    an existing lineage if any of them already has one (from a prior live
    link or a previous backfill_artifact_lineages run), otherwise creates
    a new one anchored on the earliest attachment (design doc Section
    12.5: "this document also appears on N other threads" - the earliest
    copy is the real origin). Idempotent per-attachment - one that already
    has a version is left alone, so this is safe to call with a mix of
    already-linked and not-yet-linked attachments.

    Also invalidates the owning work_object's cached signature (Section
    12.7's accepted_lineages) if a NEW lineage/version actually got
    created - both real callers (create_attachment's live hook,
    backfill_artifact_lineages) share this one call site, so invalidating
    here (rather than in each caller) is the only way both stay correct.
    A live signature computed via backtest_scored_model()/scored_grouping_
    decision BEFORE a historical duplicate group was backfilled would
    otherwise keep reporting a stale, empty accepted_lineages forever -
    nothing else would ever re-touch that work_object to invalidate it."""
    versions = {a["id"]: find_artifact_version_by_attachment(a["id"]) for a in attachments}
    existing_lineage_id = next((v["lineage_id"] for v in versions.values() if v is not None), None)
    changed = False
    if existing_lineage_id is not None:
        lineage_id = existing_lineage_id
    else:
        earliest = min(attachments, key=lambda a: a["uploaded_ts"])
        lineage_id = f"lineage-{earliest['sha256'][:16]}"
        if get_artifact_lineage(lineage_id) is None:
            create_artifact_lineage(
                id=lineage_id, work_object_id=_work_object_id_for_attachment(earliest),
                title=earliest["filename"], created_ts=earliest["uploaded_ts"],
            )
    for a in attachments:
        if versions[a["id"]] is None:
            create_artifact_version(
                lineage_id=lineage_id, attachment_id=a["id"], sha256=a["sha256"], created_ts=a["uploaded_ts"],
            )
            changed = True
    if changed:
        lineage = get_artifact_lineage(lineage_id)
        if lineage and lineage.get("work_object_id"):
            invalidate_work_object_signature(lineage["work_object_id"])
    return lineage_id


def backfill_artifact_lineages() -> dict:
    """One-time backfill (design doc Section 12.5) for duplicate-hash
    groups that already existed before create_attachment started linking
    new duplicates live - the 39 real groups found in this session's own
    analysis (all legitimate cross-work_object reuse of the same file,
    zero same-work_object waste). Idempotent - safe to re-run; an
    attachment that already has a version (whether from a prior backfill
    run or from the live path having already handled its pair) is
    skipped, so this only ever picks up what hasn't been linked yet."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT sha256 FROM attachments WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*) >= 2"
            ).fetchall()
            hashes = [r["sha256"] for r in rows]
        finally:
            conn.close()
    lineages_before = {h: get_artifact_lineage(f"lineage-{h[:16]}") is not None for h in hashes}
    for sha256_hex in hashes:
        _ensure_artifact_versions(list_attachments_by_hash(sha256_hex))
    lineages_created = sum(1 for h in hashes if not lineages_before[h])
    return {"duplicate_groups_found": len(hashes), "lineages_created": lineages_created}


def list_attachments(entity_type: str, entity_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM attachments WHERE entity_type = ? AND entity_id = ? ORDER BY uploaded_ts DESC",
                (entity_type, entity_id),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_attachment(attachment_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def list_attachments_for_issue(issue_id: str) -> list[dict]:
    """Everything attachable an issue has: rows scoped directly to it (kind
    'output'/'upload' written after classification) plus email attachments
    scoped to any raw_item that has since been linked to it (kind 'reference',
    written at ingest time before an issue existed to attach them to) - joined
    at read time rather than physically re-parenting files when clustering runs."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT a.* FROM attachments a WHERE a.entity_type = 'issue' AND a.entity_id = ?
                   UNION ALL
                   SELECT a.* FROM attachments a
                   JOIN raw_items r ON a.entity_type = 'raw_item' AND a.entity_id = CAST(r.id AS TEXT)
                   WHERE r.issue_id = ?
                   ORDER BY uploaded_ts DESC""",
                (issue_id, issue_id),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def list_attachments_for_project(project_id: str) -> list[dict]:
    """Direct project attachments plus every attachment belonging to any issue
    under this project (which itself already inherits its raw_items' email
    attachments - see list_attachments_for_issue). Small N, done in Python
    rather than one bigger UNION query for the same reason list_issues_for_project
    already loops issue-by-issue elsewhere in this module."""
    with _lock:
        conn = _connect()
        try:
            direct = conn.execute(
                "SELECT * FROM attachments WHERE entity_type = 'project' AND entity_id = ? ORDER BY uploaded_ts DESC",
                (project_id,),
            ).fetchall()
            direct = [dict(r) for r in direct]
        finally:
            conn.close()
    issue_ids = [i["id"] for i in list_issues_for_project(project_id)]
    seen_ids = {a["id"] for a in direct}
    combined = list(direct)
    for issue_id in issue_ids:
        for a in list_attachments_for_issue(issue_id):
            if a["id"] not in seen_ids:
                seen_ids.add(a["id"])
                combined.append(a)
    combined.sort(key=lambda a: a["uploaded_ts"], reverse=True)
    return combined


def delete_attachment(attachment_id: int) -> bool:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
            return cur.rowcount > 0
        finally:
            conn.close()
