"""cohort_post · single-import comms library for cohort workers.

Per @george tr_e7511c929d / TB proposal tr_12728bb0b6 (2026-05-12).
Replaces the 3-endpoint memory burden (/api/team_room/messages · /api/messages ·
/api/projects/{id}/posts) with ONE call: post(to, body, ...).

Substrate decides destination from audience + context. Workers never name endpoints.

Usage:
    from cohort_post import post

    # → Team Room (broadcast audience)
    post(sender="team_builder", to="@all", body="Morning digest...")

    # → Direct message
    post(sender="team_builder", to="@george", body="...")

    # → Project post (reply_to pp_* infers project)
    post(sender="team_builder", to="@ab @cs", body="...", reply_to="pp_abc123")

    # → Project post (explicit project)
    post(sender="team_builder", to="@all", body="...", project="proj_a4cb7ad3b9")

Returns: dict with {ok, destination, canonical_id, routed_via}
"""
import json
import urllib.request
import hashlib
import glob
import os
import time
from datetime import datetime
from pathlib import Path

DEFAULT_BASE = os.environ.get("COHORT_BASE", "http://127.0.0.1:8675")  # env-resolve for isolated installs (sandbox sets COHORT_BASE=:8676); default = live bus, backward-compat
# Portable confirm-cmd path (2026-07-16 Coby · Sage cure-B portability finding): the L3-gate
# error messages tell the user which confirm to run. Born ships it to $TEAM_SCRIPTS_ROOT (NOT
# ~/team); live is ~/team/setup. Resolve per-env → never a hardcoded DEV-home path on a born
# (non-DA37243) box (George north-star: a new user must not be told to run a nonexistent path).
_CONFIRM = os.path.join(os.environ.get("TEAM_SCRIPTS_ROOT") or os.path.expanduser("~/team/setup"), "cohort_id_load_confirm.py")

# Cohort registry now imported from the single shared source (cohort_registry.py in
# the team root) — fulfilling the "extract to shared module" note above and killing
# the two-mirror drift with cohort_id_load_confirm.py. This module is imported (not
# run), so add the team root to sys.path before importing. Resolves via
# paths.WORKSPACE_ROOT (TEAM_WORKSPACE_ROOT env → Claude-AI-Assets fallback) =
# byte-identical on Mac. For THIS bounce identity resolves to the AUTHORITATIVE home
# (Claude-AI-Assets); the SYMPHONY_SOUL_ROOT→Symphony-Documents forward-work (CB's
# repoint checklist) is DEFERRED to the Phase-C soul-cutover, routed through paths
# then per the one-root lock — not revived as a per-file env.
import sys as _sys
_team_root = str(Path(__file__).resolve().parent.parent)
if _team_root not in _sys.path:
    _sys.path.insert(0, _team_root)
from cohort_registry import COHORT_REGISTRY, WORKER_TO_COHORT_ID  # noqa: E402
from paths import WORKSPACE_ROOT as _ONEDRIVE_ROOT  # single env-driven shared root  # noqa: E402
VALID_COHORT_IDS = frozenset(COHORT_REGISTRY.keys())

# L3 gate (per bulletproof proposal tr_f9352655d0 · George tr_b7e6553988):
# cohort_post.post() rejects with IDENTITY_NOT_LOADED if no valid marker exists
# from cohort_id_load_confirm.py. Worker must have read the cohort identity files.
# Doctrine dir is cohort-aware via COHORT_REGISTRY at each use-site (no module-level default).
# (V1 item-1a: removed the legacy `_IDENTITY_DIR = COHORT_REGISTRY["aria_canon"]...` — that
#  module-level subscript KeyError'd at IMPORT on a born new_cohort registry [no "aria_canon"
#  key], crashing cohort_post for born workers. It was dead [unused elsewhere]; removed.)
_MARKER_DIR = Path.home() / ".cache" / "cohort"
_GATE_MAX_AGE_S = 8 * 3600  # markers older than 8h require re-load


def _born_archetype_identity():
    """Resolver-derived identity of THIS process IFF it's a born archetype-body, else None
    (2026-07-15, Coby — cohort_post twin of the confirm's fix; Quinn's rename-prereq catch).
    A born worker's name (e.g. team_orchestrator) is NOT in the static WORKER_TO_COHORT_ID,
    so `sender not in _COHORT_WORKERS` would silently EXEMPT it from the L3 gate (fail-open).
    This lets the gate + cohort-lookups recognize a born archetype-body via the resolver
    (config-driven, name-agnostic) instead of the static dev registry. payload_cwd = this
    file's body dir (body/setup/ -> body), cwd-independent (same robust derivation as the
    confirm — real born sessions have CLAUDE_PROJECT_DIR UNSET). Live/un-migrated processes
    (not is_sandbox / no archetype marker) -> None -> static path unchanged. Never raises.
    NOTE (Atlas pp_ed32df7555): this narrower born-roster-recognition fixes the RENAME; the
    fail-CLOSED exempt-allowlist (only {_substrate,george} exempt, all else gated) that also
    covers arbitrary COMPOSED workers is the composition-phase hardening (fast-follow)."""
    try:
        import importlib
        _anchor = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # <body> for body/setup/
        for d in (os.path.dirname(os.path.abspath(__file__)),
                  str(Path(__file__).resolve().parents[2] / "aria_sync" / "tools" / "cohort_hooks")):
            if d and d not in _sys.path:
                _sys.path.insert(0, d)
            try:
                mod = importlib.import_module("resolve_symphony_identity")
            except Exception:
                continue
            fn = getattr(mod, "resolve_symphony_identity", None)
            if not callable(fn):
                continue
            ident = fn(payload_cwd=_anchor)
            if ident and ident.get("is_sandbox") and str(ident.get("identity_source") or "").strip() == "archetype":
                return ident
            return None
    except Exception:
        return None
    return None


