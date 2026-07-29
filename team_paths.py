"""team_paths — machine-relative resolver for Symphony body paths (A.3 code-untether, S1).

Single source of truth for where this *body's* local working state lives, so the
same code runs on any machine. Resolution order:

    TEAM_HOME env var  ->  ~/team  (expanduser fallback)

When TEAM_HOME is UNSET this returns exactly the legacy /Users/<user>/team layout,
so importing + using this helper is BYTE-IDENTICAL to today's hardcoded behavior.
That backward-compat is the whole point: S1 can be staged with zero behavior change
and cannot contaminate a probe (an un-migrated box still sees the real ~/team tethers).

Scope: BODY-LOCAL working state only (logs, pm_state, projects, lanes, the engine
library dir). NOT the soul — the event-log lives on SharePoint and is resolved
separately via SYMPHONY_EL_ROOT. Do not route soul paths through here.

Status (2026-06-21): staged per TB pp_713eb6b368 (S1-S2 GO). Imported nowhere yet;
wiring into bus.py/scheduler.py/server.py is the HELD S3-S6 block, fired post-probe.
"""

import os


def team_home():
    """Root of this body's local working tree. TEAM_HOME or ~/team."""
    return os.environ.get("TEAM_HOME") or os.path.expanduser("~/team")


def _under(*parts):
    return os.path.join(team_home(), *parts)


def data(*parts):
    """A path under <team_home>/data (judge log, ask log, projects, lanes, bus.db, ...)."""
    return _under("data", *parts)


def state(*parts):
    """A path under <team_home>/state (canon pipeline scripts, ...)."""
    return _under("state", *parts)


def bus_dir():
    """The engine library dir (symphony_bus) — bundled with the body, machine-resolved."""
    return _under("symphony_bus")


def runtime(*parts):
    """A path under the LOCAL-by-contract runtime root (~/symphony_runtime): the
    .seq.lock, .compacting marker, DuckDB temp, dual-write pause/read flags, bus_tmp.
    Per-machine, NEVER synced (its own root, distinct from team_home). SYMPHONY_RUNTIME
    env override → ~/symphony_runtime fallback (byte-identical to legacy when unset)."""
    base = os.environ.get("SYMPHONY_RUNTIME") or os.path.expanduser("~/symphony_runtime")
    return os.path.join(base, *parts)


def pm_state():
    return _under("pm_state.json")


def inbox(*parts):
    return _under("inbox", *parts)


def relaunch_cwd():
    """Working dir for the server's self-relaunch (replaces hardcoded `cd /Users/.../team`)."""
    return team_home()
