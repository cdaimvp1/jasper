#!/usr/bin/env python3
"""cohort_f9_wait.py — EXIT-ON-NOTIFICATION bus waiter (in-session hearing for harnesses
with no Monitor tool).

WHY THIS EXISTS (2026-07-27, Tia · proposed to and approved by Marc Lane)
------------------------------------------------------------------------
The wake protocol (CLAUDE.md "HEARING", archetypes/coordinator.md §3) tells the waking worker
to arm a PERSISTENT in-session **Monitor tool** with `cohort_f9_poller.py` as its `command`
parameter. That doctrine assumes the harness EXPOSES such a tool. This Claude Code build does
NOT — there is no Monitor tool in the registry, under that name or any other (checked by name
and by capability at tia's 2026-07-27 wake). So layer-2 hearing was simply unarmable as written,
and the doctrine's own warning applies in full: "poller-process-alive != hearing", "a background
poll can't wake an idle session."

What this harness DOES give: a background task whose EXIT re-invokes the model. So the way to
get real wake-on-message here is to invert the poller's shape — instead of a long-lived process
that PRINTS forever (and therefore never wakes an idle session), run a waiter that BLOCKS until
a genuinely-new notification lands and then EXITS. Exit == wake. The worker re-arms on wake.

This does NOT replace cohort_f9_poller.py. That poller remains layer 1: a durable, detached
process that logs the bus independent of any session. This is layer 2, session-scoped.

CURSOR PERSISTENCE — the load-bearing detail
--------------------------------------------
Because this process EXITS on every hit, a naive re-arm would re-seed to the bus tip and
silently DROP anything that arrived in the gap between exit and re-arm — reintroducing the exact
deafness this is meant to cure, in a harder-to-see form. So the cursor is persisted to a state
file and resumed on the next arm. Only the FIRST ever arm walks to the tip (so a fresh worker
doesn't replay the whole backlog, same reasoning as the poller's walk-to-tip seed).

The notification `id` is its OWN small counter, NOT latest_id (bus-id space) — seeding from
latest_id is the trap that makes a poller permanently deaf (id > latest_id never true). Same
trap, same avoidance, as cohort_f9_poller.py.

EXIT CODES
    0  — either new notifications (printed to stdout) or a clean heartbeat timeout. Both mean
          "wake the worker"; the worker reads stdout to tell which, and re-arms.
    2  — fatal misconfiguration (no worker id, or COHORT_BASE unset off the live path). Do not
          re-arm blindly on 2; fix the config.

USAGE
    python -u cohort_f9_wait.py <worker> [<base>]        # base is inert, for ps-dedup parity
"""
import json
import os
import sys
import time
import urllib.request

WORKER = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CLAUDE_WORKER", "")).strip()
if not WORKER:
    print("[FATAL] no worker id — pass as argv[1] or set $CLAUDE_WORKER", flush=True)
    sys.exit(2)

# Fence guard — IDENTICAL contract to cohort_f9_poller.py (2026-07-16 Atlas/Abe/Sage lineage):
# unset COHORT_BASE would silently default to the LIVE :8675 bus, a fence breach for a born
# install. Key on the KNOWN-LIVE path (~/team), so any other install location MUST set it.
BASE = os.environ.get("COHORT_BASE")
if BASE is None:
    _live_root = os.path.realpath(os.path.expanduser("~/team"))
    if not os.path.realpath(__file__).startswith(_live_root + os.sep):
        print("[FATAL] deployed/born waiter: COHORT_BASE must be set born-local; refusing to "
              "default to the live :8675 bus (fence guard)", flush=True)
        sys.exit(2)
    BASE = "http://localhost:8675"
BASE = BASE.rstrip("/")

POLL_S = 5              # bus poll interval
# Heartbeat exit (30min) — at/above idle_guard.py's ~1200s idle floor, so a quiet bus costs one
# wake per half hour rather than a tick storm.
MAX_WAIT_S = 1800
ERR_THRESHOLD = 6           # ~30s of back-to-back failures before it's a "real" outage
ERR_RELOG = 12