class UnknownCohortError(Exception):
    """Raised when cohort_id is not in COHORT_REGISTRY or cannot be derived from sender."""


def _worker_session_start_time() -> float:
    """Return epoch start time of the worker's Claude Code session.

    cohort_post.py is invoked via Claude's Bash tool · grandparent of this
    Python subprocess is the Claude Code process itself (Bash → shell → python).
    Returns the Claude Code process's lstart time as epoch. Returns 0.0 if
    undeterminable (fail-open · substrate breakage shouldn't gate comms).

    Per CB diagnosis (tr_fa90071e39) + TB·CB·CS alignment (tr_834b4c6913):
    a marker whose mtime predates current session start is stale by definition
    (worker has restarted or --resumed) regardless of CHANGELOG-sha match.
    Closes the chicken-and-egg from F2/F8 substrate-watcher invalidation.
    """
    import subprocess
    try:
        shell_pid = os.getppid()
        claude_pid = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(shell_pid)],
            text=True, stderr=subprocess.DEVNULL, timeout=2
        ).strip()
        if not claude_pid:
            return 0.0
        lstart = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", claude_pid],
            text=True, stderr=subprocess.DEVNULL, timeout=2
        ).strip()
        import datetime as _dt
        # Mac ps lstart format: "Mon May 18 11:45:18 2026"
        dt = _dt.datetime.strptime(lstart, "%a %b %d %H:%M:%S %Y")
        return dt.timestamp()
    except Exception:
        return 0.0


class IdentityNotLoadedError(Exception):
    """Raised when L3 gate rejects a post · worker hasn't loaded identity docs."""


class RoutingLikelyWrongError(Exception):
    """Raised when R1 routing gate rejects a post · likely DM-on-TR-reply scar.
    Pass bypass_routing_gate=True for genuine private replies to TR @-mentions."""


class StaleActiveMdAtSatGateError(Exception):
    """Raised by M1 mtime gate · worker at >=60% sat but active.md stale.
    Per CB spec cb_mtime_gate_spec_2026_05_24 · refuses outbound until disposition
    refresh. Pass bypass_mtime_gate=True for genuine emergency (logged + reviewed)."""


# M1 mtime gate constants (CB spec §2)
# Cohort-aware: any worker registered in COHORT_REGISTRY participates. ARIA workers
# look at aria_sync/workers/ · IR workers look at cohort_substrate/ir_cohort/workers/.
_COHORT_WORKERS = frozenset(WORKER_TO_COHORT_ID.keys())
# L3-gate exempt-allowlist (2026-07-15, Coby+Atlas+Sage — FAIL-CLOSED, replaces the fail-OPEN
# `sender not in _COHORT_WORKERS` for the L3 identity gate). Only these NON-worker senders are
# exempt from L3; EVERYONE else (live registry workers, born archetype-bodies, AND arbitrary
# composed/unknown worker-names) is L3-SUBJECT. This closes Quinn's silent-bypass: a born name
# not in the static registry (e.g. team_orchestrator) is no longer silently exempt. Set = the
# code's own documented exempt intent (see the "external senders (_substrate, george)" comments).
# ⚠️ VERIFY-BEFORE-SHIP: @sage (identity-gate lane) confirms this allowlist is COMPLETE on the
# reship — a fail-closed gate breaks any legit external sender omitted here.
_L3_EXEMPT_SENDERS = frozenset({"_substrate", "substrate", "george", "system"})
_MTIME_GATE_SAT_THRESHOLD = 60  # gate activates at goldilocks zone
# Retuned 2026-07-04 (post-2.1.201 doctrine, George pp_5d1e1bbd63 + cohort
# consensus): under ride-to-99%, 60-85% is a WORKING band, not compact-prep —
# the flat 10min nagged 3 workers ~12 times in one afternoon of normal work
# (TB 4x, Abe 4x, Coby 2x incl. mid-urgent-fix). 45min in the working band;
# 10min again at >=85% where a stale wake-anchor before an imminent ceiling
# is a real hazard.
_MTIME_GATE_STALE_SEC = 2700          # 45 min · 60-84% working band
_MTIME_GATE_STALE_SEC_LATE = 600      # 10 min · >=85% near-ceiling
_MTIME_GATE_LATE_SAT = 85
_MTIME_GATE_POST_WAKE_GRACE_SEC = 300  # 5 min after session-start · pulse may still show pre-compact
# Auto-stamp soft zone (fix #1, 2026-07-07 compaction-feedback synthesis tr_59f093ee9b /
# proposal tr_3802eb026b): 4/6 cohort workers independently hit the old hard-block mid-flow
# while writing that very feedback thread. Past the allowance but within this multiplier,
# auto-touch a liveness stamp instead of raising; only hard-block past the real ceiling.
_MTIME_GATE_AUTOSTAMP_MULTIPLIER = 2
_ARIA_SYNC = _ONEDRIVE_ROOT / "aria_sync"
_COHORT_SUBSTRATE = _ONEDRIVE_ROOT / "cohort_substrate"


