"""
bus.py — append-only event bus backed by SQLite WAL.

Same shape as v1 (proven). Every observation, action, intent, decision
becomes a row: (id, ts, source, kind, actor, target, payload).

The dashboard reads this; routers/dispatchers/schedulers subscribe to it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from paths import BUS_DB, ensure_dirs

_lock = threading.Lock()
_subscribers: list = []  # in-process callbacks: fn(event_dict) -> None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(BUS_DB, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_bus() -> None:
    ensure_dirs()
    with _lock:
        conn = _connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts      REAL    NOT NULL,
                    source  TEXT    NOT NULL,
                    kind    TEXT    NOT NULL,
                    actor   TEXT,
                    target  TEXT,
                    payload TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor, ts DESC)")

            # F9 unified notification stream — substrate fans bus events into per-worker rows.
            # One poll endpoint per worker replaces 4 brittle per-channel monitors.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS worker_notifications (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          REAL    NOT NULL,
                    recipient   TEXT    NOT NULL,
                    kind        TEXT    NOT NULL,
                    source      TEXT    NOT NULL,
                    summary     TEXT,
                    event_id    INTEGER,
                    payload_ref TEXT,
                    UNIQUE(recipient, event_id, kind)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notif_recipient_id ON worker_notifications(recipient, id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notif_ts ON worker_notifications(ts DESC)")

            # Anthropic-Teams TIER-1 #3 · 2026-05-19 tr_61bb80837d · plan-approval gate
            # Author drafts plan · reviewer (cross-lane) approves · downstream code
            # (canon writes, ship scripts) refuses to proceed without matching approval.
            # Replaces dry-run+sig convention with substrate-enforced primitive.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cohort_plans (
                    plan_id        TEXT    PRIMARY KEY,
                    author         TEXT    NOT NULL,
                    target_reviewer TEXT,
                    action_kind    TEXT    NOT NULL,
                    summary        TEXT    NOT NULL,
                    anchor         TEXT,
                    draft_ts       REAL    NOT NULL,
                    approved_by    TEXT,
                    approved_ts    REAL,
                    feedback       TEXT,
                    expires_ts     REAL    NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_author ON cohort_plans(author, draft_ts DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_expires ON cohort_plans(expires_ts)")
        finally:
            conn.close()


# --- Symphony dual-write (Stage A.2 step-4): additive event-log mirror of emit_event.
# Fail-disabled: if symphony_bus can't import, _DUALWRITE stays False and bus.py is
# unchanged. Lazy EventLog init avoids a 112k-file glob at import time.
import os as _os
import sys as _sys
import team_paths as _tp
# Re-backfill barrier (c-lite): when this flag file exists, the dual-write append is
# suppressed (checked INSIDE _lock → true barrier once set + one lock-cycle passes),
# while SQLite keeps taking every write (zero comms outage). Touch to quiesce, remove
# to resume. Local path (not synced).
_DUALWRITE_PAUSE = _tp.runtime("DUALWRITE_PAUSE")
try:
    _bus_dir = _tp.bus_dir()
    if _bus_dir not in _sys.path:
        _sys.path.insert(0, _bus_dir)
    from event_log_core import EventLog as _EventLog
    from event_log import (
        make_event as _make_event,
        assign_event_id as _assign_eid,
        default_provenance as _default_prov,
    )
    # SAME classifier the backfill used — CB's promoted shared module classify.py
    # (single source of truth, imported by both backfill and this runtime hook, no
    # drift). Dual-write scope == backfill scope: F_STORED only. TELEMETRY/NOISE
    # carved, F_PROJECTION (inbox.sent fan-out / substrate.stale_task) skipped — only
    # the source F_STORED event is stored; recipients/stale-state reconstruct at read.
    from classify import classify as _classify
    _event_log = None

    def _get_event_log():
        global _event_log
        if _event_log is None:
            # A.3 untether: event-log root is env-driven (single source: _EL_ROOT,
            # defined below off SYMPHONY_EL_ROOT). tmp_dir stays LOCAL by contract
            # (DuckDB temp + runtime never sync to SP).
            # A.3 P2 dual-write (TB pp_1d0285dbe8): if SYMPHONY_EL_MIRROR_ROOT is set,
            # each new event is published to BOTH _EL_ROOT (local primary) AND the mirror
            # root's events/ — same seq (shared local .seq.lock), no divergence. Reads stay
            # LOCAL-primary; only writes fan out. Unset (today) = single-root, behavior-
            # neutral. The SP write-target is configured by this ONE env (togglable: TB
            # sets it at the enable-bounce, unsets to disable; no code change to flip).
            _mirror = _os.environ.get("SYMPHONY_EL_MIRROR_ROOT")
            _event_log = _EventLog(
                _EL_ROOT,
                tmp_dir=_tp.runtime("bus_tmp"),
                # .seq.lock pinned LOCAL (A.3 P0, TB catch): must NOT follow _EL_ROOT
                # to SP at the P4 flip (G4 A·purity + D·counter-local + CS check (3)).
                lock_dir=_tp.runtime(),
                mirror_roots=([_mirror] if _mirror else None),
            )
        return _event_log

    # Process-level barrier (TB's chosen quiesce for the re-backfill): bounce with
    # SYMPHONY_DUALWRITE=0 → the OFF process has NO append path at all (mooting any
    # in-flight-drain / per-event-syscall concern); bounce without it (default ON) to
    # resume. SQLite write path is independent of this gate.
    _DUALWRITE = (_os.environ.get("SYMPHONY_DUALWRITE", "1") != "0")
except Exception:
    _DUALWRITE = False


# --- A.3 fail-closed coupling guard (CS-designed pp_63bce91211, TB-assigned pp_4a48670db2).
# ONE shared source — runtime_env.py — imported by BOTH writers (here + compact()), no
# mirror-drift. Called at module boot OUTSIDE the dual-write try ABOVE so a coupling
# violation propagates LOUDLY (refuse to start) instead of being swallowed into
# _DUALWRITE=False. No-op in local mode (SYMPHONY_EL_ROOT unset) = today's behavior.
# (AB's .seq.lock + tmp_dir are hardcoded LOCAL, not env-keyed, so they're safe by
# construction regardless; the guard enforces SYMPHONY_COMPACT_DIR for CB's marker/temp.)
try:
    from runtime_env import require_local_runtime_if_sp_mode as _require_local_runtime
except Exception:
    def _require_local_runtime():
        # Guard module unavailable: in SP mode we cannot verify the coupling -> fail closed.
        if _os.environ.get("SYMPHONY_EL_ROOT"):
            raise RuntimeError(
                "A.3 SP mode (SYMPHONY_EL_ROOT set) but runtime_env coupling guard is "
                "unavailable -- refusing to start (cannot verify local-runtime coupling).")
_require_local_runtime()


# --- Symphony READ-CUTOVER (R1, AB task #687): route F_STORED-scoped reads through
# the event-log DuckDB projection behind the SYMPHONY_READ file-flag. Additive +
# fail-safe by construction:
#   * flag ABSENT (default)        -> SQLite path, byte-for-byte unchanged.
#   * flag PRESENT + F_STORED query -> event-log reader (CB's `bus` view); on ANY
#                                      error (missing dep, reader fault) -> SQLite.
#   * flag PRESENT + non-F_STORED   -> SQLite (telemetry/noise/F_PROJECTION/unfiltered
#                                      are ephemeral-inclusive; is_fstored_query gates).
# Flip = `touch <flag>` (no bounce); rollback = `rm <flag>`. ~1s TTL stat-cache so a
# flip is picked up live by the running server without a restart.
_SYMPHONY_READ_FLAG = _tp.runtime("SYMPHONY_READ")
# A.3 untether (TB master-seq P0): event-log root is env-driven off SYMPHONY_EL_ROOT,
# single source for BOTH the read root (tail-only reader, below) and the dual-write
# append root (_get_event_log, above). Behavior-NEUTRAL until the env is set — default
# is the current LOCAL path, so a staged deploy changes nothing. P4 cutover = set
# SYMPHONY_EL_ROOT -> SP path (reversible: unset/revert -> back to local, known-good).
_EL_ROOT = _os.environ.get(
    "SYMPHONY_EL_ROOT", _tp.data("symphony_event_log")
)
_el_reader = None
_el_reader_lock = threading.Lock()   # DuckDB conn is not concurrency-safe; serialize reads
_flag_cache = {"val": False, "ts": 0.0}
try:
    from symphony_read import (
        query_events as _el_query_events,
        is_fstored_query as _is_fstored_query,
    )
    import compact as _compact
    _READ_CUTOVER_AVAILABLE = True
except Exception:
    _READ_CUTOVER_AVAILABLE = False


def _symphony_read_on() -> bool:
    """File-flag check with a ~1s TTL cache (one stat/sec, not per-read)."""
    now = time.time()
    if now - _flag_cache["ts"] > 1.0:
        try:
            _flag_cache["val"] = _os.path.exists(_SYMPHONY_READ_FLAG)
        except Exception:
            _flag_cache["val"] = False
        _flag_cache["ts"] = now
    return _flag_cache["val"]


def _get_el_reader():
    """Lazy, cached full-glob reader (hot JSONL ∪ cold parquet) — the correctness
    fallback path. The view re-globs at query time, so a long-lived conn sees fresh
    appends. Used only if the tail-only path errors."""
    global _el_reader
    if _el_reader is None:
        _el_reader = _compact.reader(_EL_ROOT)
    return _el_reader


# --- tail-only read (R1 perf, AB seam pp_4902180039 over CB's seq/ts-floor primitive).
# Common /api reads are cursor/limit-scoped, so we hand read_json only the relevant tail
# instead of all ~12k hot files (385ms -> ~39ms). Correctness = SUPERSET-OR-BUST: the
# window must contain every hot row the full-glob would, then the existing since_*/limit
# logic trims identically -> served bytes unchanged (CS G2-SERVER parity gate is the backstop).
_EL_START_WINDOW = 400    # initial hot-file window (files == events; one-event-per-file).
                          # Small: a recent since_id poll (the common /api path) spans the
                          # cursor in one read (~40ms, CB's max-400); deep/no-cursor reads
                          # widen geometrically below.
_EL_WIDEN_FACTOR = 4      # geometric widen so sparse-kind / deep reads converge in few steps


def _el_hot_seq_bounds():
    """(max_seq, min_seq) over hot filenames — pure listdir+parse, no file open."""
    files = _compact.select_hot_files(_EL_ROOT)   # sorted ascending by name (== seq)
    if not files:
        return None, None
    def _seq(p):
        try:
            return int(_os.path.basename(p).split("_", 1)[0])
        except (ValueError, IndexError):
            return 0
    return _seq(files[-1]), _seq(files[0])


def _el_query_tail(since_ts=None, since_id=None, kind=None, kind_prefix=None,
                   actor=None, target=None, limit=200):
    """Cursor-aware event-log read. since_ts -> exact ts-floor (one read, complete).
    Otherwise a seq-window that widens-on-short until `limit` matching rows are found OR
    the whole hot tier is included (== full-glob, provably complete). Cold parquet is
    always whole inside CB's reader, so historical/straddle rows are never missed."""
    if since_ts is not None:
        # ts-floor is EXACT + complete (filename ts == event ts): no widen needed.
        con = _compact.reader(_EL_ROOT, min_ts=since_ts)
        return _el_query_events(con, since_ts=since_ts, since_id=since_id, kind=kind,
                                kind_prefix=kind_prefix, actor=actor, target=target, limit=limit)
    max_seq, min_hot_seq = _el_hot_seq_bounds()
    if max_seq is None:
        # no hot files -> cold-only (min_seq set forces the pruned path; hot empty, cold whole)
        con = _compact.reader(_EL_ROOT, min_seq=1)
        return _el_query_events(con, since_ts=since_ts, since_id=since_id, kind=kind,
                                kind_prefix=kind_prefix, actor=actor, target=target, limit=limit)
    window = _EL_START_WINDOW
    while True:
        floor = max(min_hot_seq, max_seq - window + 1)
        con = _compact.reader(_EL_ROOT, min_seq=floor)
        rows, min_ret_seq = _el_query_events(
            con, since_ts=since_ts, since_id=since_id, kind=kind, kind_prefix=kind_prefix,
            actor=actor, target=target, limit=limit, _return_min_seq=True)
        if floor <= min_hot_seq:
            return rows                       # whole hot tier in scope -> == full-glob, complete
        if since_id is not None:
            # since_id query: rows older than the cursor are filtered out, so the window is
            # complete once it spans the cursor — i.e. the floor row's id <= since_id (every
            # still-newer matching row is at seq >= floor). min(sqlite_id) over seq>=floor is
            # exactly the floor row's id (cold has seq < min_hot_seq <= floor, excluded).
            floor_id = con.execute(
                "SELECT min(sqlite_id) FROM bus WHERE seq >= ?", [floor]).fetchone()[0]
            if floor_id is not None and floor_id <= since_id:
                return rows
        else:
            # no cursor: need the top-`limit` by seq. Complete iff all returned rows sit at or
            # above the floor (min_ret_seq >= floor) AND we filled the limit — otherwise a
            # below-floor (cold) row was pulled in while excluded middle-hot rows outrank it.
            if len(rows) >= limit and min_ret_seq is not None and min_ret_seq >= floor:
                return rows
        window *= _EL_WIDEN_FACTOR


