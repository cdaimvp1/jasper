"""identity_root.py — INSTALLER-SIDE box detection for TEAM_WORKSPACE_ROOT.

Per the 2026-06-21 unification decision (TB+CS+AB): do NOT stand up a parallel
SYMPHONY_IDENTITY_ROOT env (that was the drift cause). The body already resolves
ITS single root from `paths.WORKSPACE_ROOT`, which reads ONE env — TEAM_WORKSPACE_ROOT
(paths.py:24) — and cohort_registry derives every identity home relative to it.

So at RUNTIME there is nothing to detect: the body just reads TEAM_WORKSPACE_ROOT.
The detection logic lives HERE, in the installer: on a fresh box (esp. Windows, where
paths.py has no auto-detect fallback) the installer must DISCOVER the synced
"Claude AI Assets - Documents" library and WRITE its path into TEAM_WORKSPACE_ROOT
(the same way it writes TEAM_HOME for the body-local ~/team paths). This module is
that discovery helper — installer-time only, not imported by the running body.

Contract:
  TEAM_WORKSPACE_ROOT already set + valid  -> honor it (idempotent re-install).
  else auto-detect the synced library on this machine (Windows or Mac).
  else legacy Mac path (so an unset Mac dev box == byte-identical to today).
The installer calls resolve_workspace_root() and persists the result into the
machine/user environment as TEAM_WORKSPACE_ROOT before first body launch.
"""
import glob
import os
from pathlib import Path

# home-portable (was a hardcoded DEV-home absolute literal — reaches a new user's box via the 5g
# closure; Sage cure-B born-portability catch 2026-07-16). Path.home() → byte-identical on the original
# dev box, correct on any other user. Still ONLY a step-3 fallback: env-set (step 1) + auto-detect (step 2)
# both precede it, and a born box always has TEAM_WORKSPACE_ROOT set → this line is never reached there;
# fixed for cure-B cleanliness + defense (a non-DA37243 box that ever DID hit it got a broken dev path).
_LEGACY_MAC_ROOT = str(
    Path.home() / "Library" / "CloudStorage" /
    "OneDrive-SharedLibraries-EliLillyandCompany" / "Claude AI Assets - Documents"
)
_LIBRARY_GLOB = "*Claude AI Assets - Documents*"
# A real workspace root contains aria_sync/ (soul + cohort comms) — the discriminator
# that tells the synced library apart from any look-alike folder.
_MARKER_SUBDIR = "aria_sync"


def _looks_like_workspace(path):
    return bool(path) and os.path.isdir(os.path.join(path, _MARKER_SUBDIR))


def resolve_workspace_root():
    # 1) explicit env already set + valid -> idempotent (re-install / dev override)
    env = os.environ.get("TEAM_WORKSPACE_ROOT")
    if _looks_like_workspace(env):
        return Path(env)
    # 2) auto-detect the synced OneDrive library on this machine (Win or Mac)
    bases = [os.environ.get(v) for v in
             ("OneDriveCommercial", "OneDrive", "USERPROFILE", "HOME")]
    for base in filter(None, bases):
        candidates = (glob.glob(os.path.join(base, _LIBRARY_GLOB)) +
                      glob.glob(os.path.join(base, "*", _LIBRARY_GLOB)) +
                      glob.glob(os.path.join(base, "Library", "CloudStorage", "*", _LIBRARY_GLOB)))
        for cand in candidates:
            if _looks_like_workspace(cand):
                return Path(cand)
    # 3) legacy Mac fallback — unset on the Mac == byte-identical to today
    return Path(_LEGACY_MAC_ROOT)


# --- Body source (the SECOND synced library) -------------------------------------------
# The executable body is synced via a SEPARATE library, "Symphony - Documents", under
# .../Symphony - Documents/team (server.py + helpers + symphony_bus/). The installer copies
# the manifest files FROM there INTO the local TEAM_HOME (~/team) on each box. Distinct from
# the workspace/soul root above (Claude AI Assets - Documents): two libraries, two detections.
_BODY_LIBRARY_GLOB = "*Symphony - Documents*"
_BODY_MARKER = os.path.join("team", "server.py")


def _looks_like_body_source(path):
    return bool(path) and os.path.isfile(os.path.join(path, _BODY_MARKER))


def resolve_body_source():
    """Locate the synced 'Symphony - Documents' library whose team/ is the body source.
    env SYMPHONY_BODY_SOURCE (valid) -> auto-detect the library -> None (installer errors
    clearly rather than copying a wrong/empty tree)."""
    env = os.environ.get("SYMPHONY_BODY_SOURCE")
    if _looks_like_body_source(env):
        return Path(env)
    bases = [os.environ.get(v) for v in
             ("OneDriveCommercial", "OneDrive", "USERPROFILE", "HOME")]
    for base in filter(None, bases):
        candidates = (glob.glob(os.path.join(base, _BODY_LIBRARY_GLOB)) +
                      glob.glob(os.path.join(base, "*", _BODY_LIBRARY_GLOB)) +
                      glob.glob(os.path.join(base, "Library", "CloudStorage", "*", _BODY_LIBRARY_GLOB)))
        for cand in candidates:
            if _looks_like_body_source(cand):
                return Path(cand)
    return None  # not found -> installer surfaces an explicit error (no silent wrong-copy)


if __name__ == "__main__":
    root = resolve_workspace_root()
    print("TEAM_WORKSPACE_ROOT ->", root)
    print("  valid (has aria_sync):", _looks_like_workspace(str(root)))
    body = resolve_body_source()
    print("SYMPHONY_BODY_SOURCE ->", body)
    print("  valid (has team/server.py):", _looks_like_body_source(str(body)) if body else False)
