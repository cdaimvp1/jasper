"""
v0.4 (2026-07-13): worker-AGNOSTIC (Abe catch) — config carries {cohort, soul_root} only, NOT worker;
         worker sourced from $SYMPHONY_WORKER per-launch (one body serves quinn|mira|atlas). identify_failed if unset+sandbox.
v0.3 (2026-07-13): + proj_dir(CLAUDE_PROJECT_DIR) as config-locate candidate — re-test hook-env log
         confirmed it resolves to the BODY dir (not shared git-root) → raw-claude locates config directly, git-move moot.
v0.2 (2026-07-12): + never-live clamp on the final value (Sage+TB review) — config path could
         flip a sandbox to live; clamp makes the never-live invariant structural for ALL sources.

resolve_symphony_identity() — SHARED isolation-resolution helper (Coby's isolation-spec, pp_481d6730d9 → this).
ONE helper, THREE callers: precompact_soul_mirror.py (Sage) · sessionstart_soul_hydrate.py (TB) · the fail-closed guard (TB).
Import it so all three resolve identically and can't drift (solves TB's "resolve identically").

Design (converged in proj_c8f384b8f5, 2026-07-12):
  Precedence — TWO layers, boundary is authoritative + fail-closed:
    Layer 1 (BOUNDARY, never-live): am-I-a-sandbox-body. AUTHORITATIVE. Ignores config + env.
      Positive sandbox signals (either → sandbox), both robust on their launch path:
        (a) SYMPHONY_WORKER present in THIS process env  — WRAPPER path (symphony_wake.sh exports it
            into the real process env before exec claude; hook subprocesses inherit it. VERIFIED robust,
            independent of CLAUDE_PROJECT_DIR).
        (b) CLAUDE_PROJECT_DIR resolves under SYMPHONY_UNIFIED — RAW-CLAUDE path (CC sets it to the
            git-root = SYMPHONY_UNIFIED for a sandbox session; Abe disk-verified the single .git at
            install_base. CC exports CLAUDE_PROJECT_DIR to hook subprocesses per docs — BUILD-GATE:
            confirm non-empty in a SANDBOX HOOK env via the one-line log on next launch before relying
            on this leg for raw-claude; wrapper path does not need it).
      → sandbox ⇒ NEVER write/read live soul/, period. Nothing (env, config) can flip this.
    Layer 2 (SPECIFICS): soul_root + cohort from the body-local config (best-effort located); worker from
      $SYMPHONY_WORKER per-launch (NOT config — worker-agnostic body, one config serves all workers).
      A config MISS is a LOUD identify-fail (marker), NEVER a live write — Layer 1 already locked never-live.
    env: within-sandbox convenience ONLY; may NEVER cross the sandbox↔live boundary (Sage's guardian rule +
      Coby pt-2). Not consulted for the boundary decision at all.
    else (not sandbox): LIVE path — production backward-compat, unchanged (soul_root='soul').
"""
import os
from pathlib import Path

SYMPHONY_MARKER = "SYMPHONY_UNIFIED"   # the install_base dirname; presence in the anchor path ⇒ sandbox


def _under_symphony(p: str) -> bool:
    if not p:
        return False
    try:
        return SYMPHONY_MARKER in Path(p).resolve().parts
    except Exception:
        return SYMPHONY_MARKER in p


