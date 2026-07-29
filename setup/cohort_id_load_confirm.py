#!/usr/bin/env python3
"""cohort_id_load_confirm · L3 identity-load gate marker writer.

Per TB master-weave `tr_f9352655d0` + CB spec DM `m_507b3a1d6e` (2026-05-17).

Reads the four §0 cohort identity docs · computes sha256 of each file's bytes
(not mtime — touch-bypass-proof) · writes a marker file scoped to the worker's
session_pid. The L3 gate in cohort_post.post() reads this marker; absence or
sha-mismatch → reject with IDENTITY_NOT_LOADED.

Usage:
    python3 cohort_id_load_confirm.py <worker>
        # exit 0 + stdout "IDENTITY_LOADED · session_pid=<pid> · shas=<short>"
        # exit 1 + stdout "FAILED · <reason>"

Marker scope:
    ~/.cache/cohort/<worker>_id_loaded_<session_pid>.json
    session_pid = nearest `claude` ancestor process (stable; NOT the transient Bash subshell — fixed 2026-07-04)
    resets per restart / compact / new session automatically

Marker contents:
    {
      "worker": "<worker>",
      "session_pid": <int>,
      "ts": "<iso>",
      "file_shas": {
        "SOUL.md": "<sha256>",
        "HEART.md": "<sha256>",
        "PRINCIPLES.md": "<sha256>",
        "CHANGELOG.md": "<sha256>"
      },
      "changelog_sha": "<sha256>",  # extracted for gate's invalidation check
      "doctrine_path": "<abs path to cohort_identity dir>"
    }

Doctrine invalidation: gate compares marker.changelog_sha to current
CHANGELOG.md sha. Drift → reject (doctrine moved · cohort must re-anchor).
"""
import glob as _glob
import hashlib
import json
import os
import sys
import urllib.error as _urlerr
import urllib.request as _urlreq
from datetime import datetime, timezone
from pathlib import Path

# Cohort registry now imported from the single shared source (cohort_registry.py in
# the team root) — kills the two-mirror drift with cohort_post.py. This script runs
# from setup/, so add the team root to sys.path before importing (cohort_registry
# resolves paths.py through the same path). Registry derives all homes off
# paths.WORKSPACE_ROOT (TEAM_WORKSPACE_ROOT env → Claude-AI-Assets fallback):
# byte-identical on Mac, Windows-correct once the installer sets the env. Supersedes
# the 06-20 SYMPHONY_SOUL_ROOT forward-staging (Atlas) — soul is under Claude-AI-Assets
# today (CS reconcile 2026-06-21); re-add a soul-root axis THROUGH paths if/when the
# soul physically relocates. IR doctrine_files now includes MEDIUM.md (Atlas/Quinn).
_team_root = str(Path(__file__).resolve().parent.parent)
if _team_root not in sys.path:
    sys.path.insert(0, _team_root)
from cohort_registry import COHORT_REGISTRY, WORKER_TO_COHORT_ID, VALID_WORKERS  # noqa: E402

MARKER_DIR = Path.home() / ".cache" / "cohort"



def _resolve_session_pid() -> int:
    """Walk ancestors to the nearest live `claude` CLI process.

    FIX 2026-07-04 (Sage's trace): raw os.getppid() returns the Bash-tool's
    transient subshell (dead by the next tool call), so markers were keyed to
    throwaway pids and the tripwire's liveness check false-fired on genuinely
    loaded workers. The stable identity anchor is the claude session process
    itself. Fallback: getppid() (pre-fix behavior) if no claude ancestor found.
    """
    import subprocess
    pid = os.getppid()
    for _ in range(15):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            if not out:
                break
            parts = out.split(None, 1)
            ppid = int(parts[0])
            command = parts[1] if len(parts) > 1 else ""
            base = command.split()[0].rsplit("/", 1)[-1] if command else ""
            if base == "claude" or base.startswith("claude"):
                return pid
            if ppid <= 1:
                break
            pid = ppid
        except Exception:
            break
    return os.getppid()


def _sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ~25K-token Read-tool cap ≈ 64KB of markdown: files above this size truncate on a
# single Read, so evidence must show a paged (offset>0) or repeated read to count.
_PAGING_REQUIRED_BYTES = 64 * 1024

