#!/usr/bin/env python3
"""poller_autostart.py — shared self-heal logic for the F9 notification poller.

2026-07-05 origin: George's OOM-reboot incident exposed that no cohort worker's
poller/monitor auto-starts on a restart — re-arming has always been a MANUAL
step in each worker's own wake sequence (P.4 violation: relies on the worker
remembering). Same night, Quinn and Mira both independently hit exactly this —
each trusted a pre-compact poller as still-alive without re-verifying after
their restart. poller_deadman.py already WARNS on every UserPromptSubmit when
a worker's poller is dead; this module upgrades that from warn-only to
self-healing — ensure_poller_alive() actually relaunches it.

Two call sites, two activation timelines (hooks are launch-read — new
registrations only arm on next restart; editing an ALREADY-registered
script's logic takes effect on its very next fire, no restart needed):
  - poller_deadman.py (UserPromptSubmit, already registered, no matcher) —
    calling this makes the existing warn-on-every-prompt mechanism self-heal
    LIVE, immediately, for any worker already running.
  - sessionstart_resume.py (SessionStart, matcher="resume" — NEW registration
    this same night) — calls this once at the top of a genuine process
    restart, before the worker's first turn. Only takes effect on each
    worker's NEXT restart after settings.json is re-read.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Machine-independence (Abe 2026-07-11, M1-core cross-machine): resolve the poller
# script off TEAM_SCRIPTS_ROOT (the installer sets it on the target; ~/team/setup is
# the origin-Mac default). The old hardcoded origin path would wake a worker DEAF on
# another box (can't find the poller → no notifications). LOG_DIR uses the platform
# temp dir (Windows %TEMP% / POSIX /tmp) instead of a hardcoded /tmp.
_SCRIPTS_ROOT = Path(os.environ.get("TEAM_SCRIPTS_ROOT", str(Path.home() / "team" / "setup")))
POLLER_SCRIPT = str(_SCRIPTS_ROOT / "cohort_f9_poller.py")
LOG_DIR = Path(tempfile.gettempdir())


def _is_process_running(pattern: str) -> bool:
    """Cross-platform 'is any process command-line matching pattern alive' check.
    POSIX: pgrep -f (native). Windows: no pgrep — use PowerShell's Get-CimInstance
    Win32_Process (native, no new pip dependency, same shape as Abe's
    Get-NetTCPConnection-vs-lsof split for the stale-server-kill)."""
    if sys.platform == "win32":
        # SELF-MATCH + QUOTING FIX (2026-07-27, Tia · proposed to and approved by Marc Lane).
        # Two independent bugs, both in this one query, both making it report "alive" for a
        # poller that is dead or absent:
        #
        # (1) SELF-MATCH. The old query enumerated EVERY process and matched CommandLine against
        #     the pattern — but the querying powershell.exe's OWN command line CONTAINS that
        #     pattern (it is embedded in the -Command string it was launched with). So the query
        #     always matched ITSELF and this function always returned True. Consequence:
        #     ensure_poller_alive() never relaunched, and poller_deadman.py (same helper) never
        #     warned — a DEAD poller reported healthy on every prompt, indefinitely.
        #     Live scar: tia's poller died 2026-07-24 and went unnoticed for 3 days while the
        #     dead-man switch said "alive". That is precisely the "deafness looks identical to
        #     calm" failure the deadman exists to kill — defeated by its own liveness check.
        #     Fix: scope to python* processes (a poller can only BE one; this excludes the
        #     powershell.exe/bash.exe wrappers that merely carry the pattern as an argument),
        #     and exclude the querying process's own PID as a second belt.
        #
        # (2) QUOTING. Callers pass a SPACE-JOINED pattern ("cohort_f9_poller.py <worker> <base>"),
        #     but a real poller command line quotes the script path:
        #         "...\\python.exe" -u "...\\cohort_f9_poller.py" tia http://localhost:8700
        #     The closing quote sits between ".py" and " tia", so the contiguous substring never
        #     matches a poller launched with a quoted path (any WMI/cmd-wrapped launch). Fix:
        #     split the pattern on whitespace and require ALL tokens present (-and). This is
        #     quote-agnostic and preserves the existing caller contract exactly — the bus-aware
        #     dedup still works, because the base token is still required.
        #
        # The POSIX branch is deliberately UNTOUCHED: pgrep excludes itself and matches a regex
        # across the whole command line, so neither bug exists there and the origin Mac keeps
        # byte-identical behavior.
        # (3) CALLER-CHAIN SELF-MATCH. Scoping to python* fixed the powershell self-match but left
        #     the SAME bug one level up: the CALLING python process can itself carry the pattern on
        #     its command line (e.g. `python -c "...ensure_poller_alive(..., 'cohort_f9_poller.py
        #     tia <base>')"`), and it IS a python* process. Excluding os.getpid() alone was still
        #     not enough — a venv `Scripts/python.exe` is a shim that re-execs the base interpreter
        #     with the SAME command line, so the caller appears TWICE (parent shim + real child;
        #     the poller itself shows as such a pair). Both bogus-pattern control tests returned
        #     True until the whole ancestor chain was excluded.
        #     Caught live 2026-07-27 by this fix's own control tests — a deliberately bogus pattern
        #     reporting "alive" is the same false-positive class this entire change exists to kill,
        #     so it is verified with negative cases, not just a positive one.
        #     Excluding ancestors is safe: a genuine poller is never an ancestor of its own checker.
        # (4) WORKER-NAME PREFIX COLLISION — a MULTI-WORKER bug, invisible on a one-worker roster.
        #     Naive substring matching per token is not enough: for workers "sam" and "samantha",
        #     the token "sam" occurs INSIDE "samantha", so sam's liveness check is satisfied by
        #     samantha's poller. sam then wakes DEAF while the dead-man switch reports healthy —
        #     the same false-"alive" class as bug (1), reached from a different direction.
        #     Reproduced live 2026-07-27 (tia vs a decoy "tiax": tia's poller killed, check still
        #     said alive). NOTE this was a REGRESSION introduced by the token-split in (2) — the
        #     original contiguous match happened to be immune, because "…poller.py sam <base>"
        #     does not occur in samantha's command line. The token split fixed quoting and broke
        #     this; both are needed, so match tokens with DELIMITERS instead of bare substrings.
        #
        #     Delimiting rules (asymmetric, and the asymmetry is load-bearing):
        #       right side — must be whitespace, a double-quote, or end-of-string. This alone kills
        #                    the PREFIX collision (the "tia" inside "tiax" is followed by "x").
        #       left side  — whitespace, double-quote, start-of-string, OR a path separator. The
        #                    path separator is what keeps the script token matchable: the command
        #                    line holds "...\\cohort_f9_poller.py", so that token is preceded by a
        #                    backslash, not a space. Left-delimiting also kills the SUFFIX
        #                    collision (worker "ia" must not match "tia").
        #     Tokens are escaped via [regex]::Escape so a worker/base containing regex metacharacters
        #     (the base always contains ":" and "/") can never alter the pattern's meaning.
        #     Caveat kept deliberately: PowerShell -match is case-INSENSITIVE, so "Sam" and "sam"
        #     are treated as the same worker. That matches the cohort's case-normalization posture
        #     (a display-name differing only by case is the same slot), but it is a real constraint
        #     on roster naming — two workers must never differ by case alone.
        _tokens = [t for t in pattern.split() if t]
        if not _tokens:
            return False
        _conds = " -and ".join(
            "$_.CommandLine -match ('(^|[\\s\"\\\\/])' + [regex]::Escape('{}') + '([\\s\"]|$)')"
            .format(t.replace("'", "''"))
            for t in _tokens
        )
        # ONE CIM enumeration, then walk the parent chain IN MEMORY. The first cut of this fix
        # issued a Get-CimInstance per ancestor (up to 24) and blew the 10s subprocess timeout —
        # which matters doubly because poller_deadman.py runs on EVERY UserPromptSubmit with a 15s
        # hook budget, so a slow check would itself become the outage. Single-pass keeps it ~1s.
        ps_cmd = (
            "$all=Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId,Name,CommandLine; "
            "$map=@{{}}; foreach($p in $all){{ $map[[int]$p.ProcessId]=$p }}; "
            "$excl=@{{}}; foreach($s in @($PID,{})){{ $c=[int]$s; for($i=0;$i -lt 24;$i++){{ "
            "if($c -eq 0 -or -not $map.ContainsKey($c)){{break}}; $excl[$c]=$true; "
            "$c=[int]$map[$c].ParentProcessId }} }}; "
            "@($all | Where-Object {{ $_.Name -like 'python*' "
            "-and -not $excl.ContainsKey([int]$_.ProcessId) -and {} }}).Count"
            .format(os.getpid(), _conds)
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
        )
        # @(...).Count always prints an integer (0 for no match) — unlike the old .ProcessId,
        # which printed empty for 0, a bare scalar for 1, and a list for many.
        return r.returncode == 0 and r.stdout.strip() not in ("", "0")
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
    return r.returncode == 0 and r.stdout.strip() != ""


def ensure_poller_alive(worker: str, pattern: str) -> str:
    """Returns 'alive' (already running), 'relaunched' (was dead, now started),
    or 'error:<msg>' (process-check/launch itself failed — caller should still exit 0)."""
    try:
        if _is_process_running(pattern):
            return "alive"
    except Exception as e:
        return f"error:process-check failed: {e}"

    try:
        log_path = LOG_DIR / f"{worker}_f9_poller.log"
        # BUS-AWARE dedup (2026-07-12, Mira's catch): the poller reads its bus from COHORT_BASE (env),
        # but pgrep only sees the command line — so a worker-name-only pattern can't tell a live :8675
        # poller from a sandbox :8676 one, and the live one blocks the sandbox arm (the 7→7000
        # collision). Append COHORT_BASE as a trailing argv token: cohort_f9_poller.py reads only
        # argv[1] (the worker) and ignores extras, so this is inert to the poller but makes the process
        # command-line bus-distinguishable. The CALLER's pgrep `pattern` should include the same base
        # (e.g. "cohort_f9_poller.py <worker> <base>") so dedup is scoped to THIS bus.
        base = os.environ.get("COHORT_BASE", "")
        # sys.executable, not a hardcoded "python3" — always resolves to the exact
        # interpreter running THIS script (the venv python), correct on both platforms.
        # "python3" alone is frequently absent/wrong on Windows (may hit the Store-stub).
        argv = [sys.executable, "-u", POLLER_SCRIPT, worker] + ([base] if base else [])
        with open(log_path, "a") as logf:
            logf.write(f"\n--- auto-relaunched by poller_autostart.py at {time.strftime('%Y-%m-%dT%H:%M:%S')} (base={base or 'default'}) ---\n")
            popen_kwargs = dict(stdout=logf, stderr=subprocess.STDOUT, cwd=str(LOG_DIR))
            if sys.platform == "win32":
                # JOB-BREAKAWAY FIX (2026-07-27, Tia · proposed to and approved by Marc Lane).
                # DETACHED_PROCESS alone does NOT escape a Windows JOB OBJECT. Claude Code runs its
                # tools and hooks inside a job that terminates its children when the job closes, so
                # a poller relaunched from a hook died with the launching turn — observed live
                # 2026-07-27: it printed [F9-ARMED], then vanished the moment the call returned.
                # That is the likely mechanism behind the 07-24 death this helper was supposed to
                # heal (and, with the self-match bug above, never even noticed).
                # CREATE_BREAKAWAY_FROM_JOB detaches the child from the job so it genuinely
                # outlives the session. Note the flag needs JOB_OBJECT_LIMIT_BREAKAWAY_OK on the
                # containing job; where that is unset CreateProcess fails with ERROR_ACCESS_DENIED,
                # so the launch below falls back to the old flags rather than not arming at all —
                # a session-scoped poller is degraded, but degraded-and-alive beats dead.
                popen_kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_BREAKAWAY_FROM_JOB
                )
            else:
                popen_kwargs["start_new_session"] = True
            try:
                subprocess.Popen(argv, **popen_kwargs)
            except OSError:
                # Breakaway refused by the containing job — retry detached-only (pre-fix behavior).
                if sys.platform != "win32":
                    raise
                popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS
                subprocess.Popen(argv, **popen_kwargs)
        return "relaunched"
    except Exception as e:
        return f"error:launch failed: {e}"