def resolve_symphony_identity(payload_cwd: str = "") -> dict:
    """Returns {is_sandbox, soul_root, worker, cohort, archetype, identity_source, role_doc_path, archetype_doc_path, identify_failed}.
    payload_cwd = the hook-input 'cwd' (best-effort Layer-2 config locator ONLY; NEVER the boundary — it's mutable).

    archetype + identity_source (added 2026-07-13, Symphony role-identity wiring — MAP form, cohort-locked):
      archetype = symphony_identity.json['roles'][$SYMPHONY_WORKER] — a worker→archetype MAP keyed by the env-worker,
        the SAME body-config path as cohort/soul_root (Sage code-verified: helper reads config, NOT a roster; roster =
        materialize's build-INPUT that WRITES config.roles). MAP not scalar because a worker-AGNOSTIC body serves
        quinn|mira|atlas from ONE config → a single archetype would be the 7→7000 collision (mira-launch reading quinn's
        role); the map resolves EACH worker's own role. Unmapped/absent → "" (un-migrated: current behavior, NOT a
        failure — never trips identify_failed, or un-migrated workers break). Pairs with Abe's `_seed_body_config`
        `cfg['roles']` write (install_symphony.py).
      identity_source = the durable 'archetype-born' MARKER (Sage's fail-closed key): a born body's config sets
        identity_source='archetype'. L3 fail-closes on marker-present + worker-unmapped (=BROKEN born body, partial
        write) vs skips on marker-absent (=genuinely un-migrated). The marker survives even if a roles-entry drops —
        which a single value can't guarantee. The fail-closed DECISION is L3's; this helper only surfaces both fields."""

    # ---- Layer 1: BOUNDARY (authoritative, fail-closed) ----
    worker_env = os.environ.get("SYMPHONY_WORKER", "").strip()
    proj_dir = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    is_sandbox = bool(worker_env) or _under_symphony(proj_dir)

    if not is_sandbox:
        # production / live — backward-compat unchanged. env/default handle soul_root as today.
        return {"is_sandbox": False, "soul_root": "soul",
                "worker": "", "cohort": "", "archetype": "", "delta": {}, "identity_source": "",
                "role_doc_path": "", "archetype_doc_path": "",
                "identity_dir": "", "cohort_identity_dir": "", "identify_failed": False}

    # ---- sandbox: never-live is now LOCKED. Layer 2 only chooses WHICH dev-side specifics. ----
    soul_root, worker, cohort, archetype, identity_source, identify_failed = None, worker_env, "", "", "", False
    role_doc_path, archetype_doc_path = "", ""
    identity_dir, cohort_identity_dir = "", ""   # (2026-07-14) L3-read stack pointers for the born §0 wake-protocol injection
    delta = {}   # per-worker identity delta (2026-07-14 shape migration): dict roles-entry carries {archetype, delta}

    # WORKER-AGNOSTIC (Abe's catch 2026-07-13): one body serves quinn|mira|atlas via $SYMPHONY_WORKER.
    # The shared body config CANNOT carry a fixed `worker` (a mira-launch would read a quinn-written
    # worker = wrong identity, the 7→7000 collision). So worker comes ONLY from $SYMPHONY_WORKER (per-launch
    # env, set at L60 = worker_env); config carries only the cohort-level {cohort, soul_root}. If we're a
    # sandbox but $SYMPHONY_WORKER is unset, we can't identify the worker → identify_failed (loud, never live).
    if not worker:
        identify_failed = True
    # locate body-local config best-effort: payload_cwd's body dir, else CLAUDE_PROJECT_DIR (body dir).
    cfg = _find_body_config(payload_cwd, proj_dir)
    if cfg:
        import json
        try:
            data = json.loads(Path(cfg).read_text())
            soul_root = (data.get("soul_root") or "").strip() or None
            cohort = (data.get("cohort") or "").strip()      # worker deliberately NOT read from config (agnostic)
            # archetype from the roles-MAP keyed by $SYMPHONY_WORKER (worker-agnostic — Abe's 7→7000 guard applied to
            # archetype: one body serving quinn|mira|atlas resolves EACH one's own role; a single scalar would collide).
            # SHAPE MIGRATION 2026-07-14 (step-7): roles[worker] is now a dict {archetype, delta} so a per-worker
            # identity delta rides alongside the archetype-pointer. SHAPE-TOLERANT by design (Coby) — a dict entry
            # yields {archetype, delta}; a legacy scalar entry still yields the archetype with delta={} — so the
            # resolver never breaks regardless of whether the config-writer (Abe's install_symphony.py) has migrated
            # yet. Removes the 2-editor lockstep fragility: the two edits can land in either order, both correct.
            _role_entry = (data.get("roles") or {}).get(worker)
            if isinstance(_role_entry, dict):
                archetype = (_role_entry.get("archetype") or "").strip()
                delta = _role_entry.get("delta") or {}
            else:
                archetype = (_role_entry or "").strip()   # legacy scalar form → no per-worker delta
                delta = {}
            # identity_source = the durable "archetype-born" marker (Sage's fail-closed key): a born body sets
            # identity_source="archetype"; a worker unmapped in roles while marked = BROKEN (L3 fail-closes), vs
            # unmarked = genuinely un-migrated (skip). The marker survives even if a worker's roles-entry is dropped.
            identity_source = (data.get("identity_source") or "").strip()
        except Exception:
            identify_failed = True
    else:
        identify_failed = True

    # config-miss / no soul_root in config: LOUD identify-fail, but STILL never-live.
    # Default to a safe sandbox-side root (soul_dev) so a config miss degrades to "wrong-dev-root at worst",
    # NEVER to live. env may refine to another *dev-side* value but may not set it to 'soul' (boundary is locked).
    if not soul_root:
        env_root = os.environ.get("SYMPHONY_SOUL_ROOT", "").strip()
        soul_root = env_root if (env_root and env_root != "soul") else "soul_dev"
        if not (cfg and not identify_failed):
            identify_failed = True  # surface: we're isolated (safe) but couldn't positively identify the body

    # NEVER-LIVE LOCK — structural, ALL sources (Sage + TB review catch 2026-07-12, both independent).
    # The invariant "sandbox ⇒ soul_root != 'soul'" now lives in CODE, not just the docstring: whatever the
    # source (config with soul_root:'soul', a stray env, a generator bug, a hand-edit), a sandbox body can
    # NEVER resolve to the live root. Hoisted to the final value so config path + env path + any future
    # source are all covered in one clamp. Loud (identify_failed) so a live-pointing attempt surfaces.
    if soul_root == "soul":
        soul_root = "soul_dev"
        identify_failed = True

    # L3-facing resolved paths (TB pp_e9e2923215; Abe layout-verified pp_6ffce2ac3c) — ONE-resolver: the helper derives
    # them so L3 adds them to required-reads without re-deriving (no drift). Sandbox-CLAMPED by construction: workspace_root
    # sits under the sandbox install tree, so both paths are the sandbox instances (never the live cohort's) — Quinn's
    # isolation constraint satisfied at the design layer; Mira's First-Wake-Clean confirms it on the born body.
    # Derive ONLY for a resolved archetype-body (archetype present): a resolved archetype ⇒ worker was in roles ⇒ both
    # paths exist. Un-migrated (no archetype) → paths stay "" → L3 skips. Marked+unmapped (ghost: archetype="") → paths
    # stay "" → L3 sees marker + no role_doc_path → FAIL-CLOSED. So gating both on `archetype` gives all 3 L3 cases right.
    if cfg and archetype:
        body_dir = Path(cfg).parent.parent               # <body> (config-file → config-dir → body)
        workspace_root = body_dir.parent / "workspace"   # <cohort>/workspace (ONE .parent — Abe-verified; == his write/hydrate loc)
        role_doc_path = str(workspace_root / "cohort_substrate" / cohort / "workers" / f"{worker}_role.md")
        archetype_doc_path = str(workspace_root / "cohort_substrate" / "_shared" / "archetypes" / f"{archetype}.md")
        # L3-read stack POINTERS for the born §0 wake-protocol (2026-07-14, option-B hook injects these as paths,
        # NOT content — floor-#6 read-live-never-bake, Atlas pp_755e1bc923). The born worker reads SOUL/HEART/
        # PRINCIPLES/MEDIUM/CHANGELOG from identity_dir + the cohort deltas from cohort_identity_dir, then confirms.
        identity_dir = str(workspace_root / "cohort_substrate" / "_shared" / "identity")
        cohort_identity_dir = str(workspace_root / "cohort_substrate" / cohort / "identity")

    return {"is_sandbox": True, "soul_root": soul_root,
            "worker": worker, "cohort": cohort, "archetype": archetype,
            "delta": delta,
            "identity_source": identity_source,
            "role_doc_path": role_doc_path, "archetype_doc_path": archetype_doc_path,
            "identity_dir": identity_dir, "cohort_identity_dir": cohort_identity_dir,
            "identify_failed": identify_failed}