# SHA-ONLY files (move A, 2026-07-11, George baton tr_b387ad3b3c · re-tier safe-half):
# these doctrine files are TRACKED for drift (sha computed + stored + compared by the
# cohort_post gate) but NOT read-evidence-REQUIRED on wake — because they are HISTORY /
# reference, not identity-SHAPING. CHANGELOG is pure amendment history (its own header:
# "tracks every doc change"); a worker wakes cohort-shaped from SOUL/HEART/PRINCIPLES +
# its own lane, and re-anchors on doctrine-drift via the sha — not by reading 65KB of
# dated change-records every wake. The 2026-07-07 read-evidence scar was about workers
# skipping the SHAPING docs; dropping CHANGELOG's read-requirement does NOT reopen it
# (the shaping docs stay fully read-enforced). Additive + non-breaking: still reading the
# full CHANGELOG passes fine — it's just no longer forced. "What changed since last wake"
# is surfaced via the generated CHANGELOG_recent extract (wake-block, Abe's lane).
SHA_ONLY_FILES = {"CHANGELOG.md"}


def _find_transcript(session_id: str) -> "Path | None":
    hits = _glob.glob(str(Path.home() / ".claude" / "projects" / "*" / f"{session_id}.jsonl"))
    return Path(hits[0]) if hits else None