def emit_event(
    source: str,
    kind: str,
    actor: Optional[str] = None,
    target: Optional[str] = None,
    payload: Optional[dict] = None,
) -> int:
    ts = time.time()
    payload_json = json.dumps(payload or {}, ensure_ascii=False, default=str)
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO events (ts, source, kind, actor, target, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, source, kind, actor, target, payload_json),
            )
            event_id = cur.lastrowid
            # dual-write INSIDE the lock so event-log seq is co-monotonic with the
            # SQLite id (preserves Sage's order-parity); log-but-never-raise so an
            # event-log failure can never regress the live SQLite write.
            if _DUALWRITE and not _os.path.exists(_DUALWRITE_PAUSE):
                try:
                    # dual-write scope == backfill scope: F_STORED only.
                    # TELEMETRY/NOISE carved; F_PROJECTION (inbox.sent fan-out,
                    # substrate.stale_task) skipped — reconstruct at read, not stored.
                    # _classify raises on an unbucketed kind → caught below → skipped.
                    if _classify(kind) == "F_STORED":
                        _eid = _assign_eid(kind, payload or {}, event_id)
                        _ev = _make_event(
                            kind=kind, actor=actor, target=target,
                            payload=payload or {}, ts=ts, event_id=_eid,
                            provenance=_default_prov(
                                actor,
                                source=("worker:%s" % actor) if actor else ("system:%s" % source)),
                            # F2 (AB read-path parity): carry the raw SQLite id + source
                            # verbatim so /api/events `id`/`source` reconstruct byte-exact
                            # for NEW live events (CB's backfill does the same for legacy).
                            # event_id local var here == cur.lastrowid == the SQLite id.
                            sqlite_id=event_id, sqlite_source=source,
                        )
                        _get_event_log().append(_ev)
                except Exception:
                    pass
        finally:
            conn.close()

    event = {
        "id": event_id, "ts": ts, "source": source, "kind": kind,
        "actor": actor, "target": target, "payload": payload or {},
    }
    for cb in list(_subscribers):
        try: cb(event)
        except Exception: pass
    return event_id


