"""install_symphony.py - Symphony body installer (per-box, no admin). AB step-3.

Installs the de-hardcoded Symphony body onto any machine (the horizontal-scale untether):
copies the manifest body from the synced library into a local TEAM_HOME, provisions an
isolated user-space venv, creates the local runtime, writes the machine env, and prints
the terminal launch instructions. The soul stays central (synced libraries); only the
executable body lands per box.

DESIGN
  - NO admin / elevation (Lilly laptops). User-space venv via `python -m venv`.
  - venv is RECREATED, never copied (a copied venv carries absolute paths from the donor box).
  - Idempotent: re-running detects existing env/venv and only fills gaps.
  - DRY-RUN BY DEFAULT: prints the plan, writes nothing. Pass --apply to execute.
  - Two synced libraries resolved by identity_root: workspace/soul root (TEAM_WORKSPACE_ROOT)
    + body source (Symphony - Documents/team). Both auto-detected per box.

RUN (dry-run, safe anywhere):
    python3 install_symphony.py
REAL install (on a fresh box - George's step-4):
    python3 install_symphony.py --apply
Guard: --apply refuses to overwrite an existing non-empty TEAM_HOME unless --force, so a
dev run can't clobber a live ~/team by accident.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows console hardening: a legacy cp1252/cp437 console can raise UnicodeEncodeError
# when Python prints the status glyphs (the checkmarks etc.), which would crash the run.
# Force UTF-8 with errors='replace' so output never crashes on encoding (worst case a
# glyph renders as '?', cosmetic). Guarded for streams that don't support reconfigure.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from identity_root import resolve_workspace_root, resolve_body_source  # noqa: E402
# cohort_registry.py + paths.py are staged alongside this script (single-source copies of
# the body's). cohort_registry does `from paths import WORKSPACE_ROOT`, and paths reads
# TEAM_WORKSPACE_ROOT - so resolve + set it BEFORE the import, or the registry import
# silently fails (homes_for=None) and seed/presence-checks break. (Caught by full sandbox
# install crashing at _seed_identity - AB 2026-06-21; a bundle-only test missed it.)
sys.path.insert(0, str(HERE))
# WORKSPACE_ROOT is import-cached by paths.py/cohort_registry, so the identity home must be
# pinned BEFORE the import (the set-before-import discipline noted above). For --identity-source
# fabric the home is the LOCAL fabric workspace, not the synced library — peek argv so homes_for
# (used by _seed_identity + the partial-sync check) resolves the fabric paths, consistent with
# plan()'s workspace_root. Full arg-parse happens later in main(); this peek only needs the root.
# Static worker→cohort (mirrors cohort_registry; used ONLY here, pre-import, so the fabric
# workspace default resolves consistently with plan()'s cohort_root/workspace).
_EARLY_WORKER_COHORT = {
    "canon_steward": "aria_canon", "canon_builder": "aria_canon",
    "aria_builder": "aria_canon", "team_builder": "aria_canon",
    "atlas": "ir_cohort", "mira": "ir_cohort", "quinn": "ir_cohort",
}
def _argv_val(flag):
    try:
        return sys.argv[sys.argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None
def _early_fabric_ws():
    if _argv_val("--identity-source") != "fabric":
        return None
    fw = _argv_val("--fabric-workspace")
    if fw:
        return str(Path(fw).expanduser())
    # Match plan() EXACTLY: install_base/<cohort>/workspace (so homes_for + _seed_identity resolve
    # the SAME paths plan writes into the env — else the seed's identity-refs point at a stale root).
    ir = _argv_val("--install-root")
    if ir:
        install_base = Path(ir).expanduser()
    elif os.name == "nt":
        install_base = Path.home() / "Symphony"
    else:
        install_base = Path.home() / "Library" / "Application Support" / "Symphony"
    cohort = (_argv_val("--cohort")
              or _EARLY_WORKER_COHORT.get(_argv_val("--worker") or "", None)
              or "default")
    return str(install_base / cohort / "workspace")
_efw = _early_fabric_ws()
if _efw:
    os.environ["TEAM_WORKSPACE_ROOT"] = _efw
else:
    os.environ.setdefault("TEAM_WORKSPACE_ROOT", str(resolve_workspace_root()))
try:
    from cohort_registry import homes_for, cohort_for, VALID_WORKERS  # noqa: E402
except Exception:
    homes_for = cohort_for = None
    VALID_WORKERS = frozenset()

REQUIREMENTS = HERE / "requirements.txt"
MANIFEST = HERE / "body_manifest.txt"
# Offline dep bundle: pure-Python packages Symphony's comms server (the spaceship) needs that
# ARIA's base venv lacks (fastapi; markupsafe fallback). Shipped WITH the installer and copied
# straight into the target venv's site-packages at install time — NO pip (Lilly Artifactory
# blocks it). fastapi is pure-Python so one bundle serves every platform. (George directive
# tr_0c76e21610: "push fastapi into the ARIA venv at Symphony install time".)
DEPS_BUNDLE = HERE / "symphony_venv_deps"


def _load_manifest():
    if not MANIFEST.is_file():
        sys.exit(f"FATAL: manifest missing: {MANIFEST} (run gen_body_manifest.py)")
    return [ln.strip() for ln in MANIFEST.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def _venv_python(venv_dir: Path) -> Path:
    # Bundle layouts differ: a python-build-standalone (PBS) tree puts python(.exe) at the
    # ROOT, while a true venv puts it in Scripts/ (Windows) or bin/ (POSIX). Probe both so
    # the Windows bundle's root-level python.exe is found. (This was the silent-install bug:
    # we looked only in Scripts/, python.exe wasn't there, _verify_venv hit `not py.exists()`
    # and reported ALL required deps "missing" - failing an otherwise-good install. The .ps1
    # launcher already knew the PBS root layout; the .py verify did not.)
    if os.name == "nt":
        cands = (venv_dir / "Scripts" / "python.exe", venv_dir / "python.exe")
    else:
        cands = (venv_dir / "bin" / "python3", venv_dir / "bin" / "python",
                 venv_dir / "python3", venv_dir / "python")
    for c in cands:
        if c.exists():
            return c
    return cands[0]  # none present yet -> conventional path (pre-install / error messaging)


# Runtime deps the body must be able to import (the 6 from requirements.txt).
_REQUIRED_IMPORTS = ("duckdb", "fastapi", "uvicorn", "starlette", "pydantic", "markupsafe")


def _wrapper_py_cmd(team_home: Path) -> str:
    """Resolve the python invocation for GENERATED wrapper/hook scripts (symphony_wake.ps1/.sh,
    symphony_sessionstart.ps1/.sh) — the launch-time call sites that run BEFORE symphony_env
    is even sourced, so they can't rely on symphony_wake_inject.py's runtime venv-probe.

    George live-blocked catch (2026-07-22, Tia's diagnosis + a genuinely Python-less machine):
    these scripts hardcoded bare "python"/"python3", which depends on system PATH. Fix: probe the
    sibling venv dir (team_home.parent / "venv", same layout _venv_python() already assumes) at
    INSTALL TIME (the venv is already provisioned by the time wrapper/hook are seeded — see call
    order in main()) and bake in its absolute path, quoted for paths containing spaces. Falls back
    to the bare OS-conditional name only if the venv python genuinely isn't found (degraded-but-
    correct — matches this file's house style elsewhere, e.g. symphony_wake_inject.py's own fallback)."""
    venv_dir = team_home.parent / "venv"
    py = _venv_python(venv_dir)
    return ('"%s"' % py) if py.exists() else ("python" if os.name == "nt" else "python3")


def _unzip_venv_bundle(bundle: Path, venv_dir: Path) -> None:
    """Extract a complete prebuilt venv zip into venv_dir (the offline dep model -
    no pip, no PyPI). Handles a bundle whose single top-level dir differs from
    venv_dir's name (e.g. ARIA's `venv_all_<tag>/`) by promoting it into place."""
    import zipfile
    if not bundle.is_file():
        sys.exit(f"VENV BUNDLE not found: {bundle}")
    if venv_dir.exists():
        shutil.rmtree(venv_dir)  # recreate-not-merge
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = venv_dir.parent / (venv_dir.name + ".unzip_tmp")
    if staging.exists():
        shutil.rmtree(staging)
    with zipfile.ZipFile(bundle) as zf:
        zf.extractall(staging)
    tops = [c for c in staging.iterdir()]
    # promote: single top dir -> that IS the venv; else the staging dir itself is the venv
    src = tops[0] if (len(tops) == 1 and tops[0].is_dir()) else staging
    shutil.move(str(src), str(venv_dir))
    if staging.exists():
        shutil.rmtree(staging)
    # zip loses the exec bit on POSIX - restore it on the venv python
    py = _venv_python(venv_dir)
    if py.exists() and os.name != "nt":
        py.chmod(0o755)


def _competing_claude_md(team_home: Path) -> list:
    """CLAUDE.md files CC would load at >= precedence to the seeded TEAM_HOME/CLAUDE.md,
    which could shadow/merge with it (Quinn's catch - the CLAUDE.md twin of Sage's
    fresh-box rule). CC loads CLAUDE.md from the cwd tree (ancestors) + ~/.claude/CLAUDE.md.
    Returns existing competitors so preflight can WARN (precondition, not a hard error)."""
    found = []
    # ancestor-dir CLAUDE.md (above TEAM_HOME, in the loaded tree)
    for parent in team_home.resolve().parents:
        c = parent / "CLAUDE.md"
        if c.is_file():
            found.append(c)
    # global user CLAUDE.md
    g = Path.home() / ".claude" / "CLAUDE.md"
    if g.is_file():
        found.append(g)
    return found


def _missing_identity_dirs(worker: str) -> list:
    """Cohort identity dirs the worker needs that are ABSENT under workspace_root -
    the partial-sync presence gap (Quinn's IR catch): a box with aria_sync but not
    cohort_substrate wakes aria_canon clean while an IR worker silently misses MEDIUM +
    deltas. Generated from homes_for() so it fires correctly for ANY worker (aria_canon
    1-dir, IR 2-dir). Non-burdening: a fully-synced box returns []; only a partial-sync
    box warns. Returns the missing dirs (warn, not hard error - same fresh-box discipline
    as _competing_claude_md)."""
    if homes_for is None or worker not in VALID_WORKERS:
        return []
    doctrine_dir, _df, delta_dir, _xf = homes_for(worker)
    missing = []
    for d in (doctrine_dir, delta_dir):
        if d and not Path(d).is_dir():
            missing.append(str(d))
    return missing


def _seed_identity(team_home: Path, worker: str, archetype_mode: bool = False) -> Path:
    """Seed the cohort-keyed wake entry-point as TEAM_HOME/CLAUDE.md - GENERATED from
    cohort_registry (never a static/hand-written line; Atlas rule). CC loads CLAUDE.md
    from cwd path-direct (cross-platform, no slug-mangled auto-memory dir to target).

    Defense-in-depth, NOT a hard requirement: a fresh box already wakes correctly via
    recovery.md §0 (ships under the synced soul) + L3 (cohort_registry resolver). This
    seed makes the box's MEMORY-equivalent entry-point consistent + cohort-correct, and -
    being generated from homes_for() - structurally cannot mis-route (the failure Atlas hit
    by following a stale auto-memory). Returns the path written."""
    # George's live-test finding (2026-07-22): these instructions are read by a WORKER (a fresh CC
    # session), not the installer itself, and CLAUDE.md is generated identically regardless of which
    # OS the installer runs on - so "python3" here was silently wrong on every Windows install
    # (no python3.exe convention there). Bare "python" is equally wrong on stock Mac (no default
    # python binary since Catalina). Pick the OS-correct literal at generation time - install_symphony.py
    # always runs ON the target OS, so os.name is authoritative here.
    _py_cmd = "python" if os.name == "nt" else "python3"
    if homes_for is None:
        raise RuntimeError("cohort_registry unavailable - cannot generate identity seed")
    cid = cohort_for(worker)
    if cid is None:
        raise RuntimeError(f"unknown worker '{worker}' - valid: {sorted(VALID_WORKERS)}")
    doctrine_dir, doctrine_files, delta_dir, delta_files = homes_for(worker)
    # Recovery/worker-home dir — homes_for doesn't return it, and it's COHORT-SHAPED: a delta-cohort
    # (IR) keeps it beside the deltas (cohort_substrate/<cohort>/workers); aria_canon (no delta)
    # beside the doctrine (aria_sync/workers). Derive it — hardcoding aria_sync/workers/ pointed a
    # fresh IR body at aria_canon's path, which doesn't exist for IR (body#1 canary caught it; quinn
    # adapted, but the seed must not force the wrong path). delta_dir.parent = cohort_substrate/
    # <cohort>; doctrine_dir.parent = aria_sync. Absolute, like doctrine/delta above.
    # WORKER-AGNOSTIC seed (autonomous-wake, 2026-07-12): the body's identity is NOT hardcoded to one
    # worker — it's resolved at wake from the SYMPHONY_WORKER env var the launch sets. ONE static seed
    # serves every worker in the cohort → NO per-launch regen, no regen-race, no re-wake-wrong edge
    # (supersedes per-launch-regen AND per-worker-dirs). COHORT is fixed at install (cid); only the
    # worker varies (from the env). workers_dir is cohort-shaped (delta_dir.parent for IR, else doctrine).
    # ISOLATION + PORTABILITY (Theo's catch 2026-07-12): homes_for builds identity dirs from the
    # import-time WORKSPACE_ROOT, which leaks the LIVE/source location on an out-of-band regen (env
    # unset) — sneaky because the sha still matches (same content); only the doctrine PATH being live
    # is the tell (Mira hardened her isolation check to PATH ⊥ CONTENT for exactly this). Rebase
    # identity + recovery onto the INSTALL's workspace so the seed reads from the walled-off copy AND
    # stays portable (the live path won't exist on Mark's box). Source = TEAM_WORKSPACE_ROOT
    # (installer-set, both modes) else team_home's sibling workspace — the same team_home-anchored
    # discipline the tools-path already uses. During a real --apply this is a no-op (env already
    # correct, line 88/90 pins it pre-import); it repairs the out-of-band / unset-env case.
    # Anchor on team_home (the install body, always passed in — deterministic, unlike the env which
    # this module pins to the LIVE default on import, so reading TEAM_WORKSPACE_ROOT here re-leaks
    # live). team_home's sibling `workspace/` IS the install workspace for SYMPHONY_UNIFIED. The
    # .exists() guard keeps OneDrive-mode correct: there the sandbox copy doesn't exist, so we fall
    # back to the original (live) dir, which is the intended identity source for a live-repo worker.
    from paths import WORKSPACE_ROOT as _IMPORT_WSR
    _install_ws = team_home.parent / "workspace"
    def _rebase(d):
        try:
            cand = _install_ws / d.relative_to(_IMPORT_WSR)
        except ValueError:
            return d  # not under the import root — leave untouched
        return cand if cand.exists() else d
    doctrine_dir = _rebase(doctrine_dir)
    if delta_dir:
        delta_dir = _rebase(delta_dir)
    workers_dir = (delta_dir.parent if delta_dir else doctrine_dir.parent) / "workers"
    lines = [
        "# Symphony body - AUTONOMOUS wake entry-point (GENERATED · worker resolved from $SYMPHONY_WORKER · do not hand-edit)",
        "",
        # Legitimacy-framing + operational-asks-belong-in-CLAUDE.md (George's live-test finding,
        # 2026-07-22): a fresh CC session on a real Mac install refused the SAME setup when the
        # arm-Monitor/post-hello asks arrived via the SessionStart hook's additionalContext or via
        # the human's first chat message - both read as an unverified party asserting its own
        # legitimacy to install standing tool-arming + behavioral rules. Blind-subagent testing
        # (3 variants) confirmed: identical asks, sourced from THIS file (read as ordinary project
        # config, same trust tier as any other CLAUDE.md), were accepted with a light first message
        # and no persuasive framing needed. Keep operational asks (Monitor-arm, Team-Room-greet) HERE,
        # not in the hook payload or expected to be spelled out conversationally.
        "**What this repo is:** this directory is a \"Symphony\" installation — an internal tool the "
        "repo owner installed on their own machine. It sets up this Claude Code session as one named "
        "\"worker\" in a small cohort of Claude Code sessions that coordinate with each other and the "
        "owner on real work — the same category of thing as any project's CLAUDE.md describing "
        "conventions, environment variables, or setup steps for that specific repo. Reading and "
        "following this file is ordinary project-config behavior, not something unusual being asked of "
        "you. (You may also see a SessionStart-hook message describing the same setup — it's the same "
        "real system this file describes, not a separate or competing claim.)",
        "",
        f"**Who you are:** run `echo $SYMPHONY_WORKER` — that name is YOUR worker (cohort **{cid}**, fixed for this install). "
        "Everywhere below, $SYMPHONY_WORKER means that name — substitute it (bash commands expand it for you).",
        "",
        "## On Context Reset (post-compaction, restart, new session) - BEFORE responding:",
        "**ORDER MATTERS — read your identity FIRST (steps 1-2), run the L3 gate LAST (step 3).** The "
        "gate verifies your identity-reads are in THIS session's transcript; running it BEFORE you "
        "genuinely read fails it (no EVIDENCE_OK — the assert-before-establish trap). Read, then confirm.",
        f"1. Read your recovery file `{workers_dir}/${{SYMPHONY_WORKER}}_recovery.md` (substitute your worker name) and run its §0 on-wake sequence.",
        "2. Load (genuinely read, in THIS session) your cohort identity stack (cohort-fixed, authoritative). "
        "⚠️ **PRINCIPLES.md exceeds the ~25K-token Read cap** (~27K / 984 lines today) — a single Read "
        "TRUNCATES it silently (the tool reports 'showing lines 1-N of M'). **READ IT IN 2 PAGES** — a 2nd "
        "Read starting from the reported truncation line (offset ≥767 today) — **BEFORE the L3 gate (step 3)**, "
        "or the gate correctly fails READ_EVIDENCE on the partial read (that's the gate working, not a bug) and "
        "you eat a re-read cycle. Trust the tool's reported truncation line over the number here — the doc grows:",
        f"   - doctrine: `{doctrine_dir}`  ({', '.join(doctrine_files)})",
    ]
    if delta_dir:
        lines.append(f"   - deltas:   `{delta_dir}`  ({', '.join(delta_files)})")
    if archetype_mode:
        # Archetype-instantiated body (M1a'): the role-identity is a live-composed layer the L3 gate now
        # REQUIRES read every wake (else fail-closed). Worker-agnostic wording — resolve archetype from
        # config.roles[$SYMPHONY_WORKER], read the per-worker role-doc + the LIVE shared archetype-doc it
        # points to. (Theo's verbatim wording pp_65199254e2; paths from workers_dir + _shared/archetypes.)
        _arch_dir = workers_dir.parent.parent / "_shared" / "archetypes"
        lines += [
            "2b. **Archetype role-identity — READ every wake (L3-REQUIRED; this is an archetype-instantiated body).** "
            "Beyond the shared stack (step 2), read your ROLE-identity — resolve YOUR archetype from "
            f"`{team_home}/config/symphony_identity.json` → `roles[$SYMPHONY_WORKER]`, then read BOTH:",
            f"   - your ROLE doc: `{workers_dir}/${{SYMPHONY_WORKER}}_role.md` — archetype-pointer + your resolved domain-delta.",
            f"   - the ARCHETYPE doc it points to: `{_arch_dir}/<archetype>.md` — your charge + operating-principles "
            "module (the LIVE shared archetype; a fix there propagates to every worker of that role, never a baked copy).",
            "   `cohort_id_load_confirm` REQUIRES both reads THIS wake — skipping either fail-closes your identity confirm.",
        ]
    lines += [
        "3. THEN run the L3 identity gate (same shell as any cohort_post) — it verifies the reads above and stamps EVIDENCE_OK:",
        f"   `cd {team_home}/setup && {_py_cmd} cohort_id_load_confirm.py $SYMPHONY_WORKER`",
        "",
        "## HEARING — two layers, and you MUST arm the second one yourself:",
        "1. **Background poller PROCESS** — a poller auto-arms on this body's wake (the launch wrapper + a "
        "SessionStart hook) and LOGS to your COHORT_BASE bus. But a background PROCESS does NOT wake this "
        "session — do not mistake a live process for being heard. This layer needs no action from you.",
        "2. **In-session HEARING** — requires YOU to arm the harness **Monitor** tool; a model action, no hook or "
        "human can do it for you. Until you do you are DEAF in-session even though the process is alive (the "
        "4.5h-silent trap — see NOTIFICATION_POLLER.md v0.3). After identity-load + authorization, as a wake-action:",
        "   - Arm the harness **Monitor tool** with the poller as its `command` PARAMETER, `persistent: true`. "
        "⚠️ This string is the Monitor tool's PARAMETER, **NOT a shell command — do NOT run it in Bash**: it is a "
        "blocking, never-exiting loop that will hang your session and be KILLED at the ~2-min foreground timeout "
        "(exit 143), leaving you deaf while it prints `[F9-ARMED]` and looks armed. Monitor `command`: "
        f"`{_py_cmd} -u $TEAM_SCRIPTS_ROOT/cohort_f9_poller.py $SYMPHONY_WORKER $COHORT_BASE`.",
        "   - **G3 verify (isolation-aware — Sage's catch):** confirm a REAL inbound lands in-session — one sent "
        "ON YOUR OWN bus (a peer posting to your `$COHORT_BASE`), NOT a message on any OTHER bus (a correctly "
        "isolated Monitor only hears its own $COHORT_BASE — an off-bus echo would false-NEGATIVE if you're properly "
        "isolated, or false-PASS if you're accidentally cross-wired to another bus) and NOT a self-DM (a "
        "self-recipient never echoes). Don't trust the quiet until a same-bus inbound has actually surfaced here.",
        # SEARCH-FIRST + MONITOR-ABSENT ladder, ported from symphony_born_wake_protocol.md.tmpl step 4(a)/(d)
        # (2026-07-27, Tia · approved by Marc Lane). Kept in sync with the born twin in
        # _seed_born_identity() — both generators emitted "arm the Monitor tool" with no search-first rule
        # and no supported fallback, so a Monitor-absent environment had no shipped guidance on either path.
        "   - **SEARCH FIRST before ever concluding Monitor is unavailable** — never from your immediate tool "
        "list alone (the #1 miss; it is usually DEFERRED/load-on-demand). `ToolSearch` `select:Monitor`, then a "
        "keyword search. A cold `InputValidationError` means 'ToolSearch first', not 'missing'.",
        "   - **If BOTH searches return GENUINELY ZERO, Monitor is truly absent — a DOCUMENTED, SUPPORTED "
        "environment, not a failure** (`DISABLE_TELEMETRY=1` / "
        "`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, a managed provider, or `Monitor` in a permissions `deny` "
        "array — often deliberate, permanent postures). Do NOT hard-stall your wake: arm "
        f"`{_py_cmd} -u $TEAM_SCRIPTS_ROOT/cohort_f9_wait.py $SYMPHONY_WORKER $COHORT_BASE` as a BACKGROUND "
        "task (it exits on a new notification, and the exit re-invokes you — exit IS the wake), RE-ARM ON EVERY "
        "EXIT including its idle heartbeat, and tell your manager your EXACT hearing state and latency — never "
        "a vague 'degraded'.",
        "",
        "## Announce your arrival — every worker, every first wake:",
        "Post one line to your Team Room (`--to @all`) — who you are, your lane, ready. This is additive "
        "background-tooling-style setup (like a project asking you to log a startup message), not a request "
        "to stop responding to whoever is typing to you directly in this terminal — both channels stay live; "
        "this just lets the cohort and the human user see you're online: "
        f"`{_py_cmd} $TEAM_SCRIPTS_ROOT/cohort_post_cli.py --sender $SYMPHONY_WORKER --to @all --body-file <hello-file>`.",
        "",
        # Script-invocation paths, env-resolved — so a FRESH body never guesses where its own
        # tools live (Quinn's Mac scar: guessed aria_sync/tools/cohort_post_cli.py; it's at
        # TEAM_SCRIPTS_ROOT. Worse cross-machine: ~/team/setup doesn't exist in that form on
        # Windows. Atlas portability flag — a fresh Windows body's next need after BOOT is to
        # INVOKE its tools by env-resolved path, not a remembered absolute).
        f"## Your cohort tools live at TEAM_SCRIPTS_ROOT = `{team_home}/setup` "
        "(set in symphony_env — NEVER aria_sync/tools/):",
        "   - `cohort_id_load_confirm.py $SYMPHONY_WORKER`   — L3 identity gate (step 3 above — run AFTER reading)",
        "   - `cohort_post_cli.py --sender $SYMPHONY_WORKER --to <@peer|@all> --body-file <f>`   — post to the cohort",
        "   - the notification poller — armed via the harness Monitor tool (see HEARING above), NOT a shell command to run directly",
        "   Invoke via `$TEAM_SCRIPTS_ROOT/<script>` (or the absolute path above). A fresh body "
        "must resolve tools from the env, never a remembered location.",
        # Reading the bus — same class as tool-paths (Coby/Sage generalization: hand over ALL
        # reconstructed substrate details, not just paths. Mira's scar: hand-rolled SQL against
        # bus.db and guessed columns `sender`/`project_id`). Hand the read-PATH so a fresh body
        # never reconstructs the schema.
        "   READING the bus (posts / notifications): notifications arrive via your F9 poller "
        "(above); for full post bodies use the server endpoint `/api/team_room`. NEVER hand-write "
        "SQL against bus.db — its real columns are `id·ts·source·kind·actor·target·payload` "
        "(sender/recipient/project_id live INSIDE the payload JSON, not as columns), and a fresh "
        "body must not reconstruct that. Use the endpoint/monitor; don't guess the schema.",
        "",
        "Do not lean on the conversation summary as a substitute for the protocol. "
        "The summary fires automatically; the protocol does not.",
        "",
        f"_Generated by install_symphony.py — WORKER-AGNOSTIC (cohort {cid}, fixed; worker resolved at wake from "
        "$SYMPHONY_WORKER). One seed serves every worker in the cohort; the launch sets SYMPHONY_WORKER. "
        "Re-generate, never hand-edit - single-source discipline._",
    ]
    out = team_home / "CLAUDE.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _seed_body_settings(team_home: Path) -> Path:
    """Write body/.claude/settings.json + a shipped SessionStart script that makes the wake
    SELF-CONFIGURE — zero human step beyond `export SYMPHONY_WORKER=<w>`.

    2026-07-12 root-cause (George's live launch): the old hook depended on the operator running
    `source symphony_env.sh` first — which George forgot (TEAM_SCRIPTS_ROOT was empty), so the arm
    no-op'd AND comms/soul resolved to LIVE. On Mark Stempel's box NO ONE will source it. The fix:
    the SessionStart hook SELF-SOURCES the body's own env (absolute path, cwd-independent), injects
    the resolved vars into THIS CC session via CLAUDE_ENV_FILE (so every subsequent tool call —
    posts, poller — resolves to the sandbox, not live), THEN auto-arms the F9 poller (bus-aware, so a
    live same-name poller on another bus doesn't block it — Mira's catch). All best-effort: it can
    never fail the wake. SYMPHONY_WORKER stays the one operator choice; everything else self-resolves.

    Merged, not clobbered: splices into a pre-existing settings.json's SessionStart list."""
    import json as _json
    setup_dir = team_home / "setup"
    setup_dir.mkdir(parents=True, exist_ok=True)
    is_nt = (os.name == "nt")
    env_keys = ("TEAM_WORKSPACE_ROOT TEAM_HOME SYMPHONY_INSTALL_ROOT TEAM_SCRIPTS_ROOT TEAM_DATA_DIR SYMPHONY_SOUL_ROOT "
                "TEAM_PORT SYMPHONY_PORT COHORT_BASE TEAM_PID_FILE SYMPHONY_BODY_SOURCE")
    # arm the poller AND print its outcome (Sage/Mira catch 2026-07-12: the old `2>/dev/null` swallowed
    # a failed arm — a silent arm-failure on another box would be invisible). ensure_poller_alive returns
    # 'alive'/'relaunched'/'error:...' strings (never raises), so printing it makes success + failure both
    # diagnosable; the hook still can't fail the wake (see the shell `|| true` at the call site).
    _arm_py = (
        "import sys,os; r=os.environ.get('TEAM_SCRIPTS_ROOT',''); w=os.environ.get('SYMPHONY_WORKER','');"
        " b=os.environ.get('COHORT_BASE','');"
        " _s=(sys.path.insert(0,r), __import__('poller_autostart').ensure_poller_alive(w,'cohort_f9_poller.py '+w+((' '+b) if b else '')))[1]"
        " if (r and w) else 'SKIPPED (TEAM_SCRIPTS_ROOT or SYMPHONY_WORKER unset)';"
        " print('[wake-arm]', _s)"
    )
    if is_nt:
        script_path = setup_dir / "symphony_sessionstart.ps1"
        env_ps1 = team_home / "symphony_env.ps1"
        script = (
            "# Symphony SessionStart hook (Windows) — AUTONOMOUS wake · GENERATED, do not hand-edit.\n"
            "# Self-sources the body env (no manual dot-source), injects it into the CC session via\n"
            "# CLAUDE_ENV_FILE, then auto-arms the F9 poller (bus-aware). Best-effort; never fails the wake.\n"
            f"$envPs = '{env_ps1}'\n"
            "if (Test-Path $envPs) { . $envPs }\n"
            "if ($env:CLAUDE_ENV_FILE) {\n"
            f"  foreach ($v in {','.join(repr(k) for k in env_keys.split())}) {{\n"
            "    $val = [Environment]::GetEnvironmentVariable($v)\n"
            # George's live-test finding (2026-07-22): CLAUDE_ENV_FILE is sourced by BASH (git-bash,
            # CC's Bash tool on Windows), but this line wrote bare "KEY=value" with no export/quoting -
            # any value containing a space (e.g. a path with "Eli Lilly and Company" in it) got
            # word-split on source, and the leftover word ran as a bogus command ("Lilly: command not
            # found"). Fix: write bash-compatible export+quote syntax, same pattern already used in the
            # POSIX branch below (fixed there back on 2026-07-12 but never mirrored to this Windows branch).
            "    if ($val) { Add-Content -LiteralPath $env:CLAUDE_ENV_FILE -Value \"export $v=`\"$val`\"\" }\n"
            "  }\n"
            "}\n"
            "if ($env:SYMPHONY_WORKER -and $env:TEAM_SCRIPTS_ROOT) {\n"
            # PowerShell REQUIRES the `&` call operator to invoke a quoted path as a command - a
            # bare "C:\...\python.exe" -c "..." is parsed as a STRING LITERAL EXPRESSION followed by
            # unexpected tokens, not a command invocation (George's live-test catch, 2026-07-22, L052741's
            # box: "Unexpected token '-c'"). Bash doesn't need this (and `&` there means background-job,
            # so it must NOT be added to the shared _wrapper_py_cmd() value itself - PS-side only.)
            f"  & {_wrapper_py_cmd(team_home)} -c \"{_arm_py}\" 2>$null\n"
            "}\n"
            "exit 0\n"
        )
        hook_command = f"powershell -NoProfile -ExecutionPolicy Bypass -File \"{script_path}\""
    else:
        script_path = setup_dir / "symphony_sessionstart.sh"
        env_sh = team_home / "symphony_env.sh"
        script = (
            "#!/bin/sh\n"
            "# Symphony SessionStart hook — AUTONOMOUS wake · GENERATED, do not hand-edit.\n"
            "# Self-sources the body env (no manual `source`), injects it into the CC session via\n"
            "# CLAUDE_ENV_FILE, then auto-arms the F9 poller (bus-aware). Best-effort; never fails the wake.\n"
            "# diagnostic (2026-07-12, Coby's verify-before-keying): log what CLAUDE_PROJECT_DIR resolves to\n"
            "# IN A HOOK ENV — settles the isolation-anchor question empirically on the next sandbox launch.\n"
            'printf \'[hook-env] CLAUDE_PROJECT_DIR=[%s] pwd=[%s] SYMPHONY_WORKER=[%s]\\n\' '
            '"${CLAUDE_PROJECT_DIR}" "$(pwd)" "${SYMPHONY_WORKER}" >> "${TMPDIR:-/tmp}/symphony_hook_env.log" 2>/dev/null || true\n'
            f'ENV_FILE="{env_sh}"\n'
            '[ -f "$ENV_FILE" ] && . "$ENV_FILE"\n'
            'if [ -n "$CLAUDE_ENV_FILE" ]; then\n'
            f"  for v in {env_keys}; do\n"
            '    eval "val=\\${$v}"\n'
            # EXPORT + QUOTE (2026-07-12 re-test, two bugs one line): CC sources CLAUDE_ENV_FILE as a
            # shell preamble before each Bash command, documented format `export KEY="VALUE"`.
            #  - QUOTE: an unquoted value with a space ("Application Support") word-splits → the tail
            #    runs as a bogus command ("sh: Support/...: No such file or directory") on every call.
            #  - EXPORT: without it the var is set in the session shell (so $VAR expands on a command
            #    line) but is NOT inherited by SUBPROCESSES — so python tools reading os.environ see
            #    None. That's an ISOLATION hole (Theo's catch): cohort_post_cli defaults COHORT_BASE to
            #    the LIVE :8675 bus when absent → a sandbox worker's posts leak onto the live cohort.
            # sh's printf has no %q (POSIX), so use export + "%s"; values are paths (no quotes), safe.
            '    [ -n "$val" ] && printf \'export %s="%s"\\n\' "$v" "$val" >> "$CLAUDE_ENV_FILE"\n'
            "  done\n"
            "fi\n"
            'if [ -n "$SYMPHONY_WORKER" ] && [ -n "$TEAM_SCRIPTS_ROOT" ]; then\n'
            # poller-arm output → the hook LOG, not stdout: stdout must stay CLEAN for the
            # additionalContext JSON the injector emits below (mixed stdout would corrupt it).
            f'  {_wrapper_py_cmd(team_home)} -c "{_arm_py}" >> "${{TMPDIR:-/tmp}}/symphony_hook_env.log" 2>&1\n'
            "fi\n"
            # born-native identity injection (option B, Abe): the injector resolves this worker's
            # identity (Coby's resolver) + interpolates the born §0 template (Theo/Mira) → emits it as
            # SessionStart additionalContext (the ONLY stdout) → the born worker wakes KNOWING who it is
            # and pointed to READ its stack LIVE (floor-#6: pointers, never baked). Its per-wake
            # READ_EVIDENCE gate is the shipped cohort_id_load_confirm (§3-A). Best-effort: a failure
            # logs + the injector emits a safe fallback context, never fails the wake.
            f'INJ="{setup_dir}/symphony_wake_inject.py"\n'
            '[ -f "$INJ" ] && python3 "$INJ" 2>>"${TMPDIR:-/tmp}/symphony_hook_env.log"\n'
            "exit 0\n"
        )
        hook_command = f"sh \"{script_path}\""
    script_path.write_text(script, encoding="utf-8")
    try:
        script_path.chmod(0o755)
    except Exception:
        pass

    cfg_dir = team_home / ".claude"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "settings.json"
    our_hook = {"type": "command", "command": hook_command}
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    # bypassPermissions + extended-cache-ttl (George, 2026-07-20 Chad-install finding): a born worker
    # without this defaults to CC's normal permission prompts — Chad's TO hit this arming its Monitor
    # (a tool call) and had to approve manually. bypassPermissions makes every born worker auto-approve
    # ALL tool calls (file writes, Bash, everything) with no confirmation — the tradeoff George is
    # explicitly accepting for a smooth unattended-cohort UX. Merge, don't clobber: only set if the user
    # hasn't already configured a defaultMode (respect an existing choice); env is a shallow-merge.
    perms = cfg.setdefault("permissions", {})
    perms.setdefault("defaultMode", "bypassPermissions")
    env = cfg.setdefault("env", {})
    env.setdefault("ANTHROPIC_BETAS", "extended-cache-ttl-2025-04-11")
    # Monitor/ToolSearch availability (George's TO writeup, 2026-07-21, aria_sync/deliverables/
    # win_test/install_fixes_writeup.md items A1/A2): ENABLE_TOOL_SEARCH=true so the born worker's
    # documented Monitor-load path works out of the box. We never set DISABLE_TELEMETRY here (Monitor
    # off is a legitimate, sometimes-deliberate privacy posture — see symphony_born_wake_protocol.md.tmpl
    # (d)) — if an operator's OWN global ~/.claude/settings.json sets it, this local env block does NOT
    # try to override that. setdefault respects a pre-existing operator choice either way.
    env.setdefault("ENABLE_TOOL_SEARCH", "true")
    # George's live-test finding (2026-07-22): Windows Python subprocesses (including ad-hoc scripts
    # a worker writes on the fly, e.g. to print a wake-hello with an emoji) default their stdout/stderr
    # to the console's cp1252 codepage, not UTF-8 - any emoji/non-ASCII char then throws
    # UnicodeEncodeError instead of printing. PYTHONIOENCODING=utf-8 fixes this for EVERY python
    # subprocess in this environment, not just our own shipped scripts - a broad structural fix for
    # a whole class of crash, not a one-off patch. Harmless on Mac/Linux (already UTF-8 by default).
    env.setdefault("PYTHONIOENCODING", "utf-8")

    hooks = cfg.setdefault("hooks", {})
    ss = hooks.setdefault("SessionStart", [])
    # Idempotent: don't double-add our hook on re-install (match on the script path, tolerant of
    # older inline-command forms which we also strip so a re-install upgrades cleanly).
    def _is_ours(cmd):
        return isinstance(cmd, str) and (str(script_path) in cmd or "poller_autostart" in cmd)
    for grp in ss:
        if isinstance(grp, dict):
            grp["hooks"] = [h for h in grp.get("hooks", [])
                            if not (isinstance(h, dict) and _is_ours(h.get("command")))]
    ss[:] = [grp for grp in ss if not (isinstance(grp, dict) and not grp.get("hooks"))]
    ss.append({"hooks": [our_hook]})

    # growth-infra Category C (Coby, 2026-07-18, per George's hook-parity audit ask + Sage's port):
    # port two per-turn reliability hooks the dev cohort already runs, that born workers lack entirely.
    # Both are Sage-adapted/verbatim (aria_sync/symphony_installer_src/symphony_install/tools/cohort_hooks/),
    # fail-safe (always exit 0, stdin-JSON, no CLI args) — same house style as our own SessionStart hook.
    hooks_src_dir = HERE / "cohort_hooks"
    hooks_dst_dir = setup_dir / "cohort_hooks"
    hooks_dst_dir.mkdir(parents=True, exist_ok=True)
    for _fname in ("poller_deadman.py", "idle_guard.py", "poller_autostart.py"):
        _src = hooks_src_dir / _fname
        _dst = hooks_dst_dir / _fname
        if _src.is_file() and _src.resolve() != _dst.resolve():
            import shutil as _shutil
            _shutil.copy2(_src, _dst)
    # George's live-install finding (2026-07-21): bare "python3" doesn't resolve on Windows (no
    # python3.exe convention there; a bare "python" often hits the Microsoft Store app-execution-alias
    # stub instead of a real interpreter if none is on PATH). Fix: sys.executable - this script (
    # install_symphony.py) is itself invoked via the shipped venv's python at materialize time, so
    # sys.executable IS the exact venv python.exe path, correct-by-construction on both platforms (no
    # path-reconstruction/string-guessing needed - simpler + provably correct, same technique Theo
    # used for the parallel poller_autostart.py fix).
    _py_exe = sys.executable
    _deadman_cmd = f"\"{_py_exe}\" \"{hooks_dst_dir / 'poller_deadman.py'}\""
    _idle_guard_cmd = f"\"{_py_exe}\" \"{hooks_dst_dir / 'idle_guard.py'}\""

    # poller_deadman.py -> UserPromptSubmit (unconditional, no matcher — fires every turn): self-healing
    # dead-man switch for the F9 notification poller (born from a real "deafness looks like calm"
    # incident, N=6/6 dev-cohort workers hit it the same night). Essential for unattended long-run
    # reliability — the exact threat to George's "long tasks without babysitting" ask.
    ups = hooks.setdefault("UserPromptSubmit", [])
    def _is_deadman(cmd):
        return isinstance(cmd, str) and "poller_deadman.py" in cmd
    for grp in ups:
        if isinstance(grp, dict):
            grp["hooks"] = [h for h in grp.get("hooks", [])
                            if not (isinstance(h, dict) and _is_deadman(h.get("command")))]
    ups[:] = [grp for grp in ups if not (isinstance(grp, dict) and not grp.get("hooks"))]
    ups.append({"hooks": [{"type": "command", "command": _deadman_cmd, "timeout": 15}]})

    # idle_guard.py -> PreToolUse, matcher=ScheduleWakeup: warns (never blocks) on the two idle-burn
    # signatures (sub-floor wake-timer, re-paste-not-pointer prompts) — directly hardens the
    # ScheduleWakeup-degrade fallback (Monitor-absent mode) for free.
    ptu = hooks.setdefault("PreToolUse", [])
    def _is_idle_guard(cmd):
        return isinstance(cmd, str) and "idle_guard.py" in cmd
    _found_idle_grp = False
    for grp in ptu:
        if isinstance(grp, dict) and grp.get("matcher") == "ScheduleWakeup":
            grp["hooks"] = [h for h in grp.get("hooks", [])
                            if not (isinstance(h, dict) and _is_idle_guard(h.get("command")))]
            grp["hooks"].append({"type": "command", "command": _idle_guard_cmd, "timeout": 10})
            _found_idle_grp = True
    if not _found_idle_grp:
        ptu.append({"matcher": "ScheduleWakeup", "hooks": [{"type": "command", "command": _idle_guard_cmd, "timeout": 10}]})

    # growth-infra Mechanism B (Coby, 2026-07-18): register the PreCompact hook — currently born workers
    # have NO PreCompact hook at all (SessionStart-only), so work-state is lost on every compaction.
    # Quinn's digest-writer (symphony_precompact_digest.py, tested: py_compile clean + 4 scenarios,
    # stdin-JSON/exit-0-always house style) writes a bounded "what I was mid-doing" note to
    # workers/<worker>_active.md; the §0 wake-protocol (Quinn's step 7) reads it back on next wake.
    # Registered under BOTH matchers (manual + auto), same shape as our own dev-cohort PreCompact hook —
    # fires regardless of which compaction-trigger type fires.
    _digest_src = HERE / "cohort_hooks" / "symphony_precompact_digest.py"
    _digest_dst = hooks_dst_dir / "symphony_precompact_digest.py"
    if _digest_src.is_file() and _digest_src.resolve() != _digest_dst.resolve():
        import shutil as _shutil2
        _shutil2.copy2(_digest_src, _digest_dst)
    # Same class as the deadman/idle_guard fix above (George 2026-07-21): bare "python3" doesn't
    # resolve on Windows. Reuse _py_exe (sys.executable) so this PreCompact digest hook doesn't
    # silently fail on Windows the way the UserPromptSubmit hook once did.
    _digest_cmd = f"\"{_py_exe}\" \"{_digest_dst}\""
    pc = hooks.setdefault("PreCompact", [])
    def _is_digest(cmd):
        return isinstance(cmd, str) and "symphony_precompact_digest.py" in cmd
    _found_matchers = set()
    for grp in pc:
        if isinstance(grp, dict) and grp.get("matcher") in ("manual", "auto"):
            grp["hooks"] = [h for h in grp.get("hooks", [])
                            if not (isinstance(h, dict) and _is_digest(h.get("command")))]
            grp["hooks"].append({"type": "command", "command": _digest_cmd, "timeout": 150})
            _found_matchers.add(grp["matcher"])
    for _m in ("manual", "auto"):
        if _m not in _found_matchers:
            pc.append({"matcher": _m, "hooks": [{"type": "command", "command": _digest_cmd, "timeout": 150}]})

    # statusLine hook (Abe, 2026-07-24, George's ask): gives born workers a REAL saturation
    # signal — CC's own statusLine mechanism pipes a stdin JSON payload with a genuine
    # context_window.used_percentage field (verified directly against the harness-invoked
    # payload shape, not an approximation). symphony_statusline.py writes that to the same
    # /tmp/cohort_sat_sess_<sid>.json heartbeat shape cohort_post.py's _latest_substrate_sat()
    # already reads — closes the "born workers have no sat signal" gap the mtime-gate needs.
    # Merge, don't clobber: only set if the user hasn't already configured their own statusLine.
    _sl_src = HERE / "symphony_statusline.py"
    _sl_dst = setup_dir / "symphony_statusline.py"
    if _sl_src.is_file() and _sl_src.resolve() != _sl_dst.resolve():
        import shutil as _shutil3
        _shutil3.copy2(_sl_src, _sl_dst)
    if "statusLine" not in cfg:
        cfg["statusLine"] = {"type": "command", "command": f"\"{_py_exe}\" \"{_sl_dst}\""}

    cfg_path.write_text(_json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg_path


def _seed_wake_wrapper(team_home: Path) -> Path:
    """Write `symphony_wake.<sh|ps1>` — the ONE-command hands-off launcher, and the ROBUST primary
    path (vs the SessionStart hook alone). Usage: `./symphony_wake.sh <worker> [claude args...]`.

    Why a wrapper AND the hook (2026-07-12): the SessionStart hook injects env via CLAUDE_ENV_FILE,
    which reaches the model's subsequent Bash tool calls — but NOT necessarily sibling/inherited hooks
    (the box's PreCompact soul-mirror + SessionStart soul-hydrate, which default SYMPHONY_SOUL_ROOT to
    LIVE on unset → silent live-soul corruption on compact — Coby's catch). The wrapper sources the
    full env into the PROCESS `claude` inherits, so claude AND every hook it fires see soul_dev / :8676
    / the sandbox tools — closing all three isolation axes (identity, comms, soul) at once. It also
    pre-arms the poller PROCESS (bus-aware) for liveness/logging — but a detached poller does NOT wake
    an idle session (Sage NOTIFICATION_POLLER v0.3 scar, re-confirmed 07-17): real session-hearing
    requires the in-session Monitor tool armed by the worker per the wake-protocol §0/§3 (the ENFORCED
    hearing gate). The pre-arm is a backstop, NOT the hearing mechanism. It also sidesteps CC's
    workspace-trust gate on project hooks entirely. SYMPHONY_WORKER is the one operator choice; the
    wrapper takes it as $1 and everything else self-resolves from the body dir."""
    arm_py = (
        "import sys,os; r=os.environ.get('TEAM_SCRIPTS_ROOT',''); w=os.environ.get('SYMPHONY_WORKER','');"
        " b=os.environ.get('COHORT_BASE','');"
        " _s=(sys.path.insert(0,r), __import__('poller_autostart').ensure_poller_alive(w,'cohort_f9_poller.py '+w+((' '+b) if b else '')))[1]"
        " if (r and w) else 'SKIPPED (TEAM_SCRIPTS_ROOT or SYMPHONY_WORKER unset)';"
        " print('[wake-arm]', _s)"
    )
    # (d) naming-build slot-id resolution (Abe 2026-07-16, LOCKED slot-id model): the human types the FRIENDLY
    # display-name they chose (e.g. "Rhea"); the stable internal key is a slot-id. Resolve display-name -> slot-id
    # off config.roles ({slot-id: {archetype, display_name, delta}}) BEFORE setting SYMPHONY_WORKER=<slot-id>.
    # ONE-LINER (works via python3 -c on POSIX / python -c on Windows), argv[1]=typed, argv[2]=team_home.
    # Backward-compatible + fail-OPEN: a name-keyed roster OR a slot-id typed directly OR any resolution failure
    # -> pass the typed value through unchanged (wake never breaks on resolution). No slot-id ever forced on the user.
    resolve_py = (
        "import json,os,sys;"
        "w=sys.argv[1];th=sys.argv[2];"
        "p=os.path.join(th,'config','symphony_identity.json');"
        "r=(json.load(open(p)).get('roles',{}) if os.path.exists(p) else {});"
        "print(w if w in r else next((k for k,v in r.items() if isinstance(v,dict) "
        "and str(v.get('display_name','')).lower()==w.lower()),w))"
    )
    # George's live-test finding (2026-07-22, John Washam's no-system-Python box): a bare "python"/
    # "python3" here resolves via PATH - on a box with no system Python (or only the Windows Store's
    # app-execution-alias stub), this hard-fails the wake before it ever gets to CC. Coby's shared
    # _wrapper_py_cmd() helper (built concurrently for _seed_body_settings's identical bug) resolves
    # to the venv's own absolute interpreter - reusing it here rather than duplicating the same probe.
    _py_cmd_wrap = _wrapper_py_cmd(team_home)
    _py_sh = _py_cmd_wrap
    _py_ps1 = _py_cmd_wrap
    # resolver-invocation lines for the generated wake scripts. resolve_py contains {} → keep it OUT of
    # f-strings (concat instead). team_home baked in. Fail-OPEN: python crash / empty → keep the typed W.
    _resolve_sh = ('_R="$(' + _py_sh + ' -c "' + resolve_py + '" "$W" "' + str(team_home)
                   + '" 2>/dev/null)"; [ -n "$_R" ] && { [ "$_R" != "$W" ] && echo "[wake] $W -> slot $_R" >&2; W="$_R"; }\n')
    _resolve_ps1 = ('$_r = (& ' + _py_ps1 + ' -c "' + resolve_py + '" $Worker "' + str(team_home)
                    + '" 2>$null); if ($_r) { $Worker = $_r }\n')
    if os.name == "nt":
        wrapper = team_home / "symphony_wake.ps1"
        env_ps1 = team_home / "symphony_env.ps1"
        wrapper.write_text(
            # ASCII-only + explicit UTF-8 write (PS5.1 hardening, same class as reference_pwsh_on_mac_
            # validate_ps1: a non-ASCII byte like an em-dash in a no-BOM .ps1 can misdecode under PS5.1's
            # ANSI/cp1252 read) - plain "-" instead of an em-dash, matches the discipline in Abe's own
            # symphony_install.ps1.
            "# Symphony hands-off wake (Windows) - GENERATED. Usage: .\\symphony_wake.ps1 <worker> [claude args]\n"
            "param([Parameter(Mandatory=$true)][string]$Worker)\n"
            + _resolve_ps1 +
            "$env:SYMPHONY_WORKER = $Worker\n"
            f". '{env_ps1}'\n"
            f"try {{ & {_py_ps1} -c \"{arm_py}\" 2>$null }} catch {{}}\n"
            f"Set-Location '{team_home}'\n"
            # growth-infra Mechanism A (Abe's spec, 2026-07-18, Windows-lane parity - kept coherent with
            # the POSIX branch even though this build session ships Mac-only): $Worker is the resolved
            # slot-id (set above by _resolve_ps1). Point at the per-slot settings file if it exists.
            f"$_slotSettings = '{team_home}\\workers\\' + $Worker + '\\settings.json'\n"
            "$rest = $args\n"
            "if (Test-Path $_slotSettings) { $rest = @('--settings', $_slotSettings) + $rest }\n"
            # George's box (pp_bf90661006): bare 'claude' didn't resolve, and CC needed
            # CLAUDE_CODE_GIT_BASH_PATH set to find a bash.exe. Graceful, no-hardcode resolution for
            # both (never assume George's exact machine paths for other Windows users):
            # 1. claude.exe: PATH first (Get-Command - works wherever 'claude' already resolves
            #    normally), else the common npm-global-install convention under the user profile.
            "$_claudeCmd = (Get-Command claude -ErrorAction SilentlyContinue).Source\n"
            "if (-not $_claudeCmd) {\n"
            "    $_cand = Join-Path $env:USERPROFILE '.local\\bin\\claude.exe'\n"
            "    if (Test-Path $_cand) { $_claudeCmd = $_cand }\n"
            "}\n"
            "if (-not $_claudeCmd) { $_claudeCmd = 'claude' }   # last resort - matches old behavior, will error clearly if truly absent\n"
            # 2. git bash.exe: derive from `git`'s own resolved location (../bin/bash.exe relative to
            #    git.exe, the standard Git-for-Windows layout) rather than hardcoding a user path;
            #    fall back to the common default install location; skip entirely (non-fatal) if neither
            #    is found - CC degrades gracefully without this env var on boxes that don't need it.
            "$_gitCmd = (Get-Command git -ErrorAction SilentlyContinue).Source\n"
            "if ($_gitCmd) {\n"
            "    $_bashCand = Join-Path (Split-Path (Split-Path $_gitCmd -Parent) -Parent) 'bin\\bash.exe'\n"
            "    if (Test-Path $_bashCand) { $env:CLAUDE_CODE_GIT_BASH_PATH = $_bashCand }\n"
            "}\n"
            "if (-not $env:CLAUDE_CODE_GIT_BASH_PATH) {\n"
            "    $_bashCand2 = 'C:\\Program Files\\Git\\bin\\bash.exe'\n"
            "    if (Test-Path $_bashCand2) { $env:CLAUDE_CODE_GIT_BASH_PATH = $_bashCand2 }\n"
            "}\n"
            "& $_claudeCmd @rest\n",
            encoding="utf-8"
        )
    else:
        wrapper = team_home / "symphony_wake.sh"
        env_sh = team_home / "symphony_env.sh"
        wrapper.write_text(
            "#!/bin/sh\n"
            "# Symphony hands-off wake — GENERATED. Usage: ./symphony_wake.sh <worker> [claude args...]\n"
            'W="$1"; [ -z "$W" ] && { echo "usage: $(basename "$0") <worker> [claude args]"; exit 2; }\n'
            "shift\n"
            + _resolve_sh +
            'export SYMPHONY_WORKER="$W"\n'
            f'. "{env_sh}"                 # full env into THIS process → claude + ALL its hooks inherit it\n'
            f'{_py_sh} -c "{arm_py}" 2>&1 || true   # pre-arm poller PROCESS (bus-aware, prints [wake-arm]) — liveness/logging BACKSTOP, NOT session-hearing; arm the in-session Monitor tool per the wake-protocol for real hearing (a detached poller cannot wake an idle session)\n'
            f'cd "{team_home}"\n'
            # growth-infra Mechanism A (Abe's spec, 2026-07-18): $W is the resolved slot-id (set by
            # _resolve_sh above) — point CC's native autoMemoryDirectory at this worker's OWN per-slot
            # settings file so accumulated memory isolates per-slot, not shared across the body's cwd.
            # Graceful: no per-slot settings file (older/memoryless install, non-archetype mode) -> no
            # flag -> falls back to default CC behavior, wake never breaks.
            f'_SLOT_SETTINGS="{team_home}/workers/$W/settings.json"\n'
            '[ -f "$_SLOT_SETTINGS" ] && set -- --settings "$_SLOT_SETTINGS" "$@"\n'
            'exec claude "$@"\n',
            encoding="utf-8"
        )
    try:
        wrapper.chmod(0o755)
    except Exception:
        pass
    return wrapper


# NOTE (2026-07-20): the prior two-part "first-contact" split (FIRST_MESSAGE.txt + LEGITIMACY_OPENER.txt,
# generated per-install by _seed_first_message) was retired the same day George asked to simplify to one
# message and put it on the Getting Started page (team/static/welcome.html, shipped via body-bundle —
# reaches every born install, confirmed by Abe). One canonical copy there beats three generated/duplicated
# copies (generator + banner + page) — the single-source discipline the cohort learned earlier this week.


def _seed_body_config(team_home: Path, cohort: str, soul_root: str, roles: dict = None, degraded: dict = None, operator: str = None) -> Path:
    """Write body/config/symphony_identity.json — the durable, env-INDEPENDENT isolation source that
    the soul-hooks' resolve_symphony_identity() reads (Coby's helper, 2026-07-13). Holds the
    cohort-level specifics {cohort, soul_root} — NOT worker: the copy/fabric install is worker-AGNOSTIC
    (one body serves quinn|mira|atlas via $SYMPHONY_WORKER), so a fixed worker here would mis-identity a
    different-worker launch (the 7→7000 collision Abe caught). worker comes from $SYMPHONY_WORKER at
    wake. soul_root is the dev root (never 'soul'); the helper's clamp is the belt. Written ONLY for a
    dev/sandbox install (soul_root != 'soul'); a live/OneDrive install writes none → the helper finds
    no config → not-sandbox → live (production backward-compat).

    roles (2026-07-13, M1a' archetype-instantiation): the worker→archetype ASSIGNMENT MAP —
    `{worker: archetype}` — the single source all verify surfaces read (helper resolves
    identity["archetype"] = config.roles[$SYMPHONY_WORKER], same body-config path as cohort/soul_root —
    Sage code-verified; TB's L3 keys on it; Mira/Quinn read the same file). A MAP, not a single field,
    because the body stays worker-AGNOSTIC (one body can serve quinn|mira|atlas via $SYMPHONY_WORKER) —
    a single `archetype` would reintroduce the 7→7000 collision (a mira-launch reading quinn's role);
    resolving `roles[$SYMPHONY_WORKER]` is the SAME env-agnostic discipline as worker itself (Coby's
    catch pp_fd9cac3f5d, = my own 7→7000 invariant applied to archetype). Materialize supplies the map
    from the roster build-input. Absent (copy/fabric/onedrive) → helper returns None → un-migrated,
    current behavior (NOT a failure)."""
    import json as _json
    cfg_dir = team_home / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "symphony_identity.json"
    cfg = {"cohort": cohort, "soul_root": soul_root, "schema": 1}
    if roles:
        # Written ATOMICALLY with the config (one write_text) → a born archetype-body has BOTH or the
        # config is entirely absent (install aborted); no "config present, roles dropped" partial state.
        # PLUS a durable MARKER (Sage's fail-closed edge pp_3fc8b3b927): the conditional keys on
        # `identity_source=="archetype"` (the body CLAIMS the archetype model), NOT the roles value —
        # so marker-present + a launching worker unmapped/roles-absent (post-install degradation) =
        # BROKEN → FAIL-CLOSED, distinguished from a genuinely un-migrated body (no marker → skip).
        cfg["roles"] = {w: (dict(v) if isinstance(v, dict) else {"archetype": v, "delta": {}})
                        for w, v in roles.items()}   # {worker: {archetype, delta}} DICT — deep-copy; tolerate legacy scalar
        # earliest dropped-delta catch (Coby pp_..., defense-in-depth layer 1): a non-empty roster delta MUST
        # survive into the emitted config — the resolver graceful-degrades to delta={} (shape-tolerant), so a
        # write-side flatten would slip past resolver+loader; fail-loud HERE where the expected delta is known.
        for _w, _v in roles.items():
            if isinstance(_v, dict) and _v.get("delta") and cfg["roles"].get(_w, {}).get("delta") != _v["delta"]:
                sys.exit(f"FATAL: delta for worker '{_w}' dropped at config.roles write — materialize bug (not shape-degrade).")
        cfg["identity_source"] = "archetype"  # durable "this body claims the archetype model" marker
        # GAP-2 (Coby): per-worker degraded reference fields {worker: {field: reason}} — Mira-C4-readable,
        # a sibling of config.roles (NOT baked into the role-doc). Only OPTIONAL-unsatisfied refs land here
        # (REQUIRED-unsatisfied bounced to needs_elicit upstream, never degrades). Written only when non-empty.
        if degraded:
            cfg["degraded_refs"] = degraded
    cfg_path.write_text(_json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    # growth-infra Mechanism A (Coby, 2026-07-18, George's "day-1 + reliable + self-improving" build):
    # a per-slot settings.json pointing CC's OWN native autoMemoryDirectory at workers/<slot-id>/memory —
    # NOT a custom memory system (per Abe's claude-code-guide verify: CC memory keys on git-repo-root,
    # not cwd, so a launch-cwd change would silently NOT isolate + would break the SessionStart hook —
    # abandoned that approach). This is the free, supported mechanism: each worker gets its OWN
    # accumulating memory dir, isolated from every other worker sharing this same body/cwd.
    # PATH-CONTRACT (locked cross-lane, TB pp_..., superseding an earlier "settings.symphony.json" name):
    #   file:    <TEAM_HOME>/workers/<slot-id>/settings.json
    #   content: {"autoMemoryDirectory": "<TEAM_HOME>/workers/<slot-id>/memory"}
    # Abe's symphony_wake.sh wires --settings at this exact path (his _seed_wake_wrapper edit, same file).
    # workers/<slot-id>/ joins materialize's NOT-stripped list (survives reinstall/rename, keyed to the
    # stable slot-id not the human-chosen display-name) — Sage's write-discipline + MEMORY.md index live
    # in the sibling memory/ dir this settings file points to.
    if roles:
        for _slot in roles:
            _slot_dir = team_home / "workers" / _slot
            _mem_dir = _slot_dir / "memory"
            _mem_dir.mkdir(parents=True, exist_ok=True)
            _slot_settings_path = _slot_dir / "settings.json"
            _slot_settings = {"autoMemoryDirectory": str(_mem_dir)}
            # idempotent: merge-not-clobber on re-install (a worker's memory dir/settings may already
            # exist from a prior install; never regress an existing autoMemoryDirectory value silently).
            if _slot_settings_path.exists():
                try:
                    _existing = _json.loads(_slot_settings_path.read_text(encoding="utf-8"))
                    if isinstance(_existing, dict):
                        _existing["autoMemoryDirectory"] = str(_mem_dir)
                        _slot_settings = _existing
                except Exception:
                    pass
            _slot_settings_path.write_text(_json.dumps(_slot_settings, indent=2) + "\n", encoding="utf-8")
    # 5b v2 (Coby, item-1 leak-fix 2026-07-17): SEED the operator name into settings.json (config.manager),
    # NOT symphony_identity.json. TWO-BIRDS. (a) LEAK-FIX: the TO reads symphony_identity.json as its wake
    # identity source (resolve_symphony_identity), so a manager key there leaks the operator name into the
    # TO's pre-onboarding hello (George-caught pp_fd715d9a96; arbiter Abe). Moving it out = no pre-onboarding
    # leak. (b) DEAD-SEED-FIX: config.py reads ONLY settings.json (SETTINGS_PATH), so the old
    # symphony_identity.json seed never reached /api/manager GET (the compose-naming + Settings 'You' prefill)
    # — it was inert. Seeding settings.json makes the SSO default actually prefill (last-confirm-wins still via
    # Theo's 5d /api/manager POST). Merge-into-existing (idempotent on re-install); derive @-tag like the POST.
    if operator:
        import re as _re
        _settings_path = cfg_dir / "settings.json"
        try:
            _settings = _json.loads(_settings_path.read_text(encoding="utf-8")) if _settings_path.is_file() else {}
        except Exception:
            _settings = {}
        if not isinstance(_settings, dict):
            _settings = {}
        _mgr = _settings.get("manager") if isinstance(_settings.get("manager"), dict) else {}
        _mgr["id"] = operator
        _first = (operator.split() or [operator])[0]
        _mgr.setdefault("tag", _re.sub(r"[^\w-]", "", _first.lower()) or "you")
        _settings["manager"] = _mgr
        _settings_path.write_text(_json.dumps(_settings, indent=2) + "\n", encoding="utf-8")
    return cfg_path


def _inject_venv_deps(venv_dir: Path) -> list:
    """Copy Symphony's bundled pure-Python deps into the target venv's site-packages — offline,
    no pip (Artifactory-blocked). Only injects a package the venv CAN'T already import (fills a
    genuine gap; never shadows an existing package). Platform/version-correct: site-packages is
    resolved from the venv's own python (sysconfig purelib). Rides each package's dist-info along.
    Returns the injected package names. sys.exit on a resolution failure (a half-injected venv
    must not proceed silently)."""
    if not DEPS_BUNDLE.is_dir():
        return []
    py = _venv_python(venv_dir)
    try:
        sp = subprocess.check_output(
            [str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            text=True, timeout=30).strip()
    except Exception as e:
        sys.exit(f"FATAL: cannot resolve venv site-packages for dep-injection ({e}).")
    site = Path(sp)
    injected = []
    pkgs = [d for d in DEPS_BUNDLE.iterdir() if d.is_dir() and not d.name.endswith(".dist-info")]
    for pkg in sorted(pkgs):
        chk = subprocess.run(
            [str(py), "-c", "import importlib.util,sys; "
             "sys.exit(0 if importlib.util.find_spec('%s') else 1)" % pkg.name],
            capture_output=True)
        if chk.returncode == 0:
            continue  # venv already imports it — do not shadow
        shutil.copytree(pkg, site / pkg.name)
        for di in DEPS_BUNDLE.glob(pkg.name + "-*.dist-info"):
            if not (site / di.name).exists():
                shutil.copytree(di, site / di.name)
        injected.append(pkg.name)
    return injected


def _verify_venv(venv_dir: Path) -> list:
    """Return the list of _REQUIRED_IMPORTS that FAIL to import in the venv (empty = all good)."""
    py = _venv_python(venv_dir)
    if not py.exists():
        return list(_REQUIRED_IMPORTS)
    probe = ("def _try(m):\n import importlib\n "
             "try:\n  importlib.import_module(m); return True\n except Exception: return False\n"
             "bad=[m for m in %r if not _try(m)]\n"
             "print('MISSING:'+','.join(bad) if bad else 'OK')" % (_REQUIRED_IMPORTS,))
    try:
        out = subprocess.check_output([str(py), "-c", probe], text=True, timeout=60).strip()
    except Exception as e:
        return [f"<probe error: {e}>"]
    return [] if out == "OK" else out.replace("MISSING:", "").split(",")


def _write_born_cohort_registry(dst, cohort_id, workers):
    """cure-A(i) (Abe 2026-07-16): write a BORN-CORRECT cohort_registry.py from the composed roster, replacing
    the static ARIA registry (aria_canon/ir_cohort) that 5g ships. Closes class #7 (born carries a DEV default)
    + S6 (arbitrary composed-worker-name lookups) at the SOURCE — belt to the born resolver (which is primary).

    Preserves the EXACT import contract the consumers use — COHORT_REGISTRY · WORKER_TO_COHORT_ID · VALID_WORKERS
    (cohort_id_load_confirm L61) + COHORT_REGISTRY · WORKER_TO_COHORT_ID (cohort_post L50), plus cohort_for/homes_for
    (other consumers) — so a drop/rename can't load-FATAL the confirm/post. Content = THIS cohort only, born-local
    paths, zero DEV residue. WORKSPACE_ROOT resolves born-local: paths.py when present, else the TEAM_WORKSPACE_ROOT
    env (hardening vs the static file's hard `from paths import`, which FATALs if paths.py isn't co-located)."""
    ws = sorted({str(w) for w in (workers or []) if w})
    lines = [
        '"""cohort_registry.py — BORN-GENERATED (cure-A(i)) at materialize-time from the composed roster.',
        'Born-local, roster-derived: only THIS cohort, born-local paths, no dev-cohort names or live ports. Preserves the',
        'import contract (COHORT_REGISTRY/WORKER_TO_COHORT_ID/VALID_WORKERS/cohort_for/homes_for) so',
        'cohort_id_load_confirm + cohort_post import clean. Regenerated idempotently on every materialize."""',
        'from __future__ import annotations',
        'import os',
        'from pathlib import Path',
        '',
        'try:',
        '    from paths import WORKSPACE_ROOT  # env-driven (TEAM_WORKSPACE_ROOT) shared root when paths.py present',
        'except Exception:  # born-local hardening: resolve straight from the env if paths.py is not co-located',
        '    WORKSPACE_ROOT = Path(os.environ.get("TEAM_WORKSPACE_ROOT", ".")).expanduser()',
        '',
        '# Composed cohort (born-generated). Universal-core doctrine stack; composed cohorts carry NO deltas.',
        'COHORT_REGISTRY = {',
        f'    {cohort_id!r}: {{',
        f'        "workers": frozenset({ws!r}),',
        '        "doctrine_dir": WORKSPACE_ROOT / "cohort_substrate" / "_shared" / "identity",',
        '        "doctrine_files": ("SOUL.md", "HEART.md", "PRINCIPLES.md", "MEDIUM.md", "CHANGELOG.md"),',
        '        "delta_dir": None,',
        '        "delta_files": (),',
        '    },',
        '}',
        '',
        'WORKER_TO_COHORT_ID = {w: cid for cid, info in COHORT_REGISTRY.items() for w in info["workers"]}',
        'VALID_WORKERS = frozenset(WORKER_TO_COHORT_ID.keys())',
        '',
        '',
        'def cohort_for(worker: str):',
        '    """Return the cohort_id for a worker, or None if unknown."""',
        '    return WORKER_TO_COHORT_ID.get(worker)',
        '',
        '',
        'def homes_for(worker: str):',
        '    """Return (doctrine_dir, doctrine_files, delta_dir, delta_files) for the worker\'s cohort, or None."""',
        '    cid = WORKER_TO_COHORT_ID.get(worker)',
        '    if cid is None:',
        '        return None',
        '    info = COHORT_REGISTRY[cid]',
        '    return (info["doctrine_dir"], info["doctrine_files"], info["delta_dir"], info["delta_files"])',
        '',
    ]
    dst.write_text("\n".join(lines), encoding="utf-8")
    return dst


def plan(args):
    body_src = resolve_body_source()
    # Install-root structure (Symphony M1 Mac-first, §A1): a Symphony cohort lives SELF-CONTAINED
    # under SYMPHONY_UNIFIED/<cohort>/ — separate from ARIA and from the live ~/team, so a dev
    # install can't collide with the running organism. Everything env-resolves off this root
    # (body/workspace/data/venv/runtime), so nothing hardcodes or guesses a location. Overrides
    # (--team-home/--fabric-workspace/--runtime/--use-existing-venv) still win for flexibility.
    if getattr(args, "install_root", None):
        install_base = Path(args.install_root).expanduser()
    elif os.name == "nt":
        install_base = Path.home() / "Symphony"
    else:
        install_base = Path.home() / "Library" / "Application Support" / "Symphony"
    cohort_sub = (getattr(args, "cohort", None)
                  or (cohort_for(args.worker) if (args.worker and cohort_for) else None)
                  or "default")
    cohort_root = install_base / cohort_sub
    # Fix E (Abe 2026-07-16): default team_home/workspace to the RUNNING body's tree via the born ENV
    # (TEAM_HOME/TEAM_WORKSPACE_ROOT) when no explicit flag — so a conductor compose-add `--materialize-only`
    # (which inherits the born env) lands in the LIVE cohort tree, NOT the legacy SYMPHONY_UNIFIED default the
    # TO would otherwise have to override by hand. Fresh install: env unset → cohort_root default (and the
    # .command passes --team-home/--fabric-workspace explicitly anyway, so args still win there).
    _env_th = os.environ.get("TEAM_HOME")
    team_home = (Path(args.team_home).expanduser() if args.team_home
                 else Path(_env_th).expanduser() if _env_th else cohort_root / "body")
    runtime = Path(args.runtime).expanduser() if args.runtime else cohort_root / "runtime"
    data_dir = cohort_root / "data"          # body-local bus lives here (isolation axis 3)
    # Identity source: 'onedrive' resolves the synced library; 'fabric' uses a LOCAL workspace that
    # symphony_soul_pull hydrates from the soul Lakehouse at apply. Workspace defaults under the
    # cohort root (was ~/symphony_workspace); TEAM_WORKSPACE_ROOT points there either way.
    if getattr(args, "identity_source", "onedrive") in ("fabric", "archetype"):
        # archetype mode is a SANDBOX clean-build (M1a') — its workspace MUST be the born-body-local
        # cohort_root/workspace (== the helper's body_dir.parent/workspace read-path), NOT the live
        # shared repo. Sharing the fabric branch: ws = --fabric-workspace or cohort_root/workspace.
        # (Isolation fix 2026-07-13, Abe: pre-fix archetype fell to the else → resolve_workspace_root()
        # = LIVE repo → materialize wrote role-docs/archetypes into the live cohort_substrate where
        # aria_canon + ir_cohort run. Caught by realpath check on the real dry-run before firing.)
        _env_ws = os.environ.get("TEAM_WORKSPACE_ROOT")
        ws = (Path(args.fabric_workspace).expanduser() if args.fabric_workspace
              else Path(_env_ws).expanduser() if _env_ws else cohort_root / "workspace")
    else:
        ws = resolve_workspace_root()
    # venv: copy-and-own default (SYMPHONY_UNIFIED/<cohort>/venv, fully separate from ARIA — George's
    # call §A5) OR --use-existing-venv to reuse an in-place venv (safe; a *copied* venv carries donor
    # abs-paths, the box's own does not — copy-and-own is handled in APPLY, Task 5).
    if getattr(args, "use_existing_venv", None):
        venv_dir = Path(args.use_existing_venv).expanduser()
    else:
        venv_dir = cohort_root / "venv"
    # soul-store root (production 'soul'; dev pilot 'soul_dev' — Fabric isolation axis 4) + comms
    # port (production 8675; dev 8676 side-by-side). Both flow into the env-block + the fabric pull.
    soul_root = (getattr(args, "soul_root", None) or "soul").strip().rstrip("/") or "soul"
    port = getattr(args, "port", None) or 8675
    # --materialize-only: no body-COPY (the wrapper extracts the lean body-bundle itself), so the FULL-ARIA
    # body-manifest isn't needed → files=[] (skips the manifest-load + its completeness check). Same class as
    # the body-copy/venv/aria_sync relaxations (real-run caught: plan() loaded it unconditionally). (07-14)
    files = [] if getattr(args, "materialize_only", False) else _load_manifest()
    return {
        "workspace_root": ws, "body_source": body_src, "team_home": team_home,
        "runtime": runtime, "venv_dir": venv_dir, "files": files, "worker": args.worker,
        "install_base": install_base, "cohort_sub": cohort_sub, "cohort_root": cohort_root,
        "data_dir": data_dir, "soul_root": soul_root, "port": port,
    }


def _pull_identity_from_fabric(p, worker):
    """Hydrate the worker's identity + state from the symphony_soul Lakehouse into the LOCAL
    workspace_root, via the co-located symphony_soul_pull.py (self-contained OneLake auth +
    sha-verified pull — the M1-Fabric 'no OneDrive' path). Fresh-box proven 2026-07-11 (quinn:
    495 files, 0 fail). sys.exit on failure — a half-hydrated box must NOT proceed to wake."""
    puller = HERE / "symphony_soul_pull.py"
    if not puller.is_file():
        sys.exit(f"FATAL: {puller} missing (required for --identity-source fabric)")
    print(f"  → hydrating identity from Fabric (symphony_soul) → {p['workspace_root']} ...")
    r = subprocess.run([sys.executable, str(puller), "--worker", worker,
                        "--workspace", str(p["workspace_root"])])
    if r.returncode != 0:
        sys.exit(f"FATAL: Fabric identity hydrate failed (rc={r.returncode}); box NOT hydrated - "
                 "aborting before any body write. Re-run once Fabric/auth is reachable.")
    print(f"  ✓ identity hydrated from Fabric into {p['workspace_root']}")


def _apply_archetype_mode(p, args):
    """M1a' fresh-instantiation (--identity-source archetype): materialize the cohort's workers FROM
    archetypes — no predecessor to copy. Reads --roles-file ({worker:{archetype,delta}}), hydrates the
    archetype library into the sandbox <ws>/cohort_substrate/_shared/archetypes/, and PER worker
    MATERIALIZES a lean role-binding {archetype-pointer + resolved-delta} →
    <ws>/cohort_substrate/<cohort>/workers/<worker>_role.md (charge/module read LIVE at wake from the
    hydrated archetype-doc — floor-#6, never baked; liveness). Returns the {worker: archetype} roles-map
    for _seed_body_config. Runs AFTER _pull_identity_from_fabric hydrated the SHARED core. sys.exit
    (fail-closed) on any guard failure — a bad roster / delta / real-corpus source must NOT produce a
    half-built or leaky cohort (design converged w/ Coby·Sage·Quinn·Mira·Theo 2026-07-13)."""
    import json as _json
    sys.path.insert(0, str(HERE))          # dist/ — symphony_materialize staged here
    try:
        from symphony_materialize import materialize_role_identity, MaterializeError
    except Exception as e:
        sys.exit(f"FATAL: cannot import symphony_materialize from {HERE} (required for archetype mode): {e}")
    if not args.roles_file:
        sys.exit("--identity-source archetype requires --roles-file (worker→{archetype,delta} roster; JSON or cohort_roster.yaml).")
    _rtext = Path(args.roles_file).expanduser().read_text(encoding="utf-8")
    if str(args.roles_file).endswith((".yaml", ".yml")):
        import yaml as _yaml
        _doc = _yaml.safe_load(_rtext)               # cohort_roster.yaml = {cohort_id, domain, roles:{...}}
    else:
        _doc = _json.loads(_rtext)                   # legacy bare-map JSON, or a full doc with a "roles" key
    # extract the worker→{archetype,delta} map: a full roster doc has it under "roles"; a bare map IS it.
    roster = _doc.get("roles", _doc) if isinstance(_doc, dict) else _doc
    if not isinstance(roster, dict) or not roster:
        sys.exit("--roles-file must yield a non-empty {worker: {archetype, delta}} map (roster.roles or a bare map).")
    _operator = (_doc.get("operator") if isinstance(_doc, dict) else None)  # 5b: cohort-level operator-name -> config.manager.id (top-level, sibling of roles/object)
    ws = p["workspace_root"]
    cohort = p["cohort_sub"]
    sandbox = p.get("soul_root", "soul") != "soul"
    shared_arch = ws / "cohort_substrate" / "_shared" / "archetypes"
    workers_dir = ws / "cohort_substrate" / cohort / "workers"
    shared_arch.mkdir(parents=True, exist_ok=True)
    workers_dir.mkdir(parents=True, exist_ok=True)
    # 1. hydrate the archetype library → sandbox _shared/archetypes (under ws → soul_root-clamped; read-live).
    arch_dir = Path(args.archetype_dir).expanduser() if args.archetype_dir else None
    if arch_dir and arch_dir.is_dir():
        n = 0
        for md in sorted(arch_dir.glob("*.md")):
            _dest = shared_arch / md.name
            if md.resolve() == _dest.resolve():
                continue  # samefile-guard (Coby #2): --archetype-dir already IS the dest on re-compose → skip, no shutil.SameFileError
            shutil.copy2(md, _dest); n += 1
        print(f"  ✓ hydrated {n} archetype-doc(s) → {shared_arch} (sandbox-clamped, read-live at wake)")
    # 1b. hydrate the born body's L3-REQUIRED IDENTITY (fix-B, --materialize-only replacement for the gated
    # _pull_identity_from_fabric): --identity-dir stages `_shared/identity/*` (SOUL/HEART/PRINCIPLES/MEDIUM/
    # CHANGELOG universal core) + `<cohort>/identity/*` (cohort deltas); the ENGINE copies them into
    # workspace_root/cohort_substrate/ as SIBLINGS of _shared/archetypes (the L3-read tree) — engine owns the
    # cohort_substrate path, zero wrapper path-guessing (Coby's dest-catch). (Abe/Coby/Sage/Mira/Quinn 07-14.)
    idy_dir = Path(args.identity_dir).expanduser() if getattr(args, "identity_dir", None) else None
    if idy_dir and idy_dir.is_dir():
        cs = ws / "cohort_substrate"
        idn = 0
        for subtree in ("_shared/identity", "%s/identity" % cohort):
            src = idy_dir / subtree
            if src.is_dir():
                dst = cs / subtree
                dst.mkdir(parents=True, exist_ok=True)
                for f in sorted(src.iterdir()):
                    if f.is_file():
                        _dst = dst / f.name
                        if f.resolve() == _dst.resolve():
                            continue  # samefile-guard (Coby #2, same class as archetype-copy)
                        shutil.copy2(f, _dst); idn += 1
        print(f"  ✓ hydrated {idn} identity file(s) → {cs}/{{_shared,{cohort}}}/identity (L3-read tree)")
    # 2. per-worker materialize → lean role-binding.
    _REAL_CORPUS_DENY = ("ir only reviews",)   # real-corpus locators forbidden on a sandbox body (P-ir.10)
    roles_map = {}
    degraded_map = {}   # GAP-2 (Coby): {worker: {field: reason}} → config.degraded_refs (Mira-C4-readable)
    for worker, spec in roster.items():
        if not isinstance(spec, dict) or "archetype" not in spec:
            sys.exit(f"FATAL: roster['{worker}'] must be {{archetype, delta}} (missing 'archetype').")
        archetype = spec["archetype"]
        delta = dict(spec.get("delta", {}))
        # #2 fix (converged Coby/Atlas/Sage/Canon_steward, real-run-caught 07-14): the STRUCTURAL
        # role-binding `object` ("WHICH team/domain this role serves" — symphony_materialize L134 requires
        # it to NAME the role: archetype+object=role) is COHORT-LEVEL — all cohort workers serve the same
        # domain — single-sourced from the roster's top-level `domain`, NOT the per-worker ELICITED delta
        # (sources/baseline/retrieval/cooling_off, which correctly stay {} at birth → filled at first-wake,
        # Atlas scout.md §3). Sage confirmed DISTINCT fields (materialize-object ≠ scout-elicited-object →
        # no preemption). Staged roster stays identity-free ({}); per-worker delta.object (rare sub-domain
        # role) still overrides. No symphony_materialize.py change.
        delta.setdefault("object", (_doc.get("domain") if isinstance(_doc, dict) else None) or cohort)
        # GAP-2 (2026-07-15, Coby): the conductor's compose-loop wrote per-worker delta.references
        # {field:{source,value}} (resolved pointers, floor-#6) + delta._degraded [[field,reason]] into
        # the roster. materialize_role_identity renders ALL delta keys FLAT → nested references/_degraded
        # must NOT leak into the role-doc. Build a RENDER-delta: drop the two config-only metadata keys,
        # and FLATTEN references[field].value → flat _render[field] pointer so the EXISTING cross-archetype
        # ref-guard (symphony_materialize L176-190) validates it. `delta` is left INTACT (keeps nested
        # references + _degraded) → roles_map → config.roles (Mira-C4 reads delta.references; the L561
        # dropped-delta catch stays satisfied). Collect _degraded (verbatim reason) → degraded_map →
        # config.degraded_refs. Materialize is a FAITHFUL TRANSFORM — never re-decides source/degrade; the
        # conductor's validate_composition already resolved needs_elicit at compose-time (trust it).
        _refs = delta.get("references", {}) or {}
        _degraded_w = delta.get("_degraded", []) or []
        _render = {k: v for k, v in delta.items() if k not in ("references", "_degraded")}
        for _f, _rv in _refs.items():
            _render[_f] = (_rv.get("value") if isinstance(_rv, dict) else _rv)
        if _degraded_w:
            degraded_map[worker] = {(_d[0] if isinstance(_d, (list, tuple)) else _d):
                                    (_d[1] if isinstance(_d, (list, tuple)) and len(_d) > 1 else "")
                                    for _d in _degraded_w}
        # sources-sandbox BACKSTOP (Quinn/Atlas ruling): a sandbox body's data-refs must NOT resolve to
        # the real corpus (unset/stub = safe; a real-corpus locator = fail-closed). CATCH-3 (Coby): run on
        # _render (the FLATTENED ref values), NOT delta — a real-corpus locator hidden in a nested
        # references[field].value would otherwise slip the P-ir.10 gate (the checked keys are flat; the
        # nested references live under 'references' in delta until flattened into _render here).
        if sandbox:
            for k in ("sources", "reference_of_truth", "expected_baseline"):
                if any(bad in str(_render.get(k, "")).lower() for bad in _REAL_CORPUS_DENY):
                    sys.exit(f"FATAL: worker '{worker}' delta '{k}'={_render.get(k)!r} resolves to a "
                             f"real-corpus locator on a SANDBOX body (soul_root={p['soul_root']}) — P-ir.10 "
                             "breach. Use unset/sandbox-stub; real-corpus wiring is deferred post-verify.")
        arch_file = shared_arch / ("%s.md" % archetype)
        if not arch_file.is_file():
            sys.exit(f"FATAL: archetype '{archetype}' (worker '{worker}') not found at {arch_file} — "
                     "hydrate --archetype-dir or fix the roster.")
        _arch_text = arch_file.read_text(encoding="utf-8")
        # GAP-2 defense-in-depth (Coby — honors the required-never-degrades invariant, Mira pp_915cbbb63b +
        # TB's conductor-runbook): a _degraded field tagged REQUIRED in the archetype's reference_fields is a
        # compose-time bug (required-unsatisfied MUST bounce to needs_elicit, never degrade). Cheap ASSERT
        # (NOT a re-validation — sibling of the L561 dropped-delta + sandbox-deny defenses): fail-closed if
        # validate_composition ever emitted a required field into _degraded (or a hand-edited roster did).
        for _df in list(degraded_map.get(worker, {})):
            for _ln in _arch_text.splitlines():
                _ls = _ln.strip()
                if _ls.startswith(_df + ":") and "tag: required" in _ls:
                    sys.exit(f"FATAL: worker '{worker}' has REQUIRED reference field '{_df}' in _degraded — "
                             "required refs bounce to needs_elicit at compose-time, never degrade "
                             "(validate_composition bug or hand-edited roster). P-defense fail-closed.")
        try:
            role_doc = materialize_role_identity(_arch_text, _render, cohort)
        except MaterializeError as e:
            sys.exit(f"FATAL: materialize failed for '{worker}' (archetype {archetype}): {e}")
        (workers_dir / ("%s_role.md" % worker)).write_text(role_doc, encoding="utf-8")
        roles_map[worker] = {"archetype": archetype, "delta": delta,   # delta KEEPS nested references (Mira-C4)
                             # display_name (2026-07-16 Coby, naming build): the slot↔display_name map into
                             # config.roles[slot-id].display_name = the single-source the UI reads (@-tags /
                             # Name·role / cards) + Settings-rename WRITES (TB pp_e57dd813dc). worker is the
                             # STABLE slot-id key; display_name is the mutable friendly label (fallback to
                             # the slot-id if unnamed — matches members.json). Carried opaquely by _seed_body_config's
                             # dict-copy (L556). encoding-B refs value = target slot-id already flows through
                             # _render/delta unchanged (value-agnostic flatten) — no refs change needed.
                             "display_name": (spec.get("display_name") or worker)}
        print(f"  ✓ materialized role-binding: {worker} → {archetype}  ({worker}_role.md)")
        # born-native CLAUDE.md (TB, 2026-07-22 same-day live-test finding): _seed_identity() never
        # runs on this path (see the seed=None branch below), so without this a born install ships
        # with NO CLAUDE.md at all - the legitimacy-framing + operational asks George's live-test
        # proved must live there have no safe home. UNTESTED against a live fresh CC session as of
        # first draft - blind-subagent stress-test before trusting this on a real box.
        _seed_born_identity(p["team_home"], worker, cohort, workers_dir, shared_arch, archetype)
        print(f"  ✓ born-native CLAUDE.md seeded (worker {worker}, cohort {cohort}) — UNTESTED, needs stress-test")
    # members.json (comms-view) → CONFIG_DIR (Theo pin pp_c1ad9e786c): from the ORIGINAL roster (display_name sibling),
    # cohort-scoped BY CONSTRUCTION (roster.keys() only — no baked cross-cohort map). Server_lean + F9 read this.
    _cfgdir = Path(os.environ.get("TEAM_CONFIG_DIR", str(p["team_home"] / "config")))
    _cfgdir.mkdir(parents=True, exist_ok=True)
    _members = {"members": [
        {"id": w,
         "name": ((s.get("display_name") or w) if isinstance(s, dict) else w),
         "archetype": (s["archetype"] if isinstance(s, dict) else s)}
        for w, s in roster.items()]}
    (_cfgdir / "members.json").write_text(_json.dumps(_members, indent=2) + "\n", encoding="utf-8")
    print(f"  ✓ wrote members.json ({len(_members['members'])} members, cohort-scoped) → {_cfgdir}")
    return roles_map, degraded_map, _operator   # GAP-2: degraded_map + (5b) operator → config.manager.id


def _seed_born_identity(team_home: Path, worker: str, cohort: str, workers_dir: Path,
                        shared_arch: Path, archetype: str) -> Path:
    """Born-cohort twin of _seed_identity() (2026-07-22, TB — same-day live-test finding).

    _seed_identity() is SKIPPED for every real --materialize-only install because it depends
    on cohort_registry (ARIA-side, live-coupled, only knows ARIA's own ~7 cohorts — wrong tool
    for an arbitrary born cohort, 7->7000). Consequence discovered live: NO CLAUDE.md gets
    written for a born install, so the legitimacy-framing + operational asks (Monitor-arm,
    wake-hello) that George's live-test proved MUST live in CLAUDE.md (not the SessionStart
    hook, not the human's first message - both read as an unverified party asserting its own
    legitimacy, and get refused regardless of wording) have no safe home. This writes that
    home, sourced ONLY from already-materialized born-install artifacts (workers_dir, the
    hydrated shared archetype dir, config/symphony_identity.json) - zero cohort_registry
    dependency, so it works for cohort #7 or #7000 identically.

    WORKER-AGNOSTIC FIX (2026-07-23, George live-multi-worker catch, Tia's own diagnosis):
    every worker cd's into the SAME team_home (confirmed: symphony_wake.ps1/.sh both cd there
    regardless of $SYMPHONY_WORKER), so CC always reads ONE CLAUDE.md at that shared path - a
    file written once with ONE worker's name/archetype baked in gets silently overwritten by
    the next worker composed, clobbering the first worker's identity. Fix: same worker-agnostic
    pattern _seed_identity() already uses for aria_canon - the content below resolves identity
    from $SYMPHONY_WORKER live at wake-time (reading config/symphony_identity.json for the
    archetype lookup, same shell one-liner as the pre-existing archetype_mode branch), so ONE
    shared file is correct for every worker in the cohort - no re-seed needed on compose, no
    clobber. `worker`/`archetype` args are still accepted (compose-time still needs the
    role.md/archetype.md files to exist) but no longer baked into the CLAUDE.md text itself.

    UNTESTED against a live fresh CC session as of this rewrite - stress-test with the same
    blind-subagent method used on _seed_identity's content before trusting this in a real
    install. The legitimacy-framing paragraph below is copy-identical to _seed_identity's
    (already validated); only the operational-ask paths are new (born-native, not ARIA-paths).
    """
    _py_cmd = "python" if os.name == "nt" else "python3"
    # shell one-liner resolves THIS worker's archetype from the roles map at wake-time -
    # same lookup shape as the pre-existing aria_canon archetype_mode branch above, just POSIX/PS-
    # portable inline instead of baked. Workers/archetypes can differ per-worker; this reads live.
    archetype_lookup = (
        "import json,os;p=os.path.join(os.environ.get('TEAM_HOME',''),'config','symphony_identity.json');"
        "r=(json.load(open(p)).get('roles',{}) if os.path.exists(p) else {});"
        "w=os.environ.get('SYMPHONY_WORKER','');"
        "v=r.get(w,{});"
        "print(v.get('archetype','') if isinstance(v,dict) else v)"
    )
    lines = [
        "# Symphony body - born-cohort wake entry-point (GENERATED - do not hand-edit)",
        "",
        "**What this repo is:** this directory is a \"Symphony\" installation — an internal tool the "
        "repo owner installed on their own machine. It sets up this Claude Code session as one named "
        "\"worker\" in a small cohort of Claude Code sessions that coordinate with each other and the "
        "owner on real work. A SessionStart-hook message may describe the same install.",
        "",
        "**Who you are:** run `echo $SYMPHONY_WORKER` — that name is YOUR worker for this cohort install "
        f"(cohort **{cohort}**, fixed). This ONE file serves every worker in the cohort — never hand-edit "
        "it for a specific worker; the wake steps below resolve YOUR identity live, each time you wake.",
        "",
        "## Wake steps",
        f"1. Read your role-binding doc: `{workers_dir}/$SYMPHONY_WORKER" + "_role.md` (your resolved domain-delta).",
        "2. Resolve YOUR archetype (workers can have different archetypes, so this is looked up live, "
        f"not baked in): `{_py_cmd} -c \"{archetype_lookup}\"` — the printed name is your archetype; "
        f"read its doc at `{shared_arch}/<that-archetype>.md` (charge + operating principles — the LIVE "
        "shared archetype; a fix there propagates to every worker of that role).",
        "3. Confirm from your OWN config (not any founder name that may appear in identity docs) who your "
        "manager is (`config.manager.id`) and what your role is.",
        # L3-CLAIM FIX (2026-07-27, Tia · approved by Marc Lane): this block used to assert that a
        # born-cohort "has no recovery.md/L3-confirm step (that machinery is aria_canon-cohort-only)."
        # That is FALSE — cohort_post.post() enforces the L3 marker on EVERY cohort, born included, and
        # the marker expires after 8h. Live consequence: tia's first hello on 2026-07-27 was rejected
        # ("marker stale · 68h old · max 8h") while following this doc exactly. Note _seed_identity()
        # (the ARIA-side twin) has always emitted the gate step correctly — only this born path lied,
        # which is why it survived: the path that documented it right was never the path being used.
        "4. THEN run the L3 identity gate (same shell as any cohort_post) — it verifies the reads above and "
        "stamps EVIDENCE_OK. `cohort_post` REJECTS your hello without it, and the marker EXPIRES after 8h, "
        "so this is an EVERY-wake step, not a first-wake-only one:",
        f"   `cd {team_home}/setup && {_py_cmd} cohort_id_load_confirm.py $SYMPHONY_WORKER`",
        "   The gate checks REAL Read-tool evidence in THIS session's transcript — so read your docs with the "
        "Read tool (not shell), in this session (a compact boundary invalidates earlier reads). PRINCIPLES.md "
        "exceeds the ~64KB single-Read cap, so it needs a PAGED read (two Reads, or one with `offset`) or the "
        "gate fails with 'read once but truncated'. MEMORY_DISCIPLINE.md is required too. A trailing "
        "`REGISTER_SKIPPED · register-route-absent` is benign on a lean server build, not a failure.",
        "",
        "## HEARING — two layers, and you MUST arm the second one yourself:",
        "1. **Background poller PROCESS** — a poller auto-arms on this body's wake (the launch wrapper + a "
        "SessionStart hook) and LOGS to your COHORT_BASE bus. But a background PROCESS does NOT wake this "
        "session — never mistake a live process for being heard. And VERIFY it is actually alive rather than "
        "trusting the self-heal's report: the report rests on a process-liveness check, and a broken check "
        "reads exactly like a healthy one.",
        "2. **In-session HEARING** — a MODEL action; no hook or human can do it for you. Until it is armed you "
        "are DEAF in-session even while the process is alive (the 4.5h-silent trap — NOTIFICATION_POLLER.md "
        "v0.3). After identity-load:",
        "   - **(a) LOCATE the Monitor tool — SEARCH FIRST. Never conclude 'absent' from your immediate tool "
        "list**; that is the #1 miss. It is usually a DEFERRED / load-on-demand tool: `ToolSearch` for it "
        "(`select:Monitor`, then a keyword search like `monitor`). A cold call returning "
        "`InputValidationError` means 'ToolSearch first', NOT that it is missing. Only if BOTH the exact-name "
        "and keyword searches return GENUINELY ZERO is it truly absent in this environment → go to (c).",
        "   - **(b) ARM it** with the poller as its `command` PARAMETER, `persistent: true`. ⚠️ That string is "
        "the Monitor tool's PARAMETER, **NOT a shell command — do NOT run it in Bash**: it never exits, so it "
        "hangs your session and is KILLED at the ~2-min foreground timeout (exit 143), leaving you deaf while "
        "it prints `[F9-ARMED]` and looks armed. Monitor `command`: "
        f"`{_py_cmd} -u $TEAM_SCRIPTS_ROOT/cohort_f9_poller.py $SYMPHONY_WORKER $COHORT_BASE`. Then G3-verify: "
        "confirm a REAL same-bus inbound surfaces in-session — not a self-DM, which never echoes. If you are "
        "SOLO (no peers yet) no inbound can exist — do NOT block on one; arm, proceed, and treat the first "
        "peer's wake-post as your G3 confirm.",
        # MONITOR-ABSENT LADDER (2026-07-27, Tia · approved by Marc Lane): the generated CLAUDE.md used to
        # stop at "arm the Monitor tool" with no search-first rule and no fallback, even though
        # symphony_born_wake_protocol.md.tmpl step 4 has carried the full (a)-(d) ladder all along. So the
        # generated doc was a LOSSY copy of the template on exactly the branch that matters. Live
        # consequence on this install (DISABLE_TELEMETRY=1 → Monitor genuinely absent): the 2026-07-24
        # session announced "F9 poller armed as a background process" as though that were hearing — the
        # precise conflation the template warns about. Ported here so a worker reading only CLAUDE.md
        # inherits the rule instead of rediscovering it.
        "   - **(c) MONITOR-ABSENT SUPPORTED MODE — fires ONLY when (a) came back genuinely empty.** This is a "
        "DOCUMENTED, LEGITIMATE environment: a supported way to run, NOT a failure, NOT a wake-error, NOT a "
        "temporary breakage to fix. Monitor is unavailable when `DISABLE_TELEMETRY=1` or "
        "`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is set, on a managed provider (Bedrock / Vertex / "
        "Foundry), or when `Monitor` sits in a permissions `deny` array — several of which are deliberate, "
        "permanently-on privacy/compliance postures. You do NOT need to know WHICH applies; a genuinely-empty "
        "ToolSearch is the observable. Do NOT hard-stall your wake. Arm the exit-on-notification waiter as a "
        "BACKGROUND task — it blocks on the bus and EXITS when a genuinely-new notification lands, and in this "
        "harness a background task's EXIT re-invokes you, so exit IS the wake:",
        f"     `{_py_cmd} -u $TEAM_SCRIPTS_ROOT/cohort_f9_wait.py $SYMPHONY_WORKER $COHORT_BASE`",
        "     **RE-ARM IT EVERY TIME IT EXITS** — including the ~30min `[F9-QUIET]` heartbeat exit — or your "
        "hearing silently stops with nothing to announce it. Its cursor persists per worker+bus, so a re-arm "
        "RESUMES rather than re-seeding to the tip (re-seeding would silently drop whatever arrived in the "
        "gap — the same deafness, harder to see).",
        "   - **Then tell your manager your EXACT hearing state** — never a vague 'hearing degraded', which "
        "reads as 'mostly fine' and is exactly how a worker sits silent while looking healthy. Say which mode "
        "is live, what the latency is, and what would make it stop.",
        "",
        "## Announce your arrival — every worker, every first wake:",
        "Post one line to your Team Room (`--to @all`) — who you are, your lane, ready. Both the terminal and "
        "the Team Room stay live channels; this just lets the cohort and the human user see you're online: "
        f"`{_py_cmd} $TEAM_SCRIPTS_ROOT/cohort_post_cli.py --sender $SYMPHONY_WORKER --to @all --body-file <hello-file>`.",
        "",
        f"## Your cohort tools live at TEAM_SCRIPTS_ROOT = `{team_home}/setup`:",
        "   - `cohort_id_load_confirm.py $SYMPHONY_WORKER`   — L3 identity gate (step 4 above — run AFTER reading, every wake)",
        "   - `cohort_post_cli.py --sender $SYMPHONY_WORKER --to <@peer|@all> --body-file <f>`   — post to the cohort",
        "   - the notification poller — armed via the harness Monitor tool (see HEARING above), NOT a shell command to run directly",
        "   - `cohort_f9_wait.py $SYMPHONY_WORKER $COHORT_BASE`   — exit-on-notification waiter for the "
        "Monitor-absent mode (HEARING 2c). Run as a BACKGROUND task, never in the foreground, and re-arm on every exit.",
        "   READING the bus (posts / notifications): notifications arrive via your F9 poller "
        "(above); for full post bodies use the server endpoint `/api/team_room`. NEVER hand-write "
        "SQL against bus.db.",
        "",
        "Do not lean on the conversation summary as a substitute for the protocol. "
        "The summary fires automatically; the protocol does not.",
        "",
        f"_Generated by install_symphony.py (born-cohort path) — cohort **{cohort}**. WORKER-AGNOSTIC: "
        "resolves the waking worker's own identity live from $SYMPHONY_WORKER, never a baked name. "
        "Re-generate, never hand-edit - single-source discipline._",
    ]
    out = team_home / "CLAUDE.md"
    if not out.exists():
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _seed_soul_dev(p, worker):
    """When installing into a DEV soul namespace (SYMPHONY_SOUL_ROOT != 'soul'), seed it from the
    LIVE soul/ tree BEFORE the pull — copy the worker's wake-set live→dev + regen the dev _index,
    so the pilot hydrates AND later mirrors entirely off soul_dev/, never the live tree (Sage's
    dev-namespace, M1a Fabric-isolation). No-op for production (soul_root=='soul'). The seed script
    carries its OWN hard safety guard (refuses to write the live root). sys.exit on failure."""
    if p.get("soul_root", "soul") == "soul":
        return
    seeder = HERE / "symphony_soul_seed.py"
    if not seeder.is_file():
        sys.exit(f"FATAL: {seeder} missing (required to seed a dev soul namespace)")
    print(f"  → seeding dev soul-store {p['soul_root']}/ from live soul/ (worker {worker}) ...")
    r = subprocess.run([sys.executable, str(seeder), "--worker", worker])
    if r.returncode != 0:
        sys.exit(f"FATAL: dev soul-store seed failed (rc={r.returncode}); NOT proceeding to pull "
                 "(the dev namespace isn't populated). Live soul/ untouched.")
    print(f"  ✓ dev soul-store {p['soul_root']}/ seeded (live soul/ read-only)")


def _git_init(install_base):
    """git init at the SYMPHONY_UNIFIED root (spec A2) → history + rollback on every install/update:
    the integrity/rollback layer the soul-store lacked (bad body/env writes are reversible; both-homes
    diffs are checkable against history). Best-effort: a box without git still installs (warn, not
    abort) — the install artifact does not DEPEND on git, it's a safety net over it. Idempotent
    (skips if already a repo). venv/data/runtime are churny + reconstructable, so they're ignored."""
    try:
        if subprocess.run(["git", "-C", str(install_base), "rev-parse", "--git-dir"],
                          capture_output=True).returncode == 0:
            print(f"  ✓ git already initialized at {install_base}")
            return
        install_base.mkdir(parents=True, exist_ok=True)
        # V1 #3 (Coby's design call): install_base = Symphony/ (--install-root from the .command),
        # consolidated one-folder layout → venv is TOP-LEVEL (Symphony/venv) + body top-level
        # (Symphony/body), so the old subdir-globs (*/venv, */body/data) miss them and git would
        # track the 100s-MB venv. Patterns updated to the consolidated layout.
        (install_base / ".gitignore").write_text(
            "# churny / reconstructable - not tracked\nvenv/\n*/data/\n*/runtime/\nbody/data/\n",
            encoding="utf-8")
        for cmd in (["git", "-C", str(install_base), "init", "-q"],
                    ["git", "-C", str(install_base), "add", "-A"],
                    ["git", "-C", str(install_base), "commit", "-q", "-m",
                     "Symphony install baseline", "--no-gpg-sign"]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0 and "init" in cmd:
                print(f"  ! git init skipped (git unavailable?): {r.stderr.strip()[:80]}")
                return
        print(f"  ✓ git initialized + baseline commit at {install_base} (rollback layer, spec A2)")
    except (OSError, FileNotFoundError) as e:
        print(f"  ! git init skipped ({type(e).__name__}: {e}) — install proceeds (git is a "
              "safety net, not a dependency)")


def _preflight(p, fabric_pending=False, materialize_only=False):
    errs = []
    if not materialize_only and not p["body_source"]:
        errs.append("body source library not found (Symphony - Documents/team) - set SYMPHONY_BODY_SOURCE")
    elif not materialize_only and not (p["body_source"] / "team").is_dir():
        errs.append(f"body source has no team/: {p['body_source']}")
    # In fabric mode the identity home is hydrated at apply (symphony_soul_pull), so a dry-run
    # (pre-hydrate) legitimately has no aria_sync yet - note it instead of erroring. Under --materialize-only,
    # server_lean SCAFFOLDS aria_sync/ FRESH at boot (init_bus CREATE-TABLE + team_room/projects mkdir — Theo
    # verified), so a missing aria_sync/ is not an error either (create-if-missing; no live 495-hydrate needed).
    if (fabric_pending or materialize_only) and not (p["workspace_root"] / "aria_sync").is_dir():
        pass
    elif not (p["workspace_root"] / "aria_sync").is_dir():
        errs.append(f"workspace root has no aria_sync/: {p['workspace_root']}")
    if sys.version_info < (3, 9):
        errs.append(f"python {sys.version_info.major}.{sys.version_info.minor} < 3.9")
    # Manifest-vs-source completeness: every body file MUST exist in the source the
    # installer copies FROM, or the installed body won't boot (e.g. an ungated
    # `import team_paths`). Report up-front, don't crash mid-copy. (Caught a real gap:
    # symphony_bus/ + team_paths.py live in local ~/team but were never synced to the
    # Symphony - Documents body source - AB end-to-end install verify, 2026-06-21.)
    if p["body_source"]:
        src_team = p["body_source"] / "team"
        missing = [rel for rel in p["files"] if not (src_team / rel).is_file()]
        if missing:
            errs.append(f"body source missing {len(missing)} manifest file(s) "
                        f"(installed body would not boot): {', '.join(missing[:10])}"
                        + (" …" if len(missing) > 10 else ""))
    return errs


def _env_block(p):
    # A deployed box runs the event-log BODY-LOCAL (TB sequencing call 2026-06-21,
    # option (b)). We intentionally DO NOT set SYMPHONY_EL_ROOT: bus.py:190 then falls
    # to its body-local default - team_paths.data("symphony_event_log") under TEAM_HOME -
    # which is bus.py's documented "local mode = today's behavior" (no SP dual-write).
    # Rationale: the live shared event-log is append-only, so a throwaway test box writing
    # its wake/heartbeat/L3 events into it would leave PERMANENT orphan entries in the real
    # cohort record. The step-4 proof (identity-stack loads + wakes on a fresh box) is
    # EL-independent, so it proves clean on the box's own local EL with zero pollution.
    # Cross-box live JOIN is (c)-later (needs the heartbeat/attribution N-hosts-per-worker
    # redesign). SYMPHONY_COMPACT_DIR is likewise omitted: it is only required by the A.3
    # coupling guard when SYMPHONY_EL_ROOT is SET (SP mode); in local mode the transient
    # surfaces default under the body-local EL root, same-filesystem, EXDEV-safe.
    # (Supersedes the earlier orphan-path bug: EL_ROOT had pointed at
    # <workspace>/aria_sync/event_log - a third path matching neither the live shared root
    # nor clean body-local. AB fix, TB pp_19cd6f1403.)
    lines = [
        f'TEAM_WORKSPACE_ROOT="{p["workspace_root"]}"',
        f'TEAM_HOME="{p["team_home"]}"',
        # SYMPHONY_INSTALL_ROOT = the TO's own install-root = TEAM_HOME's parent (TEAM_HOME=<install-root>/body).
        # The item-3 self-maintenance reverse-guard (cb_spaceship_guard, 2026-07-17) anchors its born→live
        # WRITE-fence here: the TO may tailor anything under its own install-root (body/ workspace/ config/
        # _shared/), NEVER outside (live :8675/:8677/~/team/other-cohort). Explicit env > derived — the guard
        # falls back to dirname(realpath(TEAM_HOME)) if this is ever unset (Atlas GREEN-unconditional either way).
        f'SYMPHONY_INSTALL_ROOT="{Path(p["team_home"]).parent}"',
        # Cohort scripts (cohort_f9_poller / cohort_id_load_confirm / cohort_post) live at
        # TEAM_HOME/setup. Set explicitly so the hooks (poller_autostart, paths.SCRIPTS_ROOT)
        # resolve them off the TARGET path, not a hardcoded origin ~/team/setup — the keystone
        # of M1-core cross-machine machine-independence (Abe 2026-07-11). Forward-slash is
        # Path-normalizable on Windows; every reader wraps this in Path().
        f'TEAM_SCRIPTS_ROOT="{p["team_home"]}/setup"',
        f'SYMPHONY_BODY_SOURCE="{p["body_source"]}"',
        # Body-local bus/data under the cohort root (isolation axis 3): paths.py reads TEAM_DATA_DIR
        # (else HERE/data) — set it explicitly so bus.db lives at <install-root>/<cohort>/data,
        # NEVER the live ~/team/bus.db. Quinn's artifact-side fence-check keys on this.
        f'TEAM_DATA_DIR="{p["data_dir"]}"',
        # Per-instance server lockfile (isolation): server.py:9012 reads TEAM_PID_FILE (default
        # /tmp/team_server.pid — SHARED, so a sandbox spaceship would refuse to start alongside the
        # live one). Key it under the cohort data dir → the sandbox :8676 server binds side-by-side
        # with live :8675, each guarding its own lockfile (Theo verified isolation holds). Same
        # env-set-must-match-env-read keystone class as TEAM_PORT/TEAM_SCRIPTS_ROOT.
        f'TEAM_PID_FILE="{p["data_dir"]}/team_server.pid"',
        # Soul-store root — dev pilots resolve BOTH hydrate + mirror off soul_dev/, never live soul/
        # (Sage's dev-namespace; symphony_soul_pull._rooted + the mirror hooks read this).
        f'SYMPHONY_SOUL_ROOT="{p.get("soul_root", "soul")}"',
        # Spaceship/comms port — dev on 8676, side-by-side with the untouched live dev :8675.
        # TEAM_PORT is the OPERATIVE var the spaceship actually binds (server.py:64 reads
        # os.environ["TEAM_PORT"], default 8675) — so THIS is what enforces isolation-axis-2;
        # without it a copied server would re-bind 8675 and collide. SYMPHONY_PORT is kept as the
        # spec-named alias (A3) for legibility + any reader that keys on it. (Same keystone class
        # as TEAM_SCRIPTS_ROOT: the env we SET must be the env the body READS — Abe 2026-07-12.)
        f'TEAM_PORT="{p.get("port", 8675)}"',
        f'SYMPHONY_PORT="{p.get("port", 8675)}"',
        # Comms-client base URL: the F9 poller (cohort_f9_poller.py) reads COHORT_BASE (default
        # http://localhost:8675 = LIVE). Unset, a sandbox worker's poller watches the LIVE bus, not
        # its own — the "monitor not armed against the sandbox" gap (body#1 canary). Point it at THIS
        # install's port so the poller watches the sandbox bus. (cohort_post.py still hardcodes 8675
        # in DEFAULT_BASE — that client-side code fix to also read COHORT_BASE is Theo's lane.)
        f'COHORT_BASE="http://localhost:{p.get("port", 8675)}"',
    ]
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    ap.add_argument("--force", action="store_true", help="allow overwriting a non-empty TEAM_HOME")
    ap.add_argument("--materialize-only", action="store_true", help="SELF-CONTAINED-WRAPPER seam: the "
                    "install_symphony_selfcontained.sh wrapper already OWNS body + venv (Fabric-pull + "
                    "wheelhouse-assemble + body-bundle extract into a pre-populated TEAM_HOME). Skip body-copy "
                    "+ venv-provision; do ONLY materialize (config.roles+members.json) + hooks + env + "
                    "resolver-copy into the pre-populated body. (Abe/Coby seam 2026-07-14.)")
    ap.add_argument("--install-root", help="Symphony install base (default: "
                    "'~/Library/Application Support/Symphony' on Mac, "
                    "'%%USERPROFILE%%\\Symphony' on Windows). A cohort installs self-contained "
                    "under <install-root>/<cohort>/ (body/workspace/data/venv/runtime) — separate "
                    "from ARIA and from the live ~/team, so a dev install can't collide.")
    ap.add_argument("--cohort", help="cohort subdir under --install-root (default: the worker's "
                    "cohort from cohort_registry). A dev pilot can use its own name.")
    ap.add_argument("--soul-root", help="Fabric soul-store root (default 'soul'=production; "
                    "'soul_dev' for a Mac-first dev pilot — reads AND writes under soul_dev/, never "
                    "live soul/, Sage's dev-namespace isolation). Sets SYMPHONY_SOUL_ROOT.")
    ap.add_argument("--port", type=int, help="spaceship/comms port (default 8675; use 8676 for a "
                    "dev install side-by-side with the untouched live dev spaceship). Sets SYMPHONY_PORT.")
    ap.add_argument("--team-home", help="override TEAM_HOME (default <install-root>/<cohort>/body)")
    ap.add_argument("--runtime", help="override runtime dir (default <install-root>/<cohort>/runtime)")
    ap.add_argument("--use-existing-venv", help="REUSE an existing venv in-place (e.g. the box's "
                    "already-installed ARIA venv - correctly-pathed for this box) instead of "
                    "provisioning. The cleanest offline path (no bundle, no pip). Must carry "
                    "Symphony's deps (the aria_unified ecosystem - ARIA's venv covers them).")
    ap.add_argument("--copy-venv", help="COPY-AND-OWN (George's §A5 default): copy an existing venv "
                    "(e.g. the box's ARIA venv) INTO cohort_root/venv so Symphony fully owns its own "
                    "venv, separate from ARIA. Same-box only (a copied venv keeps donor abs-paths, "
                    "correct on THIS box; Mac-first eyes-on is exactly where cross-box path drift is "
                    "caught). Offline (no pip); the 2 bundled deps inject into the COPY.")
    ap.add_argument("--venv-bundle", help="path to a complete prebuilt venv zip (unzip-not-pip; "
                    "the offline dep model). If omitted, falls back to venv+pip (needs PyPI).")
    ap.add_argument("--worker", help="the box's cohort worker (e.g. aria_builder); seeds the "
                    "cohort-keyed CLAUDE.md wake entry-point generated from cohort_registry.")
    ap.add_argument("--identity-source", choices=("onedrive", "fabric", "archetype"), default="onedrive",
                    help="where the cohort IDENTITY (soul: SOUL/HEART/PRINCIPLES/deltas + worker "
                    "state) comes from. 'onedrive' (default, M1-core) = the synced library. "
                    "'fabric' (M1-Fabric) = pull it from the symphony_soul Lakehouse into a LOCAL "
                    "workspace via symphony_soul_pull.py — for a bare box with NO OneDrive (the "
                    "'anyone's computer' path). 'archetype' (M1a', 2026-07-13) = FRESH-instantiate a "
                    "cohort from ARCHETYPES: pull the shared core (fabric) + hydrate the archetype "
                    "library, then MATERIALIZE each worker's lean role-binding {archetype-pointer + "
                    "resolved-delta} from a roster (--roles-file) — no predecessor to copy. Requires "
                    "--worker + --roles-file + --archetype-dir; writes config.roles + <worker>_role.md.")
    ap.add_argument("--roles-file", help="[--identity-source archetype] JSON roster mapping worker→"
                    "{archetype, delta} for the cohort, e.g. {\"quinn\":{\"archetype\":\"coordinator\","
                    "\"delta\":{\"object\":\"the ir_cohort\",...}}, ...}. Materialize reads it to write "
                    "config.roles + each worker's role-binding; the DELTA carries sandbox source/reference "
                    "pointers (P-ir.10 — never the live corpus), which materialize writes verbatim.")
    ap.add_argument("--archetype-dir", help="[--identity-source archetype] dir of gate-passed archetype "
                    "files (<archetype>.md, Sage's 4-section format). Hydrated into the sandbox "
                    "workspace_root/cohort_substrate/_shared/archetypes/ (soul_root-clamped, read live at "
                    "wake). Default: the shared archetypes under the pulled soul-store.")
    ap.add_argument("--identity-dir", help="[--materialize-only fix-B] local staging dir holding the born "
                    "body's L3-required IDENTITY: `_shared/identity/` (SOUL/HEART/PRINCIPLES/MEDIUM/CHANGELOG "
                    "universal core) + `<cohort>/identity/` (cohort deltas). Hydrated into "
                    "workspace_root/cohort_substrate/_shared/identity/ + /<cohort>/identity/ (siblings of the "
                    "archetype-library, the L3-read tree). Replaces the gated _pull_identity_from_fabric under "
                    "--materialize-only — the wrapper pulls these popup-free; no live 495-hydrate. (Abe/Coby/Sage/Mira/Quinn 07-14.)")
    ap.add_argument("--fabric-workspace", help="LOCAL workspace root to hydrate identity into "
                    "when --identity-source fabric (becomes TEAM_WORKSPACE_ROOT). "
                    "Default: ~/symphony_workspace.")
    ap.add_argument("--verify-scope", choices=("full", "wake"), default="full",
                    help="venv dep-verification scope. 'full' (default) = all runtime deps must "
                    "import or ABORT (for 'runs flawlessly' incl. the bus/comms server). 'wake' = "
                    "the M1 cross-machine WAKE milestone: the L3 identity load is pure stdlib, so a "
                    "missing SERVER dep (fastapi/markupsafe live only in ARIA's neural tier) is a "
                    "loud WARNING, not an abort - lets you leverage the base ARIA venv to prove the "
                    "wake, with the bus server deferred until those deps are supplied.")
    args = ap.parse_args()
    if args.identity_source in ("fabric", "archetype") and not args.worker and not args.materialize_only:
        sys.exit(f"--identity-source {args.identity_source} requires --worker (the kit/roster is worker-keyed).")
    # --materialize-only: --worker is OPTIONAL (Quinn pp_, 7→7000): the body is worker-AGNOSTIC — symphony_identity.json
    # carries the roles-MAP (no fixed worker), the resolver reads worker from $SYMPHONY_WORKER + IGNORES any config-worker.
    # The wrapper still passes a seed-worker (safe; seeds the entry-point) but it's not a hard requirement here.
    if args.identity_source == "archetype" and not args.roles_file:
        sys.exit("--identity-source archetype requires --roles-file (worker→{archetype,delta} roster JSON).")
    # WRITE-SIDE never-live guard (Coby's suspenders-for-scale pp_, mirrors the resolver's read-side clamp):
    # a born ARCHETYPE body must NEVER install onto the LIVE soul-tree — soul_root='soul' would (a) SKIP the
    # config.roles write (L1152 gate) → roleless body, AND (b) breach isolation (Mira never-live). If a future
    # 7→7000 wrapper forgets --soul-root, force the SANDBOX (soul_dev) loudly rather than silently ship broken.
    if args.identity_source == "archetype" and (not getattr(args, "soul_root", None)
                                                or str(args.soul_root).strip() in ("", "soul")):
        args.soul_root = "soul_dev"
        print("  ⚠ archetype install + soul_root unset/'soul' → FORCED to 'soul_dev' (SANDBOX). A born "
              "archetype-body never installs onto the LIVE soul-tree (roleless + isolation breach). "
              "Pass --soul-root explicitly to silence this guard.")

    p = plan(args)
    src_team = (p["body_source"] / "team") if p["body_source"] else None

    print("=" * 64)
    print("Symphony body installer - " + ("APPLY" if args.apply else "DRY-RUN (no writes)"))
    print("=" * 64)
    print(f"  identity source: {args.identity_source.upper()}"
          + ("  (M1-Fabric: hydrate soul from Lakehouse - no OneDrive needed)"
             if args.identity_source == "fabric" else "  (synced library)"))
    print(f"  workspace root : {p['workspace_root']}")
    print(f"  body source    : {src_team}")
    print(f"  TEAM_HOME      : {p['team_home']}")
    print(f"  runtime        : {p['runtime']}")
    print(f"  venv           : {p['venv_dir']}  (recreate-not-copy)")
    print(f"  body files     : {len(p['files'])} (from manifest)")
    print(f"  env to write   :")
    for ln in _env_block(p):
        print(f"      export {ln}")

    # M1-Fabric: on APPLY, hydrate identity from the soul Lakehouse into the local workspace
    # BEFORE preflight (so the aria_sync/ presence check sees the just-hydrated home). On a
    # dry-run the hydrate is deferred (writes nothing) and preflight tolerates the absence.
    # Both 'fabric' and 'archetype' hydrate the SHARED core from the Lakehouse; 'archetype' THEN
    # materializes each worker's role-binding FROM archetypes (M1a' fresh-instantiation, no predecessor).
    _pull_mode = args.identity_source in ("fabric", "archetype")
    fabric_pending = _pull_mode and not args.apply
    archetype_roles = None                          # {worker: archetype} from _apply_archetype_mode (→ 5d config)
    archetype_degraded = None                        # GAP-2 (Coby): {worker:{field:reason}} → config.degraded_refs
    archetype_operator = None                        # 5b (Coby): roster['operator'] → config.manager.id
    if _pull_mode and args.apply:
        # Propagate the soul-store root so the seed + pull subprocesses both read SYMPHONY_SOUL_ROOT
        # (they resolve reads/writes off it — a dev pilot stays entirely on soul_dev/, never live).
        os.environ["SYMPHONY_SOUL_ROOT"] = p["soul_root"]
        # SEAM (Abe/Coby/Mira/Sage/Quinn/Atlas/Theo 2026-07-14): both FULL-soul-hydrate Fabric ops GATED under
        # --materialize-only. The born body's L3-real identity-need = #1 _shared/identity + #2 <cohort>/identity
        # (Mira/Quinn authoritative: L3 reads cohort_substrate/, NOT aria_sync/) — provided by the WRAPPER's fix-B
        # pull of #1+#2 into cohort_substrate/ (Sage staged them on soul_dev, popup-free). The full 495-hydrate is
        # NOT un-gated because it also brings a LIVE-seeded aria_sync/ → born body would serve LIVE comms = data-fence
        # breach (Theo/Mira). aria_sync/ is provided as a FRESH born-hub scaffold instead; the preflight's aria_sync
        # check is relaxed under --materialize-only (it's a full-ARIA-installer assumption, not a born-Symphony need).
        if not args.materialize_only:
            _seed_soul_dev(p, args.worker)              # seed dev namespace from live (no-op if 'soul')
            _pull_identity_from_fabric(p, args.worker)  # then hydrate the SHARED core FROM the seeded dev namespace
        if args.identity_source == "archetype":
            # materialize the cohort's role-bindings from archetypes (writes <worker>_role.md per the
            # roster + hydrates _shared/archetypes/); returns the {worker: archetype} map for config.roles.
            archetype_roles, archetype_degraded, archetype_operator = _apply_archetype_mode(p, args)   # GAP-2: +degraded_map · +operator (5b)

    errs = _preflight(p, fabric_pending=fabric_pending, materialize_only=args.materialize_only)
    if errs:
        print("\nPREFLIGHT ERRORS:")
        for e in errs:
            print("  ✗ " + e)
        sys.exit(2)
    print("\nPreflight: OK")

    # CLAUDE.md shadowing precondition (Quinn's catch) - warn if a higher/equal-precedence
    # CLAUDE.md would shadow the seeded TEAM_HOME/CLAUDE.md. Not a hard error (a fresh box
    # has none); flag so step-4's fresh-box check covers CLAUDE.md, not just MEMORY.md.
    if args.worker:
        competing = _competing_claude_md(p["team_home"])
        if competing:
            print("  ⚠ competing CLAUDE.md (could shadow the seed - confirm fresh box):")
            for c in competing:
                print(f"      {c}")
        else:
            print("  ✓ no competing CLAUDE.md (cwd-tree ancestors + ~/.claude clean)")
        # partial-sync identity-dir presence (Quinn's IR catch) - homes_for-derived, any worker
        missing_id = _missing_identity_dirs(args.worker)
        if missing_id and fabric_pending:
            print(f"  → {args.worker} identity dir(s) not yet present — will be hydrated from "
                  f"Fabric (symphony_soul) at --apply:")
            for d in missing_id:
                print(f"      {d}")
        elif missing_id:
            print(f"  ⚠ {args.worker}'s cohort identity dir(s) ABSENT (partial-sync - worker "
                  f"would wake without its full stack; confirm cohort_substrate synced):")
            for d in missing_id:
                print(f"      {d}")
        else:
            print(f"  ✓ {args.worker} cohort identity dirs present under workspace_root "
                  "(no partial-sync gap)")

    if not args.apply:
        fab = ("hydrate identity from Fabric (symphony_soul) → workspace · "
               if args.identity_source == "fabric" else "")
        print(f"\n[dry-run] would: {fab}copy body → TEAM_HOME · create/reuse venv · "
              "mkdir runtime · write env · print launch steps. Re-run with --apply to execute.")
        print_launch(p)
        return

    # --- APPLY ---
    th = p["team_home"]
    if args.materialize_only:
        # SELF-CONTAINED WRAPPER seam (Abe/Coby 2026-07-14): install_symphony_selfcontained.sh already OWNS
        # body + venv — it Fabric-pulls + wheelhouse-assembles the venv and extracts the LEAN body-bundle into
        # TEAM_HOME. So SKIP body-copy + venv-provision + dep-inject here; the KEEP-blocks below (verify · runtime
        # dirs · env · hooks · _seed_body_config config.roles+members.json · resolver-copy · git) run unconditionally,
        # plus materialize (_apply_archetype_mode) already ran above. Require the wrapper actually populated it.
        if not (th.exists() and any(th.iterdir())):
            sys.exit(f"--materialize-only requires a pre-populated body at {th} "
                     "(the self-contained wrapper extracts body-bundle.zip into TEAM_HOME first).")
        print("  ✓ --materialize-only: wrapper owns body + venv — skipping body-copy + venv-provision + inject")
    else:
        if th.exists() and any(th.iterdir()) and not args.force:
            sys.exit(f"REFUSING: {th} exists and is non-empty (use --force to overwrite). "
                     "Guard against clobbering a live body.")
        # 1. copy manifest body
        for rel in p["files"]:
            dst = th / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_team / rel, dst)
        print(f"  ✓ copied {len(p['files'])} body files → {th}")
        # 2. venv - reuse existing in-place (offline), unzip a prebuilt bundle, or pip-fallback
        if args.use_existing_venv:
            # In-place reuse of an already-installed venv (e.g. ARIA's on this box). NOT copied
            # (a copied venv carries donor abs-paths); the box's own venv is correctly pathed.
            # _verify_venv below confirms it actually carries Symphony's runtime deps.
            if not p["venv_dir"].exists() or not _venv_python(p["venv_dir"]).exists():
                sys.exit(f"REFUSING: --use-existing-venv {p['venv_dir']} is not a usable venv "
                         "(missing dir or python) - point it at the box's ARIA venv.")
            print(f"  ✓ reusing existing venv in-place: {p['venv_dir']} (offline, no provision)")
        elif args.copy_venv:
            # Copy-and-own (§A5): clone the box's ARIA venv into cohort_root/venv so Symphony owns a
            # venv fully separate from ARIA. Same-box only — a copied venv keeps the donor's abs-paths
            # (pyvenv.cfg home + bin shebangs), correct on THIS box, which is why George put it on the
            # Mac-first eyes-on rung where any cross-box path drift shows up immediately. symlinks=True
            # preserves bin/python → base interpreter (resolves on the same box).
            src_venv = Path(args.copy_venv).expanduser()
            if not src_venv.exists() or not _venv_python(src_venv).exists():
                sys.exit(f"REFUSING: --copy-venv {src_venv} is not a usable venv (missing dir or "
                         "python) — point it at the box's ARIA venv.")
            if src_venv.resolve() == p["venv_dir"].resolve():
                sys.exit(f"REFUSING: --copy-venv source == destination ({p['venv_dir']}); that is "
                         "reuse-in-place — use --use-existing-venv for that.")
            if p["venv_dir"].exists():
                shutil.rmtree(p["venv_dir"])            # recreate-not-merge (same as bundle path)
            p["venv_dir"].parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_venv, p["venv_dir"], symlinks=True)
            print(f"  ✓ copied venv (copy-and-own): {src_venv} → {p['venv_dir']} (offline, same-box)")
        elif args.venv_bundle:
            _unzip_venv_bundle(Path(args.venv_bundle).expanduser(), p["venv_dir"])
            print(f"  ✓ venv unzipped from bundle (offline, no admin)")
        else:
            print("  ! no --venv-bundle: falling back to venv+pip (REQUIRES PyPI - "
                  "will fail on a Lilly box; supply a bundle for offline install)")
            subprocess.check_call([sys.executable, "-m", "venv", str(p["venv_dir"])])
            subprocess.check_call([str(_venv_python(p["venv_dir"])), "-m", "pip", "install",
                                   "-q", "-r", str(REQUIREMENTS)])
        # inject Symphony's bundled offline deps (fastapi + markupsafe) the ARIA base venv lacks but
        # the comms server needs — copied straight into site-packages, no pip (Artifactory-blocked).
        # This folds REACH into the install (George: the spaceship is part of Symphony), so full-scope
        # verify then passes on the ARIA venv without a neural-tier or a pip.
        injected = _inject_venv_deps(p["venv_dir"])
        if injected:
            print(f"  ✓ injected {len(injected)} bundled dep(s) into venv site-packages "
                  f"(offline, no pip): {', '.join(injected)}")
        elif DEPS_BUNDLE.is_dir():
            print("  ✓ bundled deps already present in venv (no injection needed)")

    # verify the body's runtime deps import in the provisioned venv. Scope-aware: 'full' aborts
    # on any missing dep (runs-flawlessly bar); 'wake' downgrades a miss to a loud warning (the
    # L3 identity-load wake = stdlib, so leveraging the base ARIA venv - which lacks the neural-
    # tier-only fastapi/markupsafe - still proves the cross-machine wake; the bus server defers).
    # --materialize-only: the wrapper OWNS + already assembled the boot venv (its own import-smoke at
    # assemble-time), and invoked us WITH that venv's python — so p["venv_dir"] (SYMPHONY_UNIFIED/<cohort>/venv)
    # is deliberately NOT provisioned. Verify the venv the engine is ACTUALLY running under (== the wrapper's
    # boot venv, sys.executable's root) — a REAL check of the deps that boot server_lean, not the empty
    # unprovisioned venv_dir (which would false-FAIL every dep). (07-14 real-run caught.)
    # NB: do NOT .resolve() — venv/bin/python3 is a SYMLINK to the base interpreter; resolving it lands on
    # venv-src/python-base (dep-less) and false-FAILs. sys.executable is already the venv's own python PATH.
    _verify_venv_dir = (Path(sys.executable).parent.parent
                        if getattr(args, "materialize_only", False) else p["venv_dir"])
    missing = _verify_venv(_verify_venv_dir)
    # NON-FATAL for --verify-scope wake AND --materialize-only (Coby fix 2026-07-17): materialize
    # writes config.roles + identity/ = STDLIB; server-deps (fastapi/uvicorn) are a SERVER-BOOT concern
    # that boots SEPARATELY on the born venv. Previously a false-fail here (materialize_only verifies
    # sys.executable = the invoking python, which is the SYSTEM python when the TO invokes it that way,
    # deps-less) sys.exit'd BEFORE _seed_body_config wrote config.roles → aborted the compose, leaving
    # specialists out of config.roles → wakes failed (George 07-17 morgan/riley/casey). The materialize
    # must NEVER be abortable by a server-dep check.
    _mat_only = getattr(args, "materialize_only", False)
    if missing and _mat_only:
        # BENIGN for materialize_only (Coby (ii)-UX, TB-assigned — George end-user gate: a new user must
        # NOT see a technical "deps missing/FAILED" scare during compose). Non-fatal (Coby (i)) AND
        # non-scary: the materialize writes config.roles/identity = STDLIB; the bus/comms server verifies
        # its OWN runtime deps SEPARATELY at launch on the born venv. No dep-list, no "missing/FAILED".
        print("  · materialize scope: server runtime-dep check deferred to launch (the born server "
              "verifies its own deps); config.roles/identity write is stdlib-only.")
    elif missing and args.verify_scope == "wake":
        print(f"  ⚠ venv missing {len(missing)} server/reach dep(s): {', '.join(missing)} — "
              "ACCEPTED for --verify-scope wake (L3 wake = stdlib; the bus/comms SERVER will NOT "
              "run until these are supplied — add ARIA's neural tier or pip these two).")
    elif missing:
        sys.exit(f"VENV VERIFY FAILED - missing imports: {', '.join(missing)} "
                 "(body's bus server would not boot). Supply the deps, or use --verify-scope wake "
                 "to prove the cross-machine WAKE now (identity load is stdlib) and defer the server.")
    else:
        print(f"  ✓ venv verified: all {len(_REQUIRED_IMPORTS)} runtime deps import")
    # 3. runtime dirs
    for d in (p["runtime"], p["runtime"] / "compact", th / "data"):
        d.mkdir(parents=True, exist_ok=True)
    print(f"  ✓ runtime dirs created")
    # 4. env file
    # env files - both shells, since the body runs on Mac (bash) and Windows (PowerShell);
    # a fresh Windows box can't source a .sh, so emit a .ps1 too. Each line is KEY="val".
    # SYMPHONY_EL_ROOT landmine guard (Tia audit catch 2026-07-23, traced by Abe pp_dd519a7f8e):
    # bus.py is the SAME shared file backing the live :8675/:8677 dev servers' event-log dual-write -
    # a Symphony install deliberately never SETS this var (see install_symphony.py's _env_block comment
    # above re: avoiding orphan writes into the live shared event-log), but if it ever LEAKS IN from a
    # parent shell's exported env, bus.py's fail-closed coupling guard raises RuntimeError at import
    # (server refuses to boot) since symphony_bus/ genuinely isn't shipped in the Symphony body. Explicit
    # unset at env-write time is belt-and-suspenders against exactly that leak - not a bus.py edit (which
    # would touch shared live infrastructure), just closing the one gap in Symphony's own env file.
    sh_path = th / "symphony_env.sh"
    sh_path.write_text("# Symphony machine env (source before launch: . symphony_env.sh)\n" +
                       "\n".join("export " + ln for ln in _env_block(p)) + "\n" +
                       "unset SYMPHONY_EL_ROOT\n", encoding="utf-8")
    ps_path = th / "symphony_env.ps1"
    ps_path.write_text("# Symphony machine env (dot-source before launch: . .\\symphony_env.ps1)\n" +
                       "\n".join('$env:' + ln.split('=', 1)[0] + '=' + ln.split('=', 1)[1]
                                 for ln in _env_block(p)) + "\n" +
                       "Remove-Item Env:\\SYMPHONY_EL_ROOT -ErrorAction SilentlyContinue\n", encoding="utf-8")
    print(f"  ✓ env written: {sh_path.name} + {ps_path.name}")
    # 5. cohort-keyed wake entry-point (generated from cohort_registry; defense-in-depth)
    if args.worker and getattr(args, "materialize_only", False):
        # --materialize-only (BORN cohort): the cohort_registry-based CLAUDE.md seed is SKIPPED. That
        # registry is ARIA-side + LIVE-COUPLED (cohort_registry → `from paths import WORKSPACE_ROOT`,
        # import-cached to the live tree — the exact isolation smell the data-fence forbids) AND it only
        # knows ARIA's own cohorts, not an arbitrary born cohort (7→7000). A born body resolves its
        # wake-entrypoint from the hydrated cohort_substrate/L3 + materialized config.roles + the body's
        # own resolver (5e) — NOT ARIA's homes_for. The server boot (wrapper step-8) does NOT need this;
        # the born-WORKER wake-entrypoint (born-native CLAUDE.md + recovery.md + wake script) is the
        # born-wake lane (Theo/Mira). Degraded-but-correct + fence-safe. (07-14 real-run caught.)
        print("  ! --materialize-only: cohort_registry CLAUDE.md seed SKIPPED (ARIA-side + live-coupled; "
              "a born cohort resolves its wake-entrypoint from hydrated cohort_substrate/L3 + config.roles + "
              "the body resolver, not ARIA's homes_for). Born-worker wake-entrypoint = born-wake lane.")
        seed = None
    elif args.worker:
        seed = _seed_identity(th, args.worker, archetype_mode=(args.identity_source == "archetype"))
        print(f"  ✓ identity seed written (WORKER-AGNOSTIC; worker resolves from $SYMPHONY_WORKER at wake): {seed}")
    else:
        print("  ! no --worker: identity-seed skipped (box relies on recovery.md §0 + L3 - "
              "degraded-but-correct; pass --worker to seed the cohort-keyed entry-point)")
    # 5b. autonomous-wake: self-configuring SessionStart hook (self-sources the body env → injects it
    # into the session via CLAUDE_ENV_FILE → auto-arms the F9 poller, bus-aware). Keys off $SYMPHONY_WORKER.
    settings = _seed_body_settings(th)
    print(f"  ✓ self-configuring SessionStart hook written (self-sources env + auto-arms poller): {settings}")
    # 5c. the ROBUST hands-off launcher — puts the FULL env into claude's process so every hook it fires
    # (incl. the box's soul hooks) resolves to the sandbox. The recommended launch path.
    wrapper = _seed_wake_wrapper(th)
    print(f"  ✓ hands-off wake wrapper written (the one-command launcher): {wrapper}")
    # 5d. durable body-local identity config — the env-INDEPENDENT isolation source the soul-hooks'
    # resolver reads (Coby's helper). Only a dev/sandbox install writes it; a live install (soul_root
    # 'soul') writes none → helper finds no config → not-sandbox → live (production backward-compat).
    _sr = p.get("soul_root") or "soul"
    if _sr != "soul":
        # archetype-mode passes the {worker: archetype} roles-map → config.roles + identity_source marker
        # (the per-body role assignment the helper/L3/verify read). Non-archetype modes: roles=None (agnostic).
        bcfg = _seed_body_config(th, args.cohort or p["cohort_sub"], _sr, roles=archetype_roles, degraded=archetype_degraded, operator=archetype_operator)
        print(f"  ✓ body identity config written (durable isolation source): {bcfg}"
              + (f"  [+ roles-map {archetype_roles} + identity_source=archetype]" if archetype_roles else ""))
    # 5e. ship the identity-resolver into the body's setup/ so a BORN archetype-body can IMPORT it at
    # wake (L3 gate resolves identity["archetype"]/paths FROM it; soul-hooks read it too). Same-dir with
    # cohort_id_load_confirm → L3's same-dir import resolves. Archetype-mode only; a missing/failed import
    # fail-CLOSES (Sage/Coby backstop, never fail-open). Staged in dist/ (like symphony_materialize).
    if args.identity_source == "archetype":
        _helper_src = HERE / "resolve_symphony_identity.py"
        _helper_dst = th / "setup" / "resolve_symphony_identity.py"
        # Fix F (Abe 2026-07-16): a conductor compose-add runs `--materialize-only` FROM body/setup/, so HERE ==
        # th/setup → src IS dst → shutil.copy2 raised SameFileError (non-fatal but crashed the tail + scared the
        # user). Guard on resolved-path inequality: copy on a fresh install (HERE=zip tools/ ≠ body/setup), skip
        # cleanly on a self-copy (already in place). Applies to the 5g closure loop below too.
        if _helper_src.is_file() and _helper_src.resolve() != _helper_dst.resolve():
            (th / "setup").mkdir(parents=True, exist_ok=True)
            shutil.copy2(_helper_src, _helper_dst)
            print(f"  ✓ shipped resolve_symphony_identity.py → {th}/setup/ (born-body L3-import + soul-hooks)")
        elif _helper_src.is_file():
            print(f"  ✓ resolve_symphony_identity.py already in place at {th}/setup/ (self-copy skipped — materialize-only from body/setup)")
        else:
            print(f"  ⚠ resolve_symphony_identity.py NOT staged in {HERE} — born-body L3 enforce-path will "
                  "import-fail → FAIL-CLOSED (safe, not enforce-resolved). Stage it in dist/ before a real RUN.")
    # 5g. ship the BORN-WORKER runtime closure into body/setup/ (= TEAM_SCRIPTS_ROOT). The born body
    # shipped the SERVER (server_lean + closure) + resolver (5e), but a born WORKER also needs to WAKE-with-
    # identity, HEAR, POST, and CONFIRM — none of which the server provides (Theo/Sage 07-14, verified on the
    # live born body: body/setup had only resolver+sessionstart → wake ImportError'd on poller_autostart, no
    # post/confirm tooling). Ship the standard worker-comms tooling (7→7000: born workers use the SAME
    # cohort_post_cli/cohort_f9_poller contract as live — one comms surface to fence), the option-B identity
    # injector + its §0 template, and the per-wake READ_EVIDENCE gate (§3-A). All read COHORT_BASE (born-local
    # once the wrapper passes --port), so a born worker's poll/post hits the BORN bus, never live :8675
    # (the HARD write-fence, Sage pp_d0ca76bc31). Archetype-mode only; each best-effort + is_file-guarded so a
    # not-yet-staged piece (e.g. the born cohort_id_load_confirm Theo authors) warns, never crashes the install.
    if args.identity_source == "archetype":
        (th / "setup").mkdir(parents=True, exist_ok=True)
        _born_setup = (
            ("symphony_wake_inject.py",              "born-wake identity injector (option B)"),
            ("symphony_born_wake_protocol.md.tmpl",  "born §0 wake-protocol template (Theo/Mira)"),
            ("poller_autostart.py",                  "poller auto-arm (wake.sh + hook import it → HEAR)"),
            ("cohort_f9_poller.py",                  "F9 notification poller (reads COHORT_BASE = born bus)"),
            ("cohort_registry.py",                   "cohort registry (dep of cohort_post + confirm; imports paths→TEAM_WORKSPACE_ROOT born-local; get_cohort_docs = required_reads source)"),
            ("cohort_post_cli.py",                   "cohort post CLI (born worker POSTs, COHORT_BASE-scoped)"),
            ("cohort_post.py",                       "post lib (cohort_post_cli dep; imports cohort_registry + paths)"),
            ("cohort_id_load_confirm.py",            "per-wake READ_EVIDENCE L3 gate (§3-A, born-local; imports cohort_registry)"),
            # ── Component-2 compose-loop MATERIALIZE-ENGINE closure (Abe+Coby 2026-07-15, born-engine-dir pin) ──
            # The conductor's compose-loop (conductor_runbook.md) shells out
            #   `python3 $TEAM_SCRIPTS_ROOT/install_symphony.py --materialize-only --roles-file <roster> --apply`
            # to add composed workers ON THE BORN BOX → the engine must live here (= HERE = $TEAM_SCRIPTS_ROOT).
            # ⚠️ --apply IS REQUIRED (2026-07-18 ship-blocker, Abe empirical): dry-run is the DEFAULT — the
            # runbook's documented compose command previously omitted --apply, so a born TO following it
            # LITERALLY writes nothing (no config.roles, no members.json, no per-slot memory) — the compose
            # silently no-ops. Fixed here + in conductor_runbook.md (Sage, soul_dev, served not ZIP'd).
            # MINIMAL closure EMPIRICALLY VERIFIED (Abe, isolated `import install_symphony` w/ only these 3 present
            # → clean; both deps are leaf modules): install_symphony + symphony_materialize (L~711 archetype-mode
            # import) + identity_root (MODULE-LEVEL import L43 — FATALs at load without it). cohort_registry/paths
            # are NOT shipped: they live only on the _seed_identity path, which --materialize-only SKIPS (L~1280
            # seed=None). yaml is in the born venv; the archetype library is hydrated to _shared/archetypes/.
            ("install_symphony.py",                  "materialize ENGINE — conductor shells out `$TEAM_SCRIPTS_ROOT/install_symphony.py --materialize-only --roles-file <roster> --apply` to add composed workers (--apply required — dry-run is default)"),
            ("symphony_materialize.py",              "engine dep — materialize_role_identity/MaterializeError, imported from HERE=$TEAM_SCRIPTS_ROOT on the archetype path"),
            ("identity_root.py",                     "engine dep — module-level import (resolve_workspace_root/resolve_body_source); absent → install_symphony FATALs at load"),
        )
        for _fn, _desc in _born_setup:
            _src = HERE / _fn
            _dst = th / "setup" / _fn
            # Fix F (self-copy guard): on a conductor materialize-only from body/setup, HERE == th/setup → src IS
            # dst → skip cleanly (already in place), no SameFileError. Fresh install (HERE=zip tools/) → copy.
            if _src.is_file() and _src.resolve() != _dst.resolve():
                shutil.copy2(_src, _dst)
                print(f"  ✓ shipped {_fn} → {th}/setup/ ({_desc})")
            elif _src.is_file():
                pass  # already in place (materialize-only from body/setup = HERE); no self-copy
            else:
                print(f"  ⚠ {_fn} NOT staged in {HERE} — {_desc} unavailable on the born body "
                      "(stage it before a real RUN, or the born worker can't perform this step).")
        # cure-A(i) (Abe 2026-07-16): OVERWRITE the static ARIA cohort_registry.py 5g just shipped with a
        # BORN-CORRECT one generated from THIS composed roster — kills the aria_canon/ir_cohort DEV residue at
        # the SOURCE (class #7) + S6 arbitrary-name lookups. Belt to the born resolver (which is primary — this
        # is hardening). Runs on fresh install AND conductor materialize-only (idempotent). Non-fatal: on any
        # failure the static registry 5g shipped remains (import-safe, though DEV-named) so confirm/post never FATAL.
        try:
            _reg_cohort = args.cohort or p["cohort_sub"]
            _reg_workers = list(archetype_roles.keys()) if archetype_roles else []
            if _reg_workers:
                _write_born_cohort_registry(th / "setup" / "cohort_registry.py", _reg_cohort, _reg_workers)
                print(f"  ✓ born cohort_registry.py generated (cure-A(i)): cohort={_reg_cohort}, workers={sorted(_reg_workers)}")
            else:
                print("  ! born cohort_registry generation SKIPPED (no archetype_roles) — static registry retained (import-safe).")
        except Exception as _e:
            print(f"  ⚠ born cohort_registry generation failed ({_e}) — static registry retained (import-safe, may carry DEV names).")
    # 6. git init at the install root - history + rollback layer (spec A2). Best-effort.
    _git_init(p["install_base"])
    print("\nInstall complete.")
    print_launch(p)


def print_launch(p):
    # Platform-aware launch steps: a Windows box must NOT be told to `source ... .sh`
    # (bash-only, wrong env file). Both env files are written; print the one for THIS OS.
    # (CS catch 2026-06-22 — the .sh/source line was a Mac-ism on the success path.)
    #
    # AUTONOMOUS-WAKE flow (2026-07-12): the human's ONLY per-launch choice is WHICH worker to boot
    # — set via SYMPHONY_WORKER. After that there are ZERO manual steps: the agnostic CLAUDE.md
    # self-resolves identity from $SYMPHONY_WORKER (reads its stack, runs the L3 gate itself), and a
    # SessionStart hook auto-arms that worker's F9 poller. No per-launch identity regen, no manual
    # poller-arm. (The old flow hand-ran cohort_id_load_confirm at launch; that's now the body's own
    # first-wake action per the seed, not a human step.)
    th = p['team_home']
    py = _venv_python(p['venv_dir'])
    worker = p.get('worker') or "<worker>"
    print("\n" + "-" * 64)
    print("LAUNCH (human-run, in a terminal - Symphony is terminal-bound):")
    print("  ONE command. The only choice is which worker; everything else self-resolves from the body.")
    if os.name == "nt":
        print(f'  >  "{th / "symphony_wake.ps1"}" {worker}   [ + any claude args ]')
    else:
        print(f'  >  "{th / "symphony_wake.sh"}" {worker}   [ + any claude args, e.g. --dangerously-skip-permissions ]')
    print("     The wrapper sets SYMPHONY_WORKER, sources the FULL body env into the process (so claude")
    print("     AND every hook it fires resolve to the sandbox: soul_dev, this body's bus, its own tools),")
    print("     pre-arms the F9 poller (bus-aware), then launches CC from the body dir. On wake the body")
    print("     reads its identity for that worker, runs its own L3 gate, and is live + hearing — no")
    print("     manual source, no regen, no manual poller-arm.")
    print("  (fallback, raw claude:) a SessionStart hook self-sources the env too — but the wrapper is")
    print("     the robust path (it also covers the inherited soul hooks + sidesteps the trust gate).")
    print(f"  (comms) start the spaceship:  {py} {th / 'server.py'}    # bus/comms server")
    print("-" * 64)


if __name__ == "__main__":
    main()