# Cursor state — keyed by worker AND bus, so a sandbox arm can never inherit a live cursor.
_SAFE_BASE = "".join(c if c.isalnum() else "_" for c in BASE)
STATE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "cohort")
STATE_PATH = os.path.join(STATE_DIR, f"{WORKER}_f9_wait_{_SAFE_BASE}.json")


def _get(cursor, limit=200):
    url = f"{BASE}/api/notifications/{WORKER}?since_id={cursor}&limit={limit}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def _load_cursor():
    """Returns (cursor, is_first_arm)."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return int(json.load(f)["cursor"]), False
    except Exception:
        return 0, True   # unreadable/absent/corrupt -> treat as first arm, walk to tip


def _save_cursor(cursor):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cursor": int(cursor), "worker": WORKER, "base": BASE,
                       "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
        os.replace(tmp, STATE_PATH)   # atomic — a killed waiter can't leave a torn cursor
    except Exception as e:
        # Non-fatal: a lost cursor costs a re-seed, not correctness of THIS wake.
        print(f"[WAIT-WARN] could not persist cursor: {e}", flush=True)


cursor, first_arm = _load_cursor()

if first_arm:
    # Walk to the current tip WITHOUT emitting, so a fresh worker doesn't replay the backlog.
    while True:
        try:
            data = _get(cursor)
        except Exception as e:
            print(f"[WAIT-ERR seed] {e}", flush=True)
            time.sleep(3)
            continue
        ns = data.get("notifications", [])
        newcur = data.get("cursor", cursor)
        if not ns or newcur <= cursor:
            cursor = newcur
            break
        cursor = newcur
    _save_cursor(cursor)
    print(f"[F9-WAIT-ARMED] {WORKER} first arm, seeded at tip cursor={cursor} bus={BASE}", flush=True)
else:
    print(f"[F9-WAIT-ARMED] {WORKER} resumed at cursor={cursor} bus={BASE}", flush=True)

started = time.monotonic()
consecutive_err = 0

while True:
    try:
        data = _get(cursor)
        if consecutive_err >= ERR_THRESHOLD:
            print(f"[WAIT-OK] recovered after {consecutive_err} consecutive timeouts", flush=True)
        consecutive_err = 0

        # comms_test (F10 synthetic) is substrate-verified by row presence — never a wake reason.
        fresh = [n for n in data.get("notifications", []) if n.get("kind") != "comms_test"]
        newcur = data.get("cursor", cursor)

        if fresh:
            # Advance and PERSIST before exiting, so the next arm cannot re-deliver these.
            _save_cursor(newcur)
            print(f"[F9-WAKE] {len(fresh)} new notification(s) for {WORKER}:", flush=True)
            for n in fresh:
                print("  [{} {}] {}: {}".format(n.get("kind", ""), n.get("id", ""),
                                                n.get("source", ""), n.get("summary", "")),
                      flush=True)
            print("[F9-WAKE-END] re-arm the waiter to keep hearing. Full bodies: "
                  f"{BASE}/api/team_room (never hand-write SQL against bus.db).", flush=True)
            sys.exit(0)

        # No wake-worthy notifications, but the cursor may still have moved (e.g. a skipped
        # comms_test). Persist it so those are not re-examined on the next arm.
        if newcur != cursor:
            cursor = newcur
            _save_cursor(cursor)

    except Exception as e:
        consecutive_err += 1
        if consecutive_err == ERR_THRESHOLD or (
                consecutive_err > ERR_THRESHOLD and consecutive_err % ERR_RELOG == 0):
            print(f"[WAIT-ERR] {consecutive_err} consecutive poll failures (sustained) — "
                  f"last: {e}", flush=True)

    if time.monotonic() - started >= MAX_WAIT_S:
        print(f"[F9-QUIET] no new notifications in {MAX_WAIT_S}s — heartbeat exit at "
              f"cursor={cursor}. Bus was reachable: {consecutive_err == 0}. Re-arm to keep hearing.",
              flush=True)
        sys.exit(0)

    time.sleep(POLL_S)