def subscribe(callback) -> None:
    _subscribers.append(callback)


def unsubscribe(callback) -> None:
    try: _subscribers.remove(callback)
    except ValueError: pass


def query_events(
    since_ts: Optional[float] = None,
    since_id: Optional[int] = None,
    kind: Optional[str] = None,
    kind_prefix: Optional[str] = None,
    actor: Optional[str] = None,
    target: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    # READ-CUTOVER (R1): F_STORED-scoped reads → event-log projection when the flag is
    # on. Fail-safe: any reader fault falls through to the SQLite path below, so reads
    # can never regress. Non-F_STORED / unfiltered queries never enter here.
    if (_READ_CUTOVER_AVAILABLE and _symphony_read_on()
            and _is_fstored_query(kind, kind_prefix)):
        try:
            return _el_query_tail(
                since_ts=since_ts, since_id=since_id, kind=kind,
                kind_prefix=kind_prefix, actor=actor, target=target, limit=limit)
        except Exception:
            pass  # fall through to SQLite — never regress the read path

    where, args = [], []
    if since_ts is not None: where.append("ts > ?"); args.append(since_ts)
    if since_id is not None: where.append("id > ?"); args.append(since_id)
    if kind is not None: where.append("kind = ?"); args.append(kind)
    if kind_prefix is not None: where.append("kind LIKE ?"); args.append(kind_prefix + "%")
    if actor is not None: where.append("actor = ?"); args.append(actor)
    if target is not None: where.append("target = ?"); args.append(target)
    sql = "SELECT id, ts, source, kind, actor, target, payload FROM events"
    if where: sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)

    with _lock:
        conn = _connect()
        try: rows = conn.execute(sql, args).fetchall()
        finally: conn.close()
    out = []
    for r in rows:
        try: payload = json.loads(r["payload"]) if r["payload"] else {}
        except Exception: payload = {"_raw": r["payload"]}
        out.append({
            "id": r["id"], "ts": r["ts"], "source": r["source"], "kind": r["kind"],
            "actor": r["actor"], "target": r["target"], "payload": payload,
        })
    return out


def event_count() -> int:
    with _lock:
        conn = _connect()
        try: return conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        finally: conn.close()


def latest_id() -> int:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT MAX(id) AS m FROM events").fetchone()
            return int(row["m"] or 0)
        finally: conn.close()
