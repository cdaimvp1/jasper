#!/usr/bin/env python3
"""Cohort F9 notification poller — UNIVERSAL hardened version (v0.2).

ONE shared poller for every cohort worker (replaces per-worker hand-rolled
variants that diverged). Set the worker via argv[1] or $CLAUDE_WORKER:

    python3 -u setup/cohort_f9_poller.py team_builder
    # or: CLAUDE_WORKER=canon_steward python3 -u setup/cohort_f9_poller.py

Per NOTIFICATION_POLLER.md. GET /api/notifications/<worker>?since_id=<cursor>;
advance from data['cursor'].

Two hardenings over the v0.1 16-line reference poller (both from the 2026-06-14
cohort-wide poller-noise incident — George asked TB to converge a universal fix):

1. WALK-TO-TIP SEED. The v0.1 poller starts cursor=0, which replays the entire
   backlog on every arm. The notification `id` is its OWN small counter — NOT
   latest_id (bus-id space). Seeding from latest_id is the trap that makes a
   poller silently deaf (id > latest_id never true). So we walk to the current
   tip silently at startup, then emit only genuinely-new notifications.

2. TRANSIENT-TIMEOUT SUPPRESSION. The v0.1 poller prints [NOTIF-ERR] on EVERY
   exception. On a quiet stretch a healthy poller is otherwise silent, so sparse
   localhost-socket timeouts become the only output — flooding the monitor
   (waking the agent repeatedly AND risking Monitor auto-stop), and looking like
   a broken poller when it isn't. The cursor is PRESERVED across a failed poll,
   so nothing is lost. We only emit [NOTIF-ERR] after ERR_THRESHOLD consecutive
   failures (a real sustained outage), re-log every ERR_RELOG ticks while still
   down, and print [NOTIF-OK] on recovery. Reset on any success.

Skips comms_test (F10 synthetic · substrate-verified via row presence).
"""
import os, sys, json, time, urllib.request

WORKER = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CLAUDE_WORKER", "")).strip()
if not WORKER:
    print("[FATAL] no worker id — pass as argv[1] or set $CLAUDE_WORKER", flush=True)
    sys.exit(2)
# Fence guard (2026-07-16, Atlas · born-context fail-closed — Abe's invert-to-known-live + Sage's home-derived criterion):
# the poller reads its bus from COHORT_BASE. Unset, it would silently default to the LIVE :8675 bus — a fence breach
# for a deployed/born poller (it would poll the live cohort). Key on the KNOWN-LIVE path, NOT a born-path literal:
# only ~/team is the live cohort; ANY other install location (born/deployed/dev) MUST set COHORT_BASE explicitly.
# Casing-proof (no Symphony/SYMPHONY app-support literal → can't re-introduce the item-C casing bug) + SYM_HOME-override-proof.
# home-derived (~/) not hardcoded /Users/<me>/team so it's portable to any new-user box (Sage's cure-B criterion).
BASE = os.environ.get("COHORT_BASE")
if BASE is None:
    _live_root = os.path.realpath(os.path.expanduser("~/team"))
    if not os.path.realpath(__file__).startswith(_live_root + os.sep):
        print("[FATAL] deployed/born poller: COHORT_BASE must be set born-local; refusing to default to the live :8675 bus (fence guard)", flush=True)
        sys.exit(2)
    BASE = "http://localhost:8675"   # true live cohort (poller under ~/team) only — unchanged

ERR_THRESHOLD = 6   # ~30s+ of back-to-back failures before it's a "real" outage
ERR_RELOG = 12      # while still failing, re-log at most every ~60s


def _get(cursor, limit=200):
    url = "%s/api/notifications/%s?since_id=%s&limit=%s" % (BASE, WORKER, cursor, limit)
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


# Seed: walk to current tip WITHOUT emitting (avoid replaying the whole backlog).
cursor = 0
while True:
    try:
        data = _get(cursor)
    except Exception as e:
        print("[NOTIF-ERR seed] %s" % e, flush=True)
        time.sleep(3)
        continue
    ns = data.get("notifications", [])
    newcur = data.get("cursor", cursor)
    if not ns or newcur <= cursor:
        cursor = newcur
        break
    cursor = newcur
print("[F9-ARMED] %s seeded at tip cursor=%s" % (WORKER, cursor), flush=True)

# Real loop: emit only new notifications; suppress transient-timeout noise.
consecutive_err = 0
while True:
    try:
        data = _get(cursor)
        if consecutive_err >= ERR_THRESHOLD:
            print("[NOTIF-OK] poller recovered after %d consecutive timeouts" % consecutive_err, flush=True)
        consecutive_err = 0
        for n in data.get("notifications", []):
            if n.get("kind") == "comms_test":
                continue
            line = "[%s %s] %s: %s" % (n.get("kind", ""), n.get("id", ""), n.get("source", ""), n.get("summary", ""))
            print(line, flush=True)
        cursor = data.get("cursor", cursor)
    except Exception as e:
        consecutive_err += 1
        if consecutive_err == ERR_THRESHOLD or (consecutive_err > ERR_THRESHOLD and consecutive_err % ERR_RELOG == 0):
            print("[NOTIF-ERR] %d consecutive poll failures (sustained) — last: %s" % (consecutive_err, e), flush=True)
    time.sleep(5)
