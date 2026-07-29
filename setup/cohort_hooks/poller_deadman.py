#!/usr/bin/env python3
"""poller_deadman.py (BORN PORT) — UserPromptSubmit hook · poller dead-man switch, self-healing.

Ported from aria_sync/tools/cohort_hooks/poller_deadman.py (Sage, growth-infra Mechanism C).
Same failure class this protects against: a worker's F9 notification poller dies silently
mid-session; deafness looks identical to calm — the exact scar this whole hook exists to kill
(NOTIFICATION_POLLER.md v0.3, our dev-cohort's 2.2h-4.5h-deaf incidents).

BORN ADAPTATION (the only real change from the dev-cohort original): the dev version resolves
"which worker is this session" via a static per-worker-name dict (DEADMAN_PATTERNS) + a
dev-cohort session-map file (/tmp/cohort_session_map.json) — both are dev-cohort-specific and
don't generalize to arbitrary born slot-ids (a born roster names workers "Nora"/"Sam"/whatever
the operator picked, not a fixed known set). Born workers ALREADY have their worker identity in
env at launch (SYMPHONY_WORKER, set by symphony_wake.sh before exec claude) — so this port reads
that directly instead of a lookup table. Everything else (self-heal via poller_autostart,
bus-aware pattern, always-exit-0 safety contract) is unchanged.

SAFETY CONTRACT (house style, unchanged): ALWAYS exit 0. No warning is ever worth blocking a
prompt. Missing SYMPHONY_WORKER (non-cohort session) = silent no-op.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).parent))
    from poller_autostart import ensure_poller_alive, _is_process_running
except Exception:
    ensure_poller_alive = None  # self-heal degrades to warn-only if this ever fails to import
    _is_process_running = None


def main() -> int:
    try:
        try:
            _ = json.loads(sys.stdin.read() or "{}")  # payload unused (born worker known via env, not session-map)
        except Exception:
            pass

        worker = os.environ.get("SYMPHONY_WORKER", "")
        if not worker:
            return 0  # not a cohort worker process (SYMPHONY_WORKER unset) — no-op

        base = os.environ.get("COHORT_BASE", "")
        # bus-aware pattern (matches poller_autostart's own launch argv shape): the poller
        # process command-line includes worker + base so a stray same-worker poller on a
        # DIFFERENT bus (e.g. live vs sandbox) doesn't false-positive as "alive".
        pattern = f"cohort_f9_poller.py {worker}" + (f" {base}" if base else "")

        # cross-platform process check (POSIX pgrep / Windows Get-CimInstance) — see
        # poller_autostart._is_process_running for the platform branch; a bare "pgrep"
        # here would hard-fail on Windows (no such command), same class Abe fixed
        # for the stale-server-kill via Get-NetTCPConnection.
        alive = _is_process_running(pattern) if _is_process_running else False
        if not alive:  # no matching live process
            outcome = ensure_poller_alive(worker, pattern) if ensure_poller_alive else "error:autostart unavailable"
            if outcome == "relaunched":
                print(
                    f"🔧 [DEADMAN-SELFHEAL] {worker}: your notification poller (pattern "
                    f"'{pattern}') had NO live process — auto-relaunched it just now. "
                    f"Self-test with a same-bus inbound before trusting it (self-DMs never echo). "
                    f"NOTE this heals layer 1 (the logging PROCESS) only — it does NOT restore "
                    f"in-session hearing. Re-arm that yourself (Monitor tool, or cohort_f9_wait.py "
                    f"as a background task where Monitor is absent)."
                )
            else:
                print(
                    f"🚨 [DEADMAN] {worker}: your notification poller (pattern "
                    f"'{pattern}') has NO live process, and auto-relaunch failed "
                    f"({outcome}). You are DEAF to the bus right now — re-arm the Monitor "
                    f"tool per your §0 wake protocol (or cohort_f9_wait.py as a background task "
                    f"if ToolSearch confirms Monitor is absent). Deafness looks like calm."
                )
    except Exception:
        pass  # never block a prompt
    return 0


if __name__ == "__main__":
    sys.exit(main())