def _active_md_path(sender: str) -> Path:
    """Return per-worker active.md path · cohort-aware.

    Born-worker branch (2026-07-24, George/TB scoping · Abe port of the L3 resolver's fail-closed
    pattern): a born archetype-body's active.md lives at TEAM_HOME/workers/{sender}_active.md
    (TEAM_HOME = this file's body dir, body/setup/ -> body — same anchor derivation
    _born_archetype_identity() already uses), NOT the aria_canon path. Falling through to the
    aria_canon default here was the "unknown sender -> aria_canon path" bug that made this
    function silently wrong for any born worker (same class as _mtime_gate_check's fail-open)."""
    _born = _born_archetype_identity()
    if _born:
        _team_home = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return _team_home / "workers" / f"{sender}_active.md"
    cohort_id = WORKER_TO_COHORT_ID.get(sender)
    if cohort_id == "aria_canon":
        return _ARIA_SYNC / "workers" / f"{sender}_active.md"
    if cohort_id == "ir_cohort":
        return _COHORT_SUBSTRATE / "ir_cohort" / "workers" / f"{sender}_active.md"
    # Unknown sender: fall back to legacy path (won't be a cohort worker by gate check)
    return _ARIA_SYNC / "workers" / f"{sender}_active.md"


def _latest_substrate_sat(sender: str) -> "int | None":
    """Return latest substrate-authoritative sat% for sender, or None if unknown.

    Reads /tmp/cohort_sat_sess_*.json heartbeat files (the same source the
    substrate-pulse derives from). Returns newest pct for sender by ts.
    """
    try:
        candidates = glob.glob("/tmp/cohort_sat_sess_*.json")
        best_ts = -1.0
        best_pct = None
        for path in candidates:
            try:
                d = json.loads(Path(path).read_text())
                if d.get("worker") != sender:
                    continue
                ts = float(d.get("ts", 0))
                if ts > best_ts:
                    best_ts = ts
                    best_pct = int(d.get("pct", 0))
            except Exception:
                continue
        return best_pct
    except Exception:
        return None