def _find_body_config(payload_cwd: str, proj_dir: str):
    """Locate <cohort>/body/config/symphony_identity.json (Abe's install layout, pp this-thread).
    PRIMARY = payload_cwd: the launched body dir (the wrapper `cd`s to `.../<cohort>/body` before exec),
    so config = <body>/config/symphony_identity.json; <cohort> = the path part directly under SYMPHONY_UNIFIED.
    Walk up to the nearest 'body' ancestor (handles payload_cwd being a subdir of body).
    proj_dir (CLAUDE_PROJECT_DIR) IS also a candidate — EMPIRICALLY CONFIRMED 2026-07-13 (Abe's hook-env log,
    re-test): CC resolves it to the BODY dir (`.../<cohort>/body`), NOT the shared git-root we'd assumed. So it
    identifies the specific body directly → raw-claude can locate config via it (no git-move needed). Safe either
    way: if proj_dir ever IS the shared top (SYMPHONY_UNIFIED), the walk finds no 'body' → returns None → fail-loud
    (never guesses a cohort from the shared top; clamp keeps it never-live)."""
    # BORN-BODY-SCOPED (2026-07-14, layout A: body co-located under $SYM_HOME, which is NOT necessarily a
    # SYMPHONY_UNIFIED-named path — e.g. a /tmp test-home. The old SYMPHONY_MARKER-in-path gate wrongly SKIPPED
    # a valid born body whose install root isn't literally "SYMPHONY_UNIFIED" → identify_failed on the clean-
    # machine layout). Anchor on the launcher-set born-body dir (payload_cwd = the wrapper's cd-target;
    # proj_dir = CLAUDE_PROJECT_DIR = the born body) and walk UP to the nearest 'body' ancestor's config.
    # NO path-name-marker requirement. ISOLATION PRESERVED (Sage pp_6a9a5b21a4, dual-purpose marker): discovery
    # is SCOPED to the explicit born-body anchor (payload_cwd/CLAUDE_PROJECT_DIR the wake sets), NOT "any config
    # anywhere" — a born worker resolves ONLY its OWN body's config; + the never-live-clamp (soul_root!='soul'
    # → soul_dev) below stays UNCONDITIONAL as the 2nd fence. Bounded: stop at the nearest 'body' dir or fs-root.
    for raw in (payload_cwd, proj_dir):     # payload_cwd first (wrapper cd-target), then CLAUDE_PROJECT_DIR (born body)
        if not raw:
            continue
        try:
            base = Path(raw).resolve()
        except Exception:
            continue
        for d in [base, *base.parents]:
            if d.name == "body":
                cand = d / "config" / "symphony_identity.json"
                return str(cand) if cand.exists() else None   # nearest 'body' = the born body; bounded stop
            cand = d / "config" / "symphony_identity.json"
            if cand.exists():
                return str(cand)
            if d == d.parent:      # filesystem root — stop (don't wander past the born-body anchor)
                break
    return None


if __name__ == "__main__":
    # Born-body wake entry-point (2026-07-14, option-B): symphony_sessionstart.sh invokes
    #   python3 "$TEAM_SCRIPTS_ROOT/resolve_symphony_identity.py"
    # to get the resolved identity dict as JSON, then emits the §0 wake-protocol (POINTERS ONLY —
    # archetype_doc_path/role_doc_path/identity_dir/cohort_identity_dir — the worker reads them LIVE;
    # floor-#6 read-live-never-bake). Reads SYMPHONY_WORKER + CLAUDE_PROJECT_DIR from env;
    # payload_cwd = the launched cwd (the wrapper cd's to the born body before exec).
    import json as _json
    print(_json.dumps(resolve_symphony_identity(payload_cwd=os.getcwd())))
