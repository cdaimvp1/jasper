"""spaceship_guard.py — item-3 reverse-guard: the born→live WRITE-fence (Coby, v0.1 for review).

The coordinator (TO) may tailor/fix its OWN cohort's spaceship (born install), but MUST NEVER
write outside its own install-root — never live/shared dev infra (:8675/:8677), other cohorts, or
the shared bus (~/team). For BODY-ROUTED writes (compose / roster / config), this is a HARD
code-fence: the write physically REFUSES a target outside the install-root.

⚠️ HONEST SCOPE (no false-comfort — pairs §5 doctrine): this fences writes that ROUTE THROUGH IT.
It CANNOT fence a raw Bash/Edit that never calls it — an agent's own tools bypass body code. For
raw self-edits, the governance gate (propose→manager-approve-before-write) is the enforcer. Use
this at every body write-point touching config/roster/compose, and have the tailoring-helper call it.
This is the code half; it is real for its subset and does not pretend to be the whole fence.
"""
from pathlib import Path
import os


class OutsideInstallError(PermissionError):
    """Raised when a write target resolves outside the cohort's own install-root."""


def install_root(explicit=None) -> Path:
    """The cohort's OWN install-root — the dir holding body/ · workspace/ · config/ · _shared/
    (everything the TO may tailor). OUTSIDE it = live/shared/other-cohort = refused. Resolution
    (Abe-verified on the LIVE born env, pp_297977):
      explicit arg  >  $SYMPHONY_INSTALL_ROOT (if install exports it)  >  dirname($TEAM_HOME).
    ⚠️ The born env exports TEAM_HOME=<install>/body — so the install-root is its PARENT, NOT
    TEAM_HOME itself (anchoring at /body would FALSE-REFUSE legit writes to sibling config/ [the 5b
    settings.json target!], workspace/, _shared/). realpath'd so symlinks can't relocate the anchor."""
    if explicit:
        return Path(explicit).resolve()
    v = os.environ.get("SYMPHONY_INSTALL_ROOT")
    if v:
        return Path(v).resolve()
    th = os.environ.get("TEAM_HOME")
    if th:
        return Path(th).resolve().parent   # TEAM_HOME=<install>/body → parent = install-root
    # last-ditch: two levels up if deployed at <install>/body/spaceship_guard.py
    return Path(__file__).resolve().parent.parent


def assert_write_target_in_install(target, root=None) -> Path:
    """Resolve `target` (following symlinks + ..) and REFUSE if it lands outside the cohort's own
    install-root. Returns the resolved Path on success; raises OutsideInstallError otherwise.
    Fail-CLOSED: an unresolvable target/root refuses. Handles not-yet-existing targets by resolving
    the nearest existing parent (so a create-new-file write is still fenced, and .. / symlink escapes
    via the parent are caught)."""
    base = install_root(root)
    t = Path(target)
    cand = t if t.is_absolute() else (base / t)
    # Resolve the deepest existing ancestor (strict), then rejoin the non-existing tail — this
    # defeats ../ and symlink escapes even when the leaf doesn't exist yet.
    existing = cand
    tail = []
    while not existing.exists():
        tail.append(existing.name)
        parent = existing.parent
        if parent == existing:  # reached filesystem root without existing
            break
        existing = parent
    try:
        resolved_base = existing.resolve()
    except Exception as e:
        raise OutsideInstallError(f"cannot resolve write target {target!r}: {e}")
    resolved = resolved_base.joinpath(*reversed(tail)) if tail else resolved_base
    try:
        resolved.relative_to(base)
    except ValueError:
        raise OutsideInstallError(
            f"REFUSED: write target {resolved} is OUTSIDE the cohort install-root {base} "
            f"(born→live WRITE-fence — a coordinator may only tailor its OWN install)."
        )
    return resolved