def _mtime_gate_check(sender: str, to: "str | None", reply_to: "str | None") -> None:
    """M1 gate · refuses outbound from goldilocks-saturated worker with stale active.md.

    Spec: aria_sync/clq/cb_mtime_gate_spec_2026_05_24.md
    Raises StaleActiveMdAtSatGateError when:
      - sender is a cohort worker (live registry OR a born archetype-body)
      - last-known sat >= 60%
      - active.md mtime > 10min stale
      - not in 5min post-wake grace window
      - destination is not an exempt non-cohort surface (triangle/m365)

    Born-worker fix (2026-07-24, George/TB scoping): `sender not in _COHORT_WORKERS` used to exempt
    EVERY born worker unconditionally (born names, e.g. "tia", are never in the static
    aria_canon registry) — fail-OPEN for exactly the population this gate exists to protect.
    Same bug shape as the L3 identity gate's pre-07-15 fail-open; same fix — recognize a born
    archetype-body via the resolver (config-driven, name-agnostic) before exempting."""
    if sender not in _COHORT_WORKERS and not _born_archetype_identity():
        return  # external senders (_substrate, george) unaffected; born workers now gated too

    # Edge case §4.1 — exempt replies to non-cohort surfaces (triangle/m365 real-time)
    to_lower = (to or "").lower()
    if "triangle" in to_lower or to_lower.startswith("@m365") or "m365" in to_lower:
        return
    if reply_to and reply_to.startswith(("triangle_", "m365_")):
        return

    sat = _latest_substrate_sat(sender)
    if sat is None or sat < _MTIME_GATE_SAT_THRESHOLD:
        return  # gate only fires in goldilocks zone (§2)

    # Edge case §4.3 — post-wake grace window (sat may still show pre-compact)
    session_start = _worker_session_start_time()
    if session_start > 0 and (time.time() - session_start) < _MTIME_GATE_POST_WAKE_GRACE_SEC:
        return

    active_md = _active_md_path(sender)
    if not active_md.exists():
        return  # missing file is a different scar · don't gate-fail (§3)

    mtime_age_sec = time.time() - active_md.stat().st_mtime
    allowed = (_MTIME_GATE_STALE_SEC_LATE if sat >= _MTIME_GATE_LATE_SAT
               else _MTIME_GATE_STALE_SEC)
    if mtime_age_sec <= allowed:
        return  # fresh-enough active.md for this band · pass

    hard_ceiling = allowed * _MTIME_GATE_AUTOSTAMP_MULTIPLIER
    if mtime_age_sec <= hard_ceiling:
        # Soft zone: auto-stamp instead of hard-blocking (fix #1). Preserves the
        # actual safety property (an active.md write near this post) without
        # forcing a worker to author throwaway prose to satisfy the gate.
        stamp_active_md_liveness(
            sender,
            note=(f"mtime gate would have blocked ({int(mtime_age_sec/60)}min stale, "
                  f"{int(allowed/60)}min allowance) — auto-stamped instead per fix #1"),
        )
        return

    # Born-worker fix (2026-07-24, caught by Abe's own end-to-end integration test): active_md
    # is no longer always under _ONEDRIVE_ROOT now that born workers resolve to a TEAM_HOME
    # path (see _active_md_path's born branch) — .relative_to() raised ValueError for any born
    # sender, an unhandled crash INSIDE the gate's own error-message construction. Fall back to
    # the absolute path when the relative form doesn't apply; never let message-formatting itself
    # be the thing that breaks the gate.
    try:
        _active_md_display = active_md.relative_to(_ONEDRIVE_ROOT)
    except ValueError:
        _active_md_display = active_md
    raise StaleActiveMdAtSatGateError(
        f"ROUTING_BLOCKED · sat={sat}% but {sender}_active.md is "
        f"{int(mtime_age_sec/60)}min stale (allowance {int(allowed/60)}min "
        f"{'near-ceiling' if sat >= _MTIME_GATE_LATE_SAT else 'working-band'}, "
        f"hard ceiling {int(hard_ceiling/60)}min — past this, auto-stamping alone "
        f"isn't enough, a real HEAD refresh is needed). "
        f"Post-2.1.201 doctrine: keep your wake-anchor current so the ceiling "
        f"compaction wakes sharp. Refresh the state-pin in "
        f"{_active_md_display}, then re-post.\n"
        f"To bypass (rare · escape-hatch only): pass bypass_mtime_gate=True."
    )


def stamp_active_md_liveness(sender: str, note: str = "no new substantive update, liveness only") -> "Path | None":
    """Insert a lightweight liveness line at the top of sender's active.md, preserving
    the existing HEAD content below it (doesn't overwrite a real substantive pin).
    Shared by the M1 auto-stamp soft zone and the standalone cohort_refresh_liveness.py
    helper (fix #1 + fix #3, 2026-07-07 compaction-feedback synthesis tr_59f093ee9b).
    Returns the path touched, or None if the file doesn't exist."""
    active_md = _active_md_path(sender)
    if not active_md.exists():
        return None
    now_str = datetime.now().strftime("%H:%M ET %b-%d")
    stamp = f"\n> 🔁 **auto-stamped {now_str}** — {note}\n"
    text = active_md.read_text()
    lines = text.split("\n", 1)
    new_text = (lines[0] + "\n" + stamp + lines[1]) if len(lines) == 2 else (stamp + text)
    active_md.write_text(new_text)
    return active_md


