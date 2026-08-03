"""
workgraph_store.py — SQLite-backed work graph for the Symphony cockpit.

Same connection/WAL shape as bus.py's core (`_connect`), deliberately without
bus.py's dual-write/event-log machinery — that's specific to migrating the
legacy `events` table and has no bearing on this brand-new database.

raw_items      — one row per ingested item (mail/Teams/calendar/SharePoint),
                 pre-classification.
thread_map     — deterministic stable_key -> issue_id lookup, so a thread is
                 never re-clustered by an LLM once it's been resolved once.
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
from typing import Any, Optional

from paths import WORKGRAPH_DB, ensure_dirs

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(WORKGRAPH_DB, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


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

            conn.execute("""
                CREATE TABLE IF NOT EXISTS thread_map (
                    stable_key TEXT PRIMARY KEY,
                    issue_id   TEXT NOT NULL
                )
            """)

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

            # 2026-07-31 grouping-narrowing follow-up: workgraph_projects.
            # scored_grouping_decision()'s verdict was computed on every
            # group_issue() call but only ever tallied into outcome counts
            # by batch callers, never persisted per-pair - so there was no
            # way to build a real historical dataset to review scored-model
            # behavior over time, or the adjudicated corpus a real accuracy
            # evaluation needs. Append-only, one row per group_issue() call
            # (not just when a project suggestion/merge results) - the
            # "no_match" rows matter too, since a since-corrected no_match
            # is itself a data point.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shadow_grouping_log (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id             TEXT NOT NULL REFERENCES issues(id),
                    logged_ts            REAL NOT NULL,
                    live_action          TEXT NOT NULL,   -- what group_issue() actually did (auto_merged/suggested/no_match/...)
                    live_signal          TEXT,             -- reference/party/company/topic/precedent/scored/None
                    live_sibling_id      TEXT,
                    scored_verdict       TEXT NOT NULL,   -- scored_grouping_decision()'s verdict, always computed
                    scored_score         REAL NOT NULL,
                    scored_sibling_id    TEXT,
                    scored_signals_json  TEXT NOT NULL    -- matched_signals list, as JSON
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_grouping_log_issue ON shadow_grouping_log(issue_id, logged_ts DESC)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id     TEXT,
                    kind         TEXT NOT NULL CHECK (kind IN ('stale','high_priority_ask','anomaly','stuck_action','unmet_prerequisite')),
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
                            kind         TEXT NOT NULL CHECK (kind IN ('stale','high_priority_ask','anomaly','stuck_action','unmet_prerequisite')),
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

            # Weak-signal candidate merges (e.g. same company guess but no
            # shared party, or same party but different category) - NOT
            # auto-applied, surfaced for confirmation. Strong-signal matches
            # (shared external party + same category) auto-merge directly
            # without a row here - see workgraph_projects.py.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_project_suggestions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id_a  TEXT NOT NULL,
                    issue_id_b  TEXT NOT NULL,
                    reason      TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','rejected')),
                    created_ts  REAL NOT NULL,
                    resolved_ts REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_project_suggestions_status ON pending_project_suggestions(status)")
            try:
                # 2026-07-31 (meeting-grouping/related-project identity pass):
                # every suggestion used to mean "propose a same-project
                # merge" - no way to represent "these are connected but
                # should stay separate projects" (Marc's real example:
                # exiting an IQVIA contract via H1, vs. negotiating a NEW
                # direct H1 deal - causally related, should very likely stay
                # separate). 'link' suggestions (see project_links below)
                # and 'merge_projects' (step 5 - a collision between two
                # ALREADY-established projects) reuse this same queue rather
                # than forking a second review surface/routine doc. No CHECK
                # constraint here (no precedent in this file for ALTER TABLE
                # ADD COLUMN ... CHECK) - valid values enforced in Python at
                # create_project_suggestion, same pattern set_project_status
                # already uses for its own status argument.
                conn.execute("ALTER TABLE pending_project_suggestions ADD COLUMN suggestion_kind TEXT NOT NULL DEFAULT 'merge'")
            except sqlite3.OperationalError:
                pass

            # Phase 0 fix (D2, 2026-08-03): 'expired' is a new resolution for
            # the sweep in expire_stale_project_suggestions - a status VALUE
            # on a column whose CHECK is baked into the original CREATE TABLE
            # (unlike suggestion_kind above, which was added later with no
            # CHECK at all), so this needs the same detect+rebuild migration
            # already used for issues.state (task #44) and alerts.kind (task
            # #55), not a plain ALTER. Only 'merge' suggestions expire -
            # 'link' suggestions are exempt (see expire_stale_project_
            # suggestions' own docstring for why).
            existing_suggestions_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_project_suggestions'"
            ).fetchone()
            if existing_suggestions_sql and "'expired'" not in (existing_suggestions_sql["sql"] or ""):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ALTER TABLE pending_project_suggestions RENAME TO pending_project_suggestions_pre_phase0")
                    conn.execute("""
                        CREATE TABLE pending_project_suggestions (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            issue_id_a      TEXT NOT NULL,
                            issue_id_b      TEXT NOT NULL,
                            reason          TEXT NOT NULL,
                            status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','rejected','expired')),
                            created_ts      REAL NOT NULL,
                            resolved_ts     REAL,
                            suggestion_kind TEXT NOT NULL DEFAULT 'merge'
                        )
                    """)
                    cols = [r["name"] for r in conn.execute("PRAGMA table_info(pending_project_suggestions_pre_phase0)").fetchall()]
                    col_list = ", ".join(cols)
                    conn.execute(f"INSERT INTO pending_project_suggestions ({col_list}) SELECT {col_list} FROM pending_project_suggestions_pre_phase0")
                    conn.execute("DROP TABLE pending_project_suggestions_pre_phase0")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_project_suggestions_status ON pending_project_suggestions(status)")
                    conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
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

            # claim_edges (design doc Section 12.3 / 8.2): the edge types
            # Section 8.2 named back when Phase 3 was built, now real. Only
            # `supersedes` has an actual writer in this pass (touch_claim's
            # existing repeat_signals-driven dedup, Section 9.3) -
            # `contradicts`/`supports` stay schema-ready but empty, same
            # "don't build a producer nothing calls yet" discipline as
            # everything else in this doc: Evidence Assembly's conflict
            # detection (Section 8.1, still "always empty in v0") is the real
            # future producer for those two, not invented here speculatively.
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
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass

            # `issues`/`projects` as views over work_objects - every existing
            # caller across the whole codebase keeps working completely
            # unchanged (confirmed empirically: partial-column INSERT/UPDATE,
            # matching create_issue()/update_issue()'s own exact patterns,
            # both behave identically to a real table). INSTEAD OF triggers
            # make them fully read/write, not just read-only projections.
            conn.execute("""
                CREATE VIEW IF NOT EXISTS issues AS
                SELECT id, title, category, status AS state, priority, priority_score,
                       nba_action_kind, nba_reason, owner, due, opened_at, updated_at,
                       confidence_tier, parent_id AS project_id, lesson_id_cited,
                       has_unmet_prerequisite, claims_revision
                FROM work_objects WHERE object_type = 'request'
            """)
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
                        thread_key_source, is_organizer)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source, stable_key, thread_key, dedupe_key, occurred_ts,
                     subject, from_actor, participants_json, body_preview, raw_ref, time.time(), entry_id,
                     thread_key_source, is_organizer),
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


# --- thread_map ---------------------------------------------------------------

def thread_map_lookup(stable_key: str) -> Optional[str]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT issue_id FROM thread_map WHERE stable_key = ?", (stable_key,)
            ).fetchone()
        finally:
            conn.close()
    return row["issue_id"] if row else None


def thread_map_set(stable_key: str, issue_id: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO thread_map (stable_key, issue_id) VALUES (?, ?)",
                (stable_key, issue_id),
            )
        finally:
            conn.close()


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
    'UNIQUE constraint failed: issues.id'). Max-based is stable under deletion."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT id FROM issues WHERE id LIKE 'marc-%'").fetchall()
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
            return eu_id
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


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
                              "nba_choice_log", "shadow_grouping_log"):
                    conn.execute(f"UPDATE {table} SET issue_id = ? WHERE issue_id = ?", (winner_id, loser_id))
                conn.execute("UPDATE thread_map SET issue_id = ? WHERE issue_id = ?", (winner_id, loser_id))

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


def log_shadow_grouping_decision(*, issue_id: str, live_action: str, live_signal: Optional[str],
                                  live_sibling_id: Optional[str], scored_verdict: str, scored_score: float,
                                  scored_sibling_id: Optional[str], scored_signals_json: str) -> int:
    """One row per group_issue() call - see shadow_grouping_log's own
    CREATE TABLE comment for why this exists (there was previously no
    historical record of the scored model's shadow verdict at all)."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO shadow_grouping_log
                   (issue_id, logged_ts, live_action, live_signal, live_sibling_id,
                    scored_verdict, scored_score, scored_sibling_id, scored_signals_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (issue_id, time.time(), live_action, live_signal, live_sibling_id,
                 scored_verdict, scored_score, scored_sibling_id, scored_signals_json),
            )
            return cur.lastrowid
        finally:
            conn.close()


def list_shadow_grouping_log(*, disagreements_only: bool = False, limit: int = 1000) -> list[dict]:
    """Read-only review surface for shadow_grouping_log. disagreements_only
    restricts to rows where the live (ordered) model and the scored model
    reached a different verdict-shape - the cases actually worth a human
    looking at before ever reconsidering config('grouping',
    'scored_model_enabled')."""
    with _lock:
        conn = _connect()
        try:
            if disagreements_only:
                rows = conn.execute(
                    """SELECT * FROM shadow_grouping_log
                       WHERE (live_action = 'auto_merged') != (scored_verdict = 'auto_merge')
                       ORDER BY logged_ts DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM shadow_grouping_log ORDER BY logged_ts DESC LIMIT ?", (limit,)
                ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


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
    the project level, same reasoning as issues.state's task #44."""
    if status not in ("active", "waiting", "done", "archived", "dismissed"):
        raise ValueError(f"invalid project status: {status!r}")
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
                         (status, time.time(), project_id))
        finally:
            conn.close()


def list_projects(status: Optional[list[str]] = None) -> list[dict]:
    sql = "SELECT * FROM projects"
    args: list[Any] = []
    if status:
        placeholders = ", ".join("?" for _ in status)
        sql += f" WHERE status IN ({placeholders})"
        args.extend(status)
    sql += " ORDER BY updated_at DESC"
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


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


def assign_issue_to_project(issue_id: str, project_id: Optional[str], *, reason: Optional[str] = None) -> None:
    """The one place issue.project_id ever changes - covers auto-grouping,
    a worker splitting/merging on Marc's conversational correction, and
    manual reassignment alike. Logs the transition to audit_log."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT project_id FROM issues WHERE id = ?", (issue_id,)).fetchone()
            old_project_id = row["project_id"] if row else None
            if old_project_id == project_id:
                return
            conn.execute("UPDATE issues SET project_id = ?, updated_at = ? WHERE id = ?", (project_id, time.time(), issue_id))
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
    threading.Lock - re-acquiring it from the same thread would deadlock)."""
    def _query(conn: sqlite3.Connection) -> Optional[dict]:
        row_a = conn.execute("SELECT project_id FROM issues WHERE id = ?", (issue_id_a,)).fetchone()
        row_b = conn.execute("SELECT project_id FROM issues WHERE id = ?", (issue_id_b,)).fetchone()
        project_a = row_a["project_id"] if row_a else None
        project_b = row_b["project_id"] if row_b else None
        if not (project_a and project_b and project_a != project_b):
            return None
        winner, loser = project_a, project_b
        members = conn.execute("SELECT id FROM issues WHERE project_id = ?", (loser,)).fetchall()
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
                members = conn.execute("SELECT id FROM issues WHERE project_id = ?", (project_b,)).fetchall()
                for member in members:
                    conn.execute(
                        "UPDATE issues SET project_id = ?, updated_at = ? WHERE id = ?",
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
    refuses to auto-collapse and creates a 'merge_projects' pending
    suggestion instead. Returns {"status": "merged", "project_id": ...} or
    {"status": "deferred", "suggestion_id": ..., "winner_project_id": ...,
    "loser_project_id": ...} - every caller must check "status" now, not
    assume a bare project_id."""
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            collision = would_collide_established_projects(issue_id_a, issue_id_b, _conn=conn)
            if collision is not None:
                suggestion_id = _create_project_suggestion_on(
                    conn, issue_id_a=issue_id_a, issue_id_b=issue_id_b,
                    reason=(f"{reason_label}: would merge project {collision['loser_project_id']} "
                            f"({len(collision['loser_members'])} other members) into "
                            f"{collision['winner_project_id']} - needs review before collapsing an established project"),
                    suggestion_kind="merge_projects", now=now,
                )
                return {"status": "deferred", "suggestion_id": suggestion_id,
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
                row_a = conn.execute("SELECT project_id FROM issues WHERE id = ?", (issue_id_a,)).fetchone()
                row_b = conn.execute("SELECT project_id FROM issues WHERE id = ?", (issue_id_b,)).fetchone()
                project_a = row_a["project_id"] if row_a else None
                project_b = row_b["project_id"] if row_b else None

                if project_a and project_b and project_a != project_b:
                    winner, loser = project_a, project_b
                    members = conn.execute(
                        "SELECT id FROM issues WHERE project_id = ?", (loser,)
                    ).fetchall()
                    for member in members:
                        member_id = member["id"]
                        if member_id in (issue_id_a, issue_id_b):
                            continue  # reassigned explicitly below either way
                        conn.execute(
                            "UPDATE issues SET project_id = ?, updated_at = ? WHERE id = ?",
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
                    row = conn.execute("SELECT project_id FROM issues WHERE id = ?", (iid,)).fetchone()
                    old_project_id = row["project_id"] if row else None
                    if old_project_id == project_id:
                        continue
                    conn.execute("UPDATE issues SET project_id = ?, updated_at = ? WHERE id = ?", (project_id, now, iid))
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
                return {"status": "merged", "winner_id": winner_id, "loser_id": loser_id,
                        "raw_items_moved": raw_items_moved, "evidence_moved": evidence_moved}
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
        finally:
            conn.close()


# --- pending_project_suggestions -------------------------------------------

_SUGGESTION_KINDS = ("merge", "link", "merge_projects")


def _create_project_suggestion_on(conn: sqlite3.Connection, *, issue_id_a: str, issue_id_b: str,
                                   reason: str, suggestion_kind: str, now: float) -> int:
    """Same dedupe-then-insert logic as create_project_suggestion, against a
    GIVEN connection - for merge_issues_txn's own use (2026-07-31, step 5):
    it already holds _lock for its whole body, so calling back into
    create_project_suggestion (which acquires _lock itself) would deadlock."""
    existing = conn.execute(
        """SELECT id FROM pending_project_suggestions
           WHERE status = 'pending' AND suggestion_kind = ? AND
               ((issue_id_a = ? AND issue_id_b = ?) OR (issue_id_a = ? AND issue_id_b = ?))""",
        (suggestion_kind, issue_id_a, issue_id_b, issue_id_b, issue_id_a),
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        """INSERT INTO pending_project_suggestions (issue_id_a, issue_id_b, reason, created_ts, suggestion_kind)
           VALUES (?, ?, ?, ?, ?)""",
        (issue_id_a, issue_id_b, reason, now, suggestion_kind),
    )
    return cur.lastrowid


def create_project_suggestion(*, issue_id_a: str, issue_id_b: str, reason: str,
                               suggestion_kind: str = "merge") -> int:
    """suggestion_kind added 2026-07-31 - see pending_project_suggestions'
    own schema comment. Dedupe is scoped to the SAME kind: a pending 'merge'
    suggestion for this pair must not be reused for a 'link' suggestion (or
    vice versa) - they're different questions about the same pair, not
    interchangeable rows."""
    if suggestion_kind not in _SUGGESTION_KINDS:
        raise ValueError(f"invalid suggestion_kind: {suggestion_kind!r}")
    with _lock:
        conn = _connect()
        try:
            return _create_project_suggestion_on(
                conn, issue_id_a=issue_id_a, issue_id_b=issue_id_b, reason=reason,
                suggestion_kind=suggestion_kind, now=time.time(),
            )
        finally:
            conn.close()


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


def list_project_suggestions(status: str = "pending") -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM pending_project_suggestions WHERE status = ? ORDER BY created_ts DESC", (status,)
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_project_suggestion(id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM pending_project_suggestions WHERE id = ?", (id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def resolve_project_suggestion(id: int, status: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE pending_project_suggestions SET status = ?, resolved_ts = ? WHERE id = ?",
                (status, time.time(), id),
            )
        finally:
            conn.close()


def expire_stale_project_suggestions(older_than_days: float, kinds: tuple = ("merge",)) -> int:
    """Phase 0 fix (D2, 2026-08-03): the structural guarantee that a stale
    pending queue can't just silently accumulate forever, independent of
    whether same_category_proximity_suggestions_enabled is on. Resolves
    (not deletes - reversible bookkeeping, same as any other resolution) any
    'pending' row of the given suggestion_kind(s) older than the cutoff to
    status='expired'. 'link' suggestions are exempt by default - a
    relationship suggestion doesn't go stale the way a same-project merge
    guess does; Marc or a worker may still want to confirm a 'related'
    connection long after it first surfaced. Returns the number of rows
    resolved."""
    if not kinds:
        return 0
    cutoff = time.time() - older_than_days * 86400
    placeholders = ",".join("?" for _ in kinds)
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                f"""UPDATE pending_project_suggestions
                    SET status = 'expired', resolved_ts = ?
                    WHERE status = 'pending' AND created_ts < ? AND suggestion_kind IN ({placeholders})""",
                (time.time(), cutoff, *kinds),
            )
            return cur.rowcount
        finally:
            conn.close()


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

def create_extraction(raw_item_id: int, extracted_json: str) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO raw_item_extractions (raw_item_id, extracted_json, extracted_ts)
                   VALUES (?, ?, ?)
                   ON CONFLICT(raw_item_id) DO UPDATE SET
                       extracted_json = excluded.extracted_json, extracted_ts = excluded.extracted_ts""",
                (raw_item_id, extracted_json, time.time()),
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


def insert_claim(
    *, issue_id: str, raw_item_id: int, claim_type: str, text: str,
    author: str, author_basis: str, owner: Optional[str] = None,
    date_kind: Optional[str] = None, ts: Optional[float] = None,
) -> int:
    now = ts if ts is not None else time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """INSERT INTO claims
                   (issue_id, raw_item_id, claim_type, text, author, author_basis,
                    owner, date_kind, status, first_seen_ts, last_seen_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (issue_id, raw_item_id, claim_type, text, author, author_basis,
                 owner, date_kind, now, now),
            )
            claim_id = cur.lastrowid
            conn.execute(
                "UPDATE issues SET claims_revision = claims_revision + 1 WHERE id = ?",
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
                conn.execute(
                    "UPDATE issues SET claims_revision = claims_revision + 1 WHERE id = ?",
                    (row["issue_id"],),
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
                conn.execute(
                    "UPDATE issues SET claims_revision = claims_revision + 1 WHERE id = ?",
                    (row["issue_id"],),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def log_claim_event(claim_id: int, event_type: str, *, actor: str, note: Optional[str] = None,
                     ts: Optional[float] = None) -> int:
    """Design doc Section 12.3's right-sized event log - 5 real event types
    (create/escalate/acknowledge/complete/dismiss), each with a real,
    deterministic producer in workgraph_claims.py, not the Blueprint's full
    14-type taxonomy (no current producer for the other 9 - a named,
    deliberate gap, not silently dropped)."""
    now = ts if ts is not None else time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO claim_events (claim_id, event_type, ts, actor, note) VALUES (?, ?, ?, ?, ?)",
                (claim_id, event_type, now, actor, note),
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


def get_claim(claim_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def get_claims_revision(issue_id: str) -> int:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT claims_revision FROM issues WHERE id = ?", (issue_id,)
            ).fetchone()
        finally:
            conn.close()
    return row["claims_revision"] if row else 0


def get_max_claims_revision_for_project(project_id: str) -> int:
    """Project-level revision is derived, not stored (Section 9.5) -
    MAX(claims_revision) across member issues, computed at synthesis-check
    time rather than kept as a second counter in sync with its members."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT MAX(claims_revision) AS m FROM issues WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        finally:
            conn.close()
    return (row["m"] if row and row["m"] is not None else 0)


def list_open_claims_for_issue(issue_id: str, claim_type: Optional[str] = None) -> list[dict]:
    return list_open_claims_for_issues([issue_id], claim_type=claim_type).get(issue_id, [])


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


def search_evidence_fts(query: str, issue_id: Optional[str] = None, limit: int = 50) -> list[dict]:
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
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO attachments
                   (entity_type, entity_id, kind, filename, stored_path, content_type, size_bytes, sha256, uploaded_by, uploaded_ts, extracted_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (entity_type, entity_id, kind, filename, stored_path, content_type, size_bytes, sha256_hex, uploaded_by, time.time(), extracted_text),
            )
            return cur.lastrowid
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