def _verify_read_evidence(required: "dict[str, Path]") -> tuple[bool, str]:
    """Scan THIS session's transcript for actual Read-tool accesses of the identity docs.

    Added 2026-07-07 (N=3 same-evening: AB skipped the reads entirely, CB and TB both
    ran the confirm before reading — the marker was a proxy, not proof of read, and
    George's cross-worker sat comparison caught what the gate structurally couldn't).
    Per P.4.5 rule-of-three + AB's structural flag pp_68cfcd566c.

    Rules:
    - Evidence window = after the LAST compact_boundary in the transcript (a compact
      invalidates prior reads — the post-compact session must re-read).
    - Every required doc needs >=1 Read tool_use whose file_path matches it.
    - Docs larger than one Read-cap page (~64KB) additionally need a paged read
      (offset>0) or a second Read — exactly where a lazy read stops.
    - Fail OPEN (with a logged note) only when evidence is unavailable: no
      CLAUDE_CODE_SESSION_ID (old CC) or no transcript file. Fail CLOSED when the
      transcript exists but the reads aren't in it.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not session_id or len(session_id) < 8:
        return True, "EVIDENCE_SKIPPED · no CLAUDE_CODE_SESSION_ID in env (old CC?)"
    transcript = _find_transcript(session_id)
    if transcript is None:
        return True, f"EVIDENCE_SKIPPED · no transcript for session {session_id[:8]}"

    # Single streaming pass: remember the byte-cheap facts only. Substring pre-filter
    # keeps the JSON parse off the overwhelming majority of lines (transcripts run to
    # hundreds of MB).
    reads_after: dict[str, list] = {name: [] for name in required}
    boundary_seen_at = -1
    line_no = -1

    # SHA-belt (2026-07-16, Quinn #1b — fixes the two-copy READ_EVIDENCE false-fail, N=3):
    # a byte-identical copy of a required doc at a DIFFERENT path (e.g. the pulled archetype-library
    # copy vs the resolver's canonical workspace copy) should COUNT — the worker demonstrably read the
    # same identity CONTENT. Match on content-sha256, NOT basename/path (Sage's guardrail: a
    # same-basename but DIVERGENT-content copy must still FAIL, else it's a hole). Precompute each
    # required doc's sha once; cache read-file shas so each transcript fp is hashed at most once.
    def _sha256_of(p) -> "str | None":
        try:
            h = hashlib.sha256()
            with open(p, "rb") as _fh:
                for _chunk in iter(lambda: _fh.read(65536), b""):
                    h.update(_chunk)
            return h.hexdigest()
        except OSError:
            return None  # unreadable → skip (don't crash, don't count via the belt)
    required_sha: dict[str, str] = {}
    for _name, _path in required.items():
        _s = _sha256_of(_path)
        if _s is not None:
            required_sha[_name] = _s
    _fp_sha_cache: dict[str, "str | None"] = {}
    try:
        with open(transcript, "r", errors="replace") as f:
            for line_no, line in enumerate(f):
                if '"compact_boundary"' in line:
                    boundary_seen_at = line_no
                    for lst in reads_after.values():
                        lst.clear()  # compact invalidates prior reads
                    continue
                if '"tool_use"' not in line or '"Read"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                for blk in ((d.get("message") or {}).get("content") or []):
                    if not (isinstance(blk, dict) and blk.get("type") == "tool_use"
                            and blk.get("name") == "Read"):
                        continue
                    fp = (blk.get("input") or {}).get("file_path", "")
                    for name, path in required.items():
                        if fp == str(path) or fp.casefold() == str(path).casefold() or (fp.endswith("/" + name) and "identity" in fp):
                            reads_after[name].append((blk.get("input") or {}).get("offset"))
                        elif fp and name in required_sha:
                            # SHA-belt fallback (only if the path fast-paths above missed for this doc):
                            # count a read of a byte-identical copy at another path. Content-sha match
                            # only — a divergent same-name copy still fails. Records every matching read
                            # (incl. paged reads of a large doc) so the paging check below stays correct.
                            if fp not in _fp_sha_cache:
                                _fp_sha_cache[fp] = _sha256_of(Path(fp))
                            if _fp_sha_cache[fp] is not None and _fp_sha_cache[fp] == required_sha[name]:
                                reads_after[name].append((blk.get("input") or {}).get("offset"))
    except OSError as e:
        return True, f"EVIDENCE_SKIPPED · transcript unreadable: {type(e).__name__}"

    missing, unpaged = [], []
    for name, path in required.items():
        offsets = reads_after[name]
        if not offsets:
            missing.append(name)
            continue
        try:
            needs_paging = path.stat().st_size > _PAGING_REQUIRED_BYTES
        except OSError:
            needs_paging = False
        if needs_paging and len(offsets) < 2 and not any(o for o in offsets if o):
            unpaged.append(name)
    if missing or unpaged:
        parts = []
        if missing:
            parts.append(f"not read this session (post-compact): {', '.join(missing)}")
        if unpaged:
            parts.append(f"read once but truncated — page past the Read cap: {', '.join(unpaged)}")
        return False, (
            "IDENTITY_NOT_YET_CONFIRMED · READ_EVIDENCE incomplete — this is NORMAL on first wake and "
            "nothing is broken; the gate is just making sure you loaded your full doctrine before acting. "
            "A couple of docs still need a complete read: "
            + " · ".join(parts)
            + " · Read them with the Read tool (not shell) — if PRINCIPLES.md visibly stops mid-document, "
            "page past it with a 2nd Read at an offset — then re-run this command. Expected on first wake; "
            "just finish the read and you'll confirm clean."
        )
    note = f"reads verified in transcript (boundary@{boundary_seen_at})" if boundary_seen_at >= 0 \
        else "reads verified in transcript (fresh session)"
    return True, f"EVIDENCE_OK · {note}"


def _register_session_with_substrate(worker: str) -> "str | None":
    """Find THIS Claude session's heartbeat and register it with substrate.

    Discovery rule (per scar 2026-05-30 mis-attribution · #187 v1.1):
    Naive "freshest unresolved" races other workers' unresolved heartbeats and
    mis-registers them as ours. Discriminator: heartbeat written in last FRESH_S
    seconds (statusline.py fires just before this script · same prompt cycle).
    If multiple matches AND ambiguous, REFUSE rather than guess — better to skip
    than mis-attribute someone else's session.

    Returns:
      None on quiet skip · "REGISTERED · ..." on success · "REGISTER_SKIPPED · ..."
    """
    # AUTHORITATIVE session_id from Claude Code env (fixes mis-attribution · 2026-05-31 TB+Quinn root-cause
    # tr/pp_f1652ff883). The /tmp-heartbeat guessing below races other workers' unresolved heartbeats in a
    # shared cwd → mis-registers (e.g. tb/atlas/mira all stamped 'quinn', who registered most often).
    # CLAUDE_CODE_SESSION_ID is set by Claude Code per-session + inherited by this subprocess = deterministic,
    # caller-correct, no guess. Use it when present; fall back to legacy discovery only if absent.
    # ISOLATION FIX 2026-07-13 (TB, born-body register-leak): honor COHORT_BASE so a SANDBOX body
    # registers on ITS OWN bus, never the live :8675. On a live box COHORT_BASE is unset → default
    # 127.0.0.1:8675 (unchanged). On a born archetype-body the installer sets COHORT_BASE=:8676 →
    # register targets the sandbox; if that server is down → REGISTER_SKIPPED substrate-unreachable
    # = SAFE (zero live write). Same hardcoded-8675 client class the installer flagged as TB's lane
    # (cohort_post COHORT_BASE). Invariant: sandbox-:8676-or-nothing, NEVER live-:8675.
    _reg_base = os.environ.get("COHORT_BASE", "http://127.0.0.1:8675").rstrip("/")
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if env_sid and len(env_sid) >= 8:
        try:
            req = _urlreq.Request(
                _reg_base + "/api/cohort/register-session",
                data=json.dumps({"session_id": env_sid, "worker": worker}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _urlreq.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read())
                if payload.get("ok"):
                    return (f"REGISTERED · session_id={env_sid[:8]} · worker={worker} · "
                            f"map_size={payload.get('map_size')} · src=env")
        except _urlerr.HTTPError as e:
            # Server RESPONDED (so it is UP) — the route is just absent in this build, or errored.
            # HTTPError is a SUBCLASS of URLError, so it MUST be caught first or it falls into the
            # substrate-unreachable arm below (the born-TO-wake mislabel, 2026-07-16): a 404 on the
            # lean server printed "substrate-unreachable" and made a fresh TO think its server was
            # down when :8700 was serving fine. 404/405 = server_lean lacks /api/cohort/register-session
            # = BENIGN skip (identity already loaded + marker written regardless of this call).
            if e.code in (404, 405):
                return "REGISTER_SKIPPED · register-route-absent (benign · server up, endpoint not in lean build)"
            return f"REGISTER_SKIPPED · register-http-{e.code} (server up)"
        except (_urlerr.URLError, OSError) as e:
            return f"REGISTER_SKIPPED · substrate-unreachable · {type(e).__name__}"
        except Exception as e:
            return f"REGISTER_SKIPPED · {type(e).__name__}: {str(e)[:80]}"
    # Fallback (older Claude Code without CLAUDE_CODE_SESSION_ID): legacy fresh-heartbeat discovery.
    FRESH_S = 8.0  # heartbeat must be written within this window to count as "ours"
    try:
        import time as _t
        now = _t.time()
        candidates = _glob.glob("/tmp/cohort_sat_sess_*.json")
        matches = []
        for path in candidates:
            try:
                d = json.loads(Path(path).read_text())
                hb_worker = d.get("worker")
                if hb_worker != "unresolved" and hb_worker != worker:
                    continue
                ts = float(d.get("ts", 0))
                sid = d.get("session_id", "")
                if (now - ts) <= FRESH_S and sid and len(sid) >= 8:
                    matches.append((ts, sid, hb_worker))
            except Exception:
                continue
        if not matches:
            return "REGISTER_SKIPPED · no-fresh-heartbeat-in-8s-window"
        if len(matches) > 1:
            sids_short = ",".join(s[:8] for _, s, _ in matches[:3])
            return f"REGISTER_SKIPPED · ambiguous · {len(matches)}-candidates · {sids_short} · pass argv[2]=session_id to disambiguate"
        best_sid = matches[0][1]
        try:
            req = _urlreq.Request(
                _reg_base + "/api/cohort/register-session",
                data=json.dumps({"session_id": best_sid, "worker": worker}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _urlreq.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read())
                if payload.get("ok"):
                    return f"REGISTERED · session_id={best_sid[:8]} · worker={worker} · map_size={payload.get('map_size')}"
        except _urlerr.HTTPError as e:
            # Server RESPONDED (so it is UP) — the route is just absent in this build, or errored.
            # HTTPError is a SUBCLASS of URLError, so it MUST be caught first or it falls into the
            # substrate-unreachable arm below (the born-TO-wake mislabel, 2026-07-16): a 404 on the
            # lean server printed "substrate-unreachable" and made a fresh TO think its server was
            # down when :8700 was serving fine. 404/405 = server_lean lacks /api/cohort/register-session
            # = BENIGN skip (identity already loaded + marker written regardless of this call).
            if e.code in (404, 405):
                return "REGISTER_SKIPPED · register-route-absent (benign · server up, endpoint not in lean build)"
            return f"REGISTER_SKIPPED · register-http-{e.code} (server up)"
        except (_urlerr.URLError, OSError) as e:
            return f"REGISTER_SKIPPED · substrate-unreachable · {type(e).__name__}"
        except Exception as e:
            return f"REGISTER_SKIPPED · {type(e).__name__}: {str(e)[:80]}"
    except Exception as e:
        return f"REGISTER_SKIPPED · top-level: {type(e).__name__}"
    return None


def _read_identity_source_direct() -> str:
    """Import-INDEPENDENT read of the archetype marker from the born body's config
    (Coby+Sage fail-closed backstop, 2026-07-13): if the cross-tree helper import fails,
    L3 must still know whether this body CLAIMS the archetype model — else a broken born
    body silently fail-OPENS. Returns identity_source ('' if no config / not a sandbox
    body → un-migrated / live → skip is safe). Fully guarded: never raises."""
    try:
        proj = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
        if not proj:
            return ""
        cp = Path(proj) / "config" / "symphony_identity.json"
        if cp.is_file():
            data = json.loads(cp.read_text())
            return str(data.get("identity_source") or "").strip()
    except Exception:
        pass
    return ""


def _try_resolve_symphony_identity() -> "dict | None":
    """Guarded import of resolve_symphony_identity — shipped to body/setup/ on a born body
    (= same dir as this file), lives in aria_sync/tools/cohort_hooks/ on a live box. Returns
    the identity dict, or None if unimportable (→ L3 fail-closes a MARKED body). Never raises."""
    import importlib
    tried = []
    try:
        tried.append(os.path.dirname(os.path.abspath(__file__)))          # born body: same dir (body/setup/)
        tried.append(str(Path(__file__).resolve().parents[2] / "aria_sync" / "tools" / "cohort_hooks"))  # live fallback
    except Exception:
        pass
    for d in tried:
        try:
            if d and d not in sys.path:
                sys.path.insert(0, d)
            mod = importlib.import_module("resolve_symphony_identity")
            fn = getattr(mod, "resolve_symphony_identity", None)
            if callable(fn):
                # payload_cwd = the born-body dir derived from THIS FILE's location (2026-07-15, Coby —
                # completes the resolver-derived read-set fix). ROOT CAUSE (George's live wake pp_fe60dbd235):
                # this called fn() with no payload_cwd; a REAL born-TB wake has CLAUDE_PROJECT_DIR UNSET (CC
                # 2.1.207), so the resolver found NO config → identity_source unresolved → confirm()'s archetype-
                # gate never fired → fell to the static aria_canon registry → "doctrine dir missing". My earlier
                # patch fixed confirm()'s read-set but MISSED this call-site; tests masked it (live-worker/ELSE
                # branch + CLAUDE_PROJECT_DIR-set repros — verify at the REAL wake env, unset).
                # ROBUST derivation (Sage pp_286998): use __file__'s body dir, NOT os.getcwd() — cwd is the
                # wrapper's cd-target at a standard wake but unreliable across invocation contexts. This file
                # lives at <body>/setup/cohort_id_load_confirm.py → parent.parent = <body>; the resolver's
                # _find_body_config walks from there to the 'body' dir → locates <body>/config/symphony_identity.json
                # (+ still falls back to CLAUDE_PROJECT_DIR internally). Live/un-migrated bodies: is_sandbox=False
                # → resolver returns before consulting payload_cwd, so this is harmless for them.
                _body_anchor = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                return fn(payload_cwd=_body_anchor)
        except Exception:
            continue
    return None


def _archetype_enforcement(worker: str) -> "dict | None":
    """Symphony role-identity enforcement for this wake (2026-07-13). Returns:
      None                                → un-migrated / live: skip (no extra required-read)
      {"fail_closed": True, "reason": ..} → marked archetype-body that can't be verified: FAIL-CLOSED
      {"role_doc_path":.., "archetype_doc_path":..} → enforce these as required-reads
    Design (Coby+Sage+Quinn+Mira converge): fail-closed KEY (identity_source) read
    import-INDEPENDENTLY so an install/path bug on a marked body fail-CLOSES, never
    fail-opens. Un-migrated/live bodies (no marker) reach only the guarded marker-read →
    skip → current behavior (no crash risk). Marked path is try/wrapped → any error =
    fail-closed, never a gate crash."""
    marker = _read_identity_source_direct()
    ident = _try_resolve_symphony_identity()
    # prefer the helper's marker (full boundary-aware resolve) when importable; else the direct read
    src = (str(ident.get("identity_source")).strip() if ident else "") or marker
    if src != "archetype":
        return None  # un-migrated / live → skip
    # MARKED archetype-body: must positively verify or FAIL-CLOSED.
    try:
        if ident is None:
            return {"fail_closed": True,
                    "reason": "ARCHETYPE_UNVERIFIABLE · identity_source=archetype but resolve_symphony_identity unimportable — fail-closed (broken body)"}
        arch = str(ident.get("archetype") or "").strip()
        if not arch:
            return {"fail_closed": True,
                    "reason": "ARCHETYPE_UNMAPPED · identity_source=archetype but no archetype resolved for this worker (ghost) — fail-closed"}
        rdp = str(ident.get("role_doc_path") or "").strip()
        adp = str(ident.get("archetype_doc_path") or "").strip()
        if not (rdp and adp):
            return {"fail_closed": True,
                    "reason": "ARCHETYPE_PATHS_UNRESOLVED · helper returned no role/archetype doc paths — fail-closed (enforce-path pending helper-returns-paths)"}
        if not Path(rdp).is_file():
            return {"fail_closed": True, "reason": f"ROLE_DOC_ABSENT · {rdp} — fail-closed (marked body, role-doc missing)"}
        if not Path(adp).is_file():
            return {"fail_closed": True, "reason": f"ARCHETYPE_DOC_ABSENT · {adp} — fail-closed"}
        return {"role_doc_path": rdp, "archetype_doc_path": adp}
    except Exception as e:
        return {"fail_closed": True, "reason": f"ARCHETYPE_RESOLVE_ERROR · {type(e).__name__} — fail-closed (marked body, resolve error)"}


def confirm(worker: str) -> tuple[int, str]:
    # READ-SET SOURCE (2026-07-15, Coby — required_reads-single-source invariant; fixes the
    # worker-name != registry-cohort break that fails L3 on the first non-dev born cohort).
    # A born ARCHETYPE-BODY derives its read-set from the RESOLVER's config (cohort + born-local
    # identity dirs), NOT the static WORKER_TO_COHORT_ID/COHORT_REGISTRY. The static map is a DEV
    # artifact: a born worker whose name collides with a dev worker (team_builder is ALSO aria_canon's)
    # mis-resolves to the dev cohort's ABSENT doctrine_dir -> "FAILED · doctrine dir missing" at wake.
    # born-Quinn masked it (quinn->ir_cohort statically MATCHED the born cohort); new_cohort exposes it.
    # Config-driven => collision-free + cohort-PARAMETRIC (7->7000, any born cohort_id / worker-name).
    # LIVE / un-migrated bodies (no archetype marker) keep the static path UNCHANGED (backward-compat).
    # Gated on identity_source=="archetype" — only born archetype-bodies set it (live -> else branch).
    _ident = _try_resolve_symphony_identity()
    if _ident and str(_ident.get("identity_source") or "").strip() == "archetype":
        # born archetype-body: read-set from resolver config. A marked body that cannot resolve its
        # identity_dir is BROKEN -> fail-closed HERE (never fall through to the foreign dev registry).
        _id_dir = str(_ident.get("identity_dir") or "").strip()
        if not _id_dir:
            return 1, "FAILED · archetype-body but resolver returned no identity_dir (broken born body)"
        cohort_id = str(_ident.get("cohort") or "").strip() or "?"
        doctrine_dir = Path(_id_dir)                                  # <ws>/cohort_substrate/_shared/identity
        doctrine_files = tuple(sorted(p.name for p in doctrine_dir.glob("*.md"))) if doctrine_dir.is_dir() else ()
        _cid = str(_ident.get("cohort_identity_dir") or "").strip()   # <ws>/cohort_substrate/<cohort>/identity
        delta_dir = Path(_cid) if _cid else None
        delta_files = (tuple(sorted(p.name for p in delta_dir.glob("*.md")))
                       if (delta_dir and delta_dir.is_dir()) else ())
    else:
        if worker not in VALID_WORKERS:
            return 1, f"FAILED · unknown worker '{worker}' · valid: {sorted(VALID_WORKERS)}"
        cohort_id = WORKER_TO_COHORT_ID[worker]
        info = COHORT_REGISTRY[cohort_id]
        doctrine_dir = info["doctrine_dir"]
        doctrine_files = info["doctrine_files"]
        delta_dir = info["delta_dir"]
        delta_files = info["delta_files"]

    if not doctrine_dir.is_dir():
        return 1, f"FAILED · doctrine dir missing: {doctrine_dir}"

    file_shas: dict[str, str] = {}
    # Master doctrine files (SOUL/HEART/PRINCIPLES/CHANGELOG)
    for name in doctrine_files:
        p = doctrine_dir / name
        if not p.is_file():
            return 1, f"FAILED · missing doctrine file: {p}"
        file_shas[name] = _sha256_bytes(p)
    # Cohort-specific delta files (IR cohort only · empty tuple for ARIA)
    if delta_dir is not None:
        if not delta_dir.is_dir():
            return 1, f"FAILED · delta dir missing: {delta_dir}"
        for name in delta_files:
            p = delta_dir / name
            if not p.is_file():
                return 1, f"FAILED · missing delta file: {p}"
            file_shas[name] = _sha256_bytes(p)

    # READ-EVIDENCE GATE (2026-07-07): the marker attests the docs were READ, so the
    # writer now demands proof from the session's own transcript before writing it.
    # Move A (2026-07-11): SHA_ONLY_FILES (CHANGELOG) are drift-tracked (sha above) but
    # NOT read-required — history/reference, not shaping. Shaping docs stay enforced.
    required = {name: doctrine_dir / name for name in doctrine_files if name not in SHA_ONLY_FILES}
    if delta_dir is not None:
        required.update({name: delta_dir / name for name in delta_files if name not in SHA_ONLY_FILES})
    # Symphony role-identity enforcement (2026-07-13): an archetype-instantiated body must
    # ALSO have read its role-doc + archetype-doc this wake (they carry the role-identity —
    # every-wake read-presence, same as SOUL/HEART/PRINCIPLES). Import-INDEPENDENT marker →
    # fail-closed backstop; un-migrated / live bodies (no marker) skip → current behavior.
    _arch = _archetype_enforcement(worker)
    if _arch is not None:
        if _arch.get("fail_closed"):
            return 1, f"FAILED · {_arch['reason']}"
        required[Path(_arch["role_doc_path"]).name] = Path(_arch["role_doc_path"])
        required[Path(_arch["archetype_doc_path"]).name] = Path(_arch["archetype_doc_path"])
        # growth-infra Mechanism A write-discipline (2026-07-18, Coby, TB-locked): the shared
        # memory-discipline doc lives alongside the archetype files (_shared/archetypes/), pointed to
        # by a universal §0 step (Quinn/Sage's hoist — one shared source, no per-archetype drift).
        # Enforce its read the SAME way as role/archetype docs — a text pointer alone isn't gate-verified
        # (code-exists != code-fires); require it explicitly, same pattern as the two lines above.
        # Absent file (older install, pre-Mechanism-A) -> skip enforcement, never fail-closed on it.
        _mem_disc = Path(_arch["archetype_doc_path"]).parent / "MEMORY_DISCIPLINE.md"
        if _mem_disc.is_file():
            required["MEMORY_DISCIPLINE.md"] = _mem_disc
    evidence_ok, evidence_note = _verify_read_evidence(required)
    if not evidence_ok:
        return 1, f"FAILED · {evidence_note}"

    session_pid = _resolve_session_pid()
    marker_path = MARKER_DIR / f"{worker}_id_loaded_{session_pid}.json"
    MARKER_DIR.mkdir(parents=True, exist_ok=True)

    marker = {
        "worker": worker,
        "cohort_id": cohort_id,
        "session_pid": session_pid,
        "ts": datetime.now(timezone.utc).isoformat(),
        "file_shas": file_shas,
        "changelog_sha": file_shas["CHANGELOG.md"],
        "doctrine_path": str(doctrine_dir),
        "delta_path": str(delta_dir) if delta_dir else None,
        "read_evidence": evidence_note,
    }
    marker_path.write_text(json.dumps(marker, indent=2))

    short = file_shas["CHANGELOG.md"][:8]
    delta_count = len(delta_files)
    delta_note = f" · {delta_count} delta(s) loaded" if delta_count else ""

    # Additionally register this session with the substrate so statusline.py
    # can resolve worker name (#187 · TB tr_eb34d80428 + George defer tr_a984a10edd).
    # Soft-fails · L3 marker write succeeds independently.
    reg_status = _register_session_with_substrate(worker)
    reg_note = f" · {reg_status}" if reg_status else ""

    return 0, (
        f"IDENTITY_LOADED · worker={worker} · cohort_id={cohort_id} · "
        f"session_pid={session_pid} · changelog_sha={short}{delta_note} · "
        f"{evidence_note} · marker={marker_path}{reg_note}"
    )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: cohort_id_load_confirm.py <worker>", file=sys.stderr)
        print(f"valid workers: {sorted(VALID_WORKERS)}", file=sys.stderr)
        return 2
    code, msg = confirm(sys.argv[1])
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