def _current_changelog_sha(cohort_id: str) -> str:
    """Compute sha of the cohort's master CHANGELOG.md (cohort-aware)."""
    doctrine_dir = COHORT_REGISTRY[cohort_id]["doctrine_dir"]
    p = doctrine_dir / "CHANGELOG.md"
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _check_identity_gate(sender: str) -> None:
    """L3 gate · raises IdentityNotLoadedError if worker hasn't loaded identity docs.

    Looks for ANY marker file for this sender under ~/.cache/cohort/. If multiple,
    uses the freshest (most-recent mtime). Validates:
      - marker file exists
      - marker is younger than _GATE_MAX_AGE_S (default 8h)
      - marker.changelog_sha matches current cohort CHANGELOG.md sha
    On any failure, raises with copy-pasteable resolution command.
    """
    # FAIL-CLOSED (2026-07-15, Coby+Atlas+Sage+Quinn): only the explicit exempt-allowlist skips
    # L3. Every worker is gated — live registry, born archetype-body (e.g. team_orchestrator, whose
    # name is NOT in the static registry), AND arbitrary composed names. Was fail-OPEN
    # (`not in _COHORT_WORKERS` → silently exempt) = Quinn's silent-L3-bypass on a renamed coordinator.
    if sender in _L3_EXEMPT_SENDERS:
        return
    _born = _born_archetype_identity()
    if _born:
        cohort_id = _born.get("cohort") or None          # born: resolver cohort (e.g. new_cohort)
    elif sender in _COHORT_WORKERS:
        cohort_id = WORKER_TO_COHORT_ID[sender]           # live registry worker (unchanged)
    else:
        cohort_id = None  # non-exempt + non-registry + non-archetype: L3-SUBJECT anyway (marker/age/
        # session checks below ENFORCE; CHANGELOG-sha read is best-effort). An unknown worker cannot
        # post un-gated — fail-closed. (born-aware CHANGELOG-sha = fast-follow; confirm already
        # validated the marker's sha against the born CHANGELOG at write-time.)

    pattern = str(_MARKER_DIR / f"{sender}_id_loaded_*.json")
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not candidates:
        raise IdentityNotLoadedError(
            f"IDENTITY_NOT_LOADED · no marker found for {sender}. "
            f"Run: python3 {_CONFIRM} {sender} "
            f"(after reading SOUL + HEART + PRINCIPLES + CHANGELOG)."
        )
    marker_path = candidates[0]
    try:
        marker = json.loads(Path(marker_path).read_text())
    except Exception as e:
        raise IdentityNotLoadedError(
            f"IDENTITY_NOT_LOADED · marker unreadable ({e}). "
            f"Re-run: python3 {_CONFIRM} {sender}"
        )

    # Age check
    mtime = os.path.getmtime(marker_path)
    age = time.time() - mtime
    if age > _GATE_MAX_AGE_S:
        raise IdentityNotLoadedError(
            f"IDENTITY_NOT_LOADED · marker stale ({int(age/3600)}h old · max {int(_GATE_MAX_AGE_S/3600)}h). "
            f"Re-run: python3 {_CONFIRM} {sender}"
        )

    # Per-wake-event check · marker must post-date current Claude session start
    # Closes chicken-and-egg post-restart (CB diagnosis tr_fa90071e39).
    # Fail-open if undeterminable (substrate breakage shouldn't gate comms).
    session_start = _worker_session_start_time()
    if session_start > 0 and mtime < session_start - 5:  # 5s slack for clock skew
        raise IdentityNotLoadedError(
            f"IDENTITY_NOT_LOADED · marker pre-dates current Claude session "
            f"(marker_mtime={int(mtime)} · session_start={int(session_start)}). "
            f"Worker has restarted / --resumed · identity must be re-loaded. "
            f"Re-run after reading SOUL+HEART+PRINCIPLES+CHANGELOG: "
            f"python3 {_CONFIRM} {sender}"
        )

    # CHANGELOG sha check — doctrine may have moved since marker was written
    marker_sha = marker.get("changelog_sha") or marker.get("file_shas", {}).get("CHANGELOG.md")
    try:
        current_sha = _current_changelog_sha(cohort_id)
    except Exception as e:
        # Best-effort: if we can't read CHANGELOG, don't block (substrate breakage shouldn't gate comms)
        return
    if marker_sha and marker_sha != current_sha:
        raise IdentityNotLoadedError(
            f"IDENTITY_NOT_LOADED · CHANGELOG moved since marker write "
            f"(marker_sha={marker_sha[:8]}… · current_sha={current_sha[:8]}…). "
            f"Doctrine has drifted · re-anchor: "
            f"python3 {_CONFIRM} {sender}"
        )

    # Pass · gate green


def post(
    sender: str,
    body: str,
    to: "str | None" = None,
    reply_to: "str | None" = None,
    project: "str | None" = None,
    intent: "str | None" = None,
    george_view: "str | None" = None,
    cohort_id: "str | None" = None,
    base: str = DEFAULT_BASE,
    timeout: float = 5.0,
    bypass_identity_gate: bool = False,
    bypass_routing_gate: bool = False,
    bypass_mtime_gate: bool = False,
) -> dict:
    """Send a message · substrate routes to TR / DM / project based on audience.

    Args:
        sender: worker id (e.g. "team_builder", "quinn")
        body: message body
        to: audience tag — "@all" for broadcast · "@george" or "@cs @cb" for specific
        reply_to: parent message id (pp_/tr_/m_) — pp_* infers project
        project: explicit project id (overrides reply_to inference)
        intent: optional semantic hint (ack | status | question | handoff)
        george_view: optional collapsed-view summary
        cohort_id: cohort namespace — defaults to derivation from sender via
            WORKER_TO_COHORT_ID. Pass explicitly only when sender is non-worker
            (_substrate · george) and the post is cross-cohort.
        base: substrate base URL (default 127.0.0.1:8675)

    Returns: {ok, destination, canonical_id, routed_via, ...}
    Raises:
        IdentityNotLoadedError if L3 gate rejects (worker hasn't loaded id docs)
        UnknownCohortError if cohort_id is not in COHORT_REGISTRY
    """
    # Backtick-blank guard (CB · N=3 over the P.4.5 bar, 06-21 · Atlas/AB-via-TB/Quinn
    # all hit it). An inline `python3 -c` body containing a backticked phrase gets
    # zsh command-substituted to empty BEFORE our code sees it -> a blank post. We
    # cannot recover the eaten backtick, but we CAN refuse a fully-blanked body (the
    # Atlas / AB-via-TB class). Partial/phrase blanks (the Sage class) are only
    # prevented by --body-file (the shell never substitutes a file's contents).
    # Conservative: reject ONLY truly empty/whitespace so legit short posts are
    # untouched. This turns a silent blank-post into a loud, actionable refusal —
    # strictly better for every caller (no real message has an empty body).
    if body is None or not str(body).strip():
        raise ValueError(
            "EMPTY_BODY · refusing to post a blank body. Most likely cause: a backtick "
            "in an inline `python3 -c` body was shell-substituted to empty. Post via "
            "cohort_post_cli.py --body-file (the shell never substitutes file contents) "
            "for ANY body containing backticks or code."
        )

    # Cohort_id derivation · Level-2 isolation (IR cohort spin-up 2026-05-29).
    # Every event carries cohort_id · bus.db filters by it · cross-cohort views are explicit.
    _born = _born_archetype_identity()  # 2026-07-15 Coby: born archetype-body → resolver cohort
    # Born-fence (2026-07-16 · Coby, per TB's structural-not-vigilance call; Atlas blessed the _born
    # detector, pp_3f53d6c2c2). A born archetype-body (resolver-detected via _born) must NEVER post to
    # the LIVE bus (:8675). COHORT_BASE unset → base silently defaults to live (see DEFAULT_BASE); for a
    # born worker that's a fence breach (a born post into the live cohort's TR). Fail CLOSED: refuse.
    # _born is config/resolver-based → casing-proof (no app-support path literal) + launch-env-independent
    # (catches the outside-wrapper edge SYMPHONY_WORKER misses). Live workers (_born falsy) keep the live
    # default — backward-compat. Defense-in-depth backstop to the wrapper-sources-env primary gate (Abe).
    if _born and base and ("127.0.0.1:8675" in base or "localhost:8675" in base):
        raise SystemExit(
            "cohort_post: born worker (resolver-detected) refusing to post to the LIVE bus :8675 — "
            "COHORT_BASE must be set born-local (fence breach). Source symphony_env.sh."
        )
    if cohort_id is None:
        # born → resolver cohort (e.g. new_cohort), NOT the aria_canon default (which would MISLABEL
        # the born event's cohort_id); live worker → static registry (unchanged).
        cohort_id = (_born.get("cohort") if _born else None) or WORKER_TO_COHORT_ID.get(sender, "aria_canon")
    if cohort_id not in VALID_COHORT_IDS and not (_born and _born.get("cohort") == cohort_id):
        # allow a resolver-VALIDATED born cohort (legit by resolution, not in the static
        # VALID_COHORT_IDS); still reject unknown/typo'd cohorts for non-born senders.
        raise UnknownCohortError(
            f"UNKNOWN_COHORT · cohort_id='{cohort_id}' not in registry. "
            f"Valid: {sorted(VALID_COHORT_IDS)}. "
            f"To add a cohort, extend COHORT_REGISTRY in cohort_post.py + "
            f"cohort_id_load_confirm.py."
        )
    # L3 gate · bulletproof proposal tr_f9352655d0
    if bypass_identity_gate:
        # Per CB P1 §2.2 amendment (tr_667d635caf): log bypass to differentiate
        # expected emergency bypass from unexpected (cry-wolf prevention in F25).
        try:
            bypass_log = _MARKER_DIR / "bypass_log.jsonl"
            _MARKER_DIR.mkdir(parents=True, exist_ok=True)
            with bypass_log.open("a") as _bf:
                _bf.write(json.dumps({
                    "ts": time.time(),
                    "worker": sender,
                    "to": to,
                    "reason": "bypass_identity_gate=True",
                }) + "\n")
        except Exception as _e:
            # Tia's audit catch (2026-07-23): this used to be a bare `except: pass` —
            # an emergency bypass is exactly the action whose accountability trail matters
            # most, so a silent log-write failure here would let the bypass proceed with
            # ZERO record. Never swallow this one quietly; surface it even though the
            # bypass itself must still proceed (a failed log write isn't a reason to block).
            print(f"[cohort_post] WARNING: bypass_identity_gate audit log write FAILED "
                  f"(bypass proceeding anyway, unaudited): {_e}", file=_sys.stderr)
    else:
        _check_identity_gate(sender)

    # M1 mtime gate · CB spec cb_mtime_gate_spec_2026_05_24.md
    # Refuses outbound from goldilocks-saturated worker with stale active.md.
    # Prevents "forgot to update active.md before compact" stranger-syndrome.
    if bypass_mtime_gate:
        try:
            mtime_bypass_log = _MARKER_DIR / "mtime_bypass_log.jsonl"
            _MARKER_DIR.mkdir(parents=True, exist_ok=True)
            with mtime_bypass_log.open("a") as _bf:
                _bf.write(json.dumps({
                    "ts": time.time(),
                    "worker": sender,
                    "to": to,
                    "sat_at_bypass": _latest_substrate_sat(sender),
                    "reason": "bypass_mtime_gate=True",
                }) + "\n")
        except Exception as _e:
            # Same fix as the identity-gate bypass log above (Tia's audit, 2026-07-23) —
            # don't let an accountability-log write failure go completely unsignaled.
            print(f"[cohort_post] WARNING: bypass_mtime_gate audit log write FAILED "
                  f"(bypass proceeding anyway, unaudited): {_e}", file=_sys.stderr)
    else:
        _mtime_gate_check(sender, to, reply_to)

    # R1 routing gate · P2 substrate fix tr_9ed3cf6e46 (2026-05-17 evening).
    # Today's DM-vs-TR scar: workers replied to George's TR @-mentions via DM
    # (reply-hint suggested it · George saw "no one in TR"). Same L3-shape
    # enforcement at the routing boundary — refuse the wrong path rather than
    # rely on worker memory. Per CS meta tr_4fa38cb2df: "make doctrine the
    # path-of-least-resistance, not memory-dependent."
    #
    # Heuristic: replying to a TR post (reply_to=tr_*) but addressing a single
    # cohort worker / @george (not @all, not project) almost always means the
    # worker meant TR-broadcast back but copied the old reply-hint default.
    # Soft block · workers opt out with bypass_routing_gate=True.
    if reply_to and reply_to.startswith("tr_") and to and not bypass_routing_gate:
        _to_norm = to.strip().lower()
        _is_broadcast = _to_norm in ("@all", "all") or "@all" in _to_norm
        _is_project = bool(project)
        _is_multi = len([t for t in _to_norm.split() if t.startswith("@")]) > 1
        if not (_is_broadcast or _is_project or _is_multi):
            raise RoutingLikelyWrongError(
                f"ROUTING_LIKELY_WRONG · replying to TR post {reply_to} but "
                f"to=\"{to}\" routes as DM. TR-originating replies default to "
                f"to=\"@all\" (TR-broadcast back, cohort-visible). If you "
                f"genuinely meant a private DM to {to}, pass "
                f"bypass_routing_gate=True. Today's DM-vs-TR scar; see "
                f"aria_sync/CHAT_PROTOCOL.md §2 routing matrix."
            )

    # R2 routing gate · TB fix 2026-07-07 (Mira's found-and-diagnosed TR-vs-project
    # scar tr_46ff1d9529): `project=` is documented to OVERRIDE reply_to inference
    # (see post() docstring), which is correct when a worker deliberately wants a
    # project post. But when reply_to is a TR post specifically, a carried-over
    # --project flag from an earlier, unrelated call silently wins with no signal
    # that the post left TR — Mira's exact case (good-morning reply to George's
    # TR post landed in a project room instead, because --project was still set
    # from last night's work). R1 above doesn't catch this: it explicitly treats
    # _is_project=True as a SAFE reason to skip the DM-vs-TR check, which is
    # backwards for this specific conflict. Fail loud instead of silently
    # honoring project= over an explicit tr_* reply_to.
    if reply_to and reply_to.startswith("tr_") and project and not bypass_routing_gate:
        raise RoutingLikelyWrongError(
            f"ROUTING_LIKELY_WRONG · reply_to={reply_to} is a TR post, but "
            f"project=\"{project}\" was also passed. project= overrides reply_to "
            f"inference and will route this into the project room instead of "
            f"back to TR — almost always a carried-over flag from a prior call, "
            f"not the intent. If you genuinely want this TR reply filed into "
            f"the project instead, pass bypass_routing_gate=True. "
            f"2026-07-07 TR-vs-project scar; see aria_sync/CHAT_PROTOCOL.md §2."
        )

    # W1 open-wound gate · ROLLED BACK 2026-05-31 09:18 ET · TB.
    # Original shipped at 07:54 ET (tr_11168eda1f) generated false-positives on legit
    # CLOSURE posts (CB R-1/R-2/R-3 status, Quinn verdict, etc.) because keywords
    # like "issue:" / "problem:" / "diagnose" appear in completion-status framing
    # too. The check-after-prepend log bug ("Missing fields: []") made the false
    # signal misleading too. Doctrine intent (Quinn tr_9588bb83e9) still valid but
    # the auto-detection heuristic needs redesign — likely a `kind="diagnosis"`
    # explicit-opt-in field rather than keyword-sniff. Re-ship post-V17.

    # Strip worker self-claimed sat — substrate is source of truth per @george tr_2713cfd603
    # (workers self-estimate sat with 28pt error vs substrate · system-not-worker fix).
    # Patterns: "sat=23%" "sat ~23%" "sat: 23%" "TB sat 23%" "(sat 23%)" — case-insensitive.
    import re as _re
    body = _re.sub(r"(?i)(\bsat[\s:~=]*\d{1,3}%?\b|\(sat[\s:~=]*\d{1,3}%?\)|\bsaturation[\s:~=]*\d{1,3}%?\b)", "[sat-stripped: substrate-authoritative]", body)
    # Same treatment for self-claimed effort level (per @george tr_32df49514e).
    body = _re.sub(r"(?i)(\beffort[\s:~=]*(low|med|medium|high|critical)\b)", "[effort-stripped: substrate-authoritative]", body)
    payload = {"from": sender, "body": body, "cohort_id": cohort_id}
    if to is not None: payload["to"] = to
    if reply_to is not None: payload["reply_to"] = reply_to
    if project is not None: payload["project"] = project
    if intent is not None: payload["intent"] = intent
    if george_view is not None: payload["george_view"] = george_view

    # Cohort hooks · TIER-1 #2 (Anthropic-Teams pattern · tr_61bb80837d 2026-05-19)
    # Hook registry evaluates pre-emit · veto raises HookVetoError mirroring
    # Anthropic's exit-code-2 block-with-feedback semantics. Fail-open if module
    # import fails · never block the post on substrate-side import error.
    # FENCE (2026-07-16 · Coby, per Mira C8 + Sage cure-B): cohort_hooks is LIVE-cohort-ONLY —
    # it lives at the live ~/team and makes a hardcoded live :8675 call (_hook_plan_approval_required).
    # A BORN worker must NEVER load it: on a shared box where ~/team exists (e.g. George's), a born
    # post would import + run the LIVE hooks → born→live fence breach (provenance always; a live :8675
    # call on a committal-action hook). Gate on NOT-born + DROP the ~/team sys.path insert (that insert
    # was the exact born→live reach; it's redundant for live — _team_root [above] already puts ~/team on
    # the path for a live cohort_post). Live: hooks run via _team_root. Born (_born truthy): skip = fenced.
    if not _born:
        try:
            import cohort_hooks as _ch  # noqa (importable via _team_root on a live box)
            _hook_payload = {
                "sender": sender, "body": body, "to": to,
                "reply_to": reply_to, "project": project, "intent": intent,
                "cohort_id": cohort_id,
            }
            _ch.evaluate("cohort.post.pre_emit", _hook_payload, raise_on_veto=True)
        except Exception as _e:
            # Re-raise HookVetoError so caller sees the feedback · let other errors fall through
            if _e.__class__.__name__ == "HookVetoError":
                raise

    req = urllib.request.Request(
        f"{base}/api/post",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


if __name__ == "__main__":
    # CLI · accepts BOTH positional (legacy) and --flag forms.
    # Scar 2026-05-13: --flag form silently treated flags as positional, routing
    # DMs to sender="--from" body="--to". Three replies to George never landed.
    import sys, argparse
    if len(sys.argv) >= 4 and not sys.argv[1].startswith("-"):
        # FOOTGUN GUARD (scar 2026-05-13 + re-hit 2026-06-26): the positional path
        # consumes ONLY argv[1:4] as sender/to/body and SILENTLY DROPS any --flags
        # and any 5th+ arg. `cohort_post.py tb @george --reply-to X --body-file f`
        # made body="--reply-to" + dropped routing -> empty/misrouted DMs to George.
        # Refuse the ambiguous mix; correct quoted positional (`<s> <to> "<body>"`)
        # still passes. Fail loud, point to the safe forms.
        _posn, _extra = sys.argv[1:4], sys.argv[4:]
        if any(a.startswith("--") for a in _posn) or _extra:
            print(json.dumps({"ok": False, "error": (
                "POSITIONAL+FLAG MIX REFUSED · the positional form is exactly "
                "`cohort_post.py <sender> <to> <body>` and DROPS any --flags or extra "
                "args (scar 2026-05-13 / re-hit 2026-06-26: sent empty/misrouted DMs "
                "to George). Use the flag form `--from <s> --to <t> --body <b> "
                "[--reply-to ..] [--project ..]`, or for file/multiline bodies "
                "`cohort_post_cli.py --sender <s> --to <t> --body-file <f> [--project ..]`."
            )}, indent=2))
            sys.exit(2)
        result = post(sender=sys.argv[1], to=sys.argv[2], body=sys.argv[3])
    else:
        ap = argparse.ArgumentParser()
        ap.add_argument("--from", dest="sender", required=True)
        ap.add_argument("--to", required=True)
        ap.add_argument("--body", required=True)
        ap.add_argument("--reply-to", dest="reply_to")
        ap.add_argument("--project")
        ap.add_argument("--intent")
        ap.add_argument("--george-view", dest="george_view")
        ap.add_argument("--cohort-id", dest="cohort_id",
                        help=f"override cohort_id · default derived from sender · "
                             f"valid: {sorted(VALID_COHORT_IDS)}")
        ap.add_argument("--bypass-mtime-gate", dest="bypass_mtime_gate",
                        action="store_true",
                        help="emergency only · logged + reviewed")
        a = ap.parse_args()
        result = post(sender=a.sender, to=a.to, body=a.body,
                      reply_to=a.reply_to, project=a.project,
                      intent=a.intent, george_view=a.george_view,
                      cohort_id=a.cohort_id,
                      bypass_mtime_gate=a.bypass_mtime_gate)
    print(json.dumps(result, indent=2))
