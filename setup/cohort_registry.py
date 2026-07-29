"""cohort_registry.py — BORN-GENERATED (cure-A(i)) at materialize-time from the composed roster.
Born-local, roster-derived: only THIS cohort, born-local paths, no dev-cohort names or live ports. Preserves the
import contract (COHORT_REGISTRY/WORKER_TO_COHORT_ID/VALID_WORKERS/cohort_for/homes_for) so
cohort_id_load_confirm + cohort_post import clean. Regenerated idempotently on every materialize."""
from __future__ import annotations
import os
from pathlib import Path

try:
    from paths import WORKSPACE_ROOT  # env-driven (TEAM_WORKSPACE_ROOT) shared root when paths.py present
except Exception:  # born-local hardening: resolve straight from the env if paths.py is not co-located
    WORKSPACE_ROOT = Path(os.environ.get("TEAM_WORKSPACE_ROOT", ".")).expanduser()

# Composed cohort (born-generated). Universal-core doctrine stack; composed cohorts carry NO deltas.
COHORT_REGISTRY = {
    'new_cohort': {
        "workers": frozenset(['bridge', 'curator', 'relay', 'tia']),
        "doctrine_dir": WORKSPACE_ROOT / "cohort_substrate" / "_shared" / "identity",
        "doctrine_files": ("SOUL.md", "HEART.md", "PRINCIPLES.md", "MEDIUM.md", "CHANGELOG.md"),
        "delta_dir": None,
        "delta_files": (),
    },
}

WORKER_TO_COHORT_ID = {w: cid for cid, info in COHORT_REGISTRY.items() for w in info["workers"]}
VALID_WORKERS = frozenset(WORKER_TO_COHORT_ID.keys())


def cohort_for(worker: str):
    """Return the cohort_id for a worker, or None if unknown."""
    return WORKER_TO_COHORT_ID.get(worker)


def homes_for(worker: str):
    """Return (doctrine_dir, doctrine_files, delta_dir, delta_files) for the worker's cohort, or None."""
    cid = WORKER_TO_COHORT_ID.get(worker)
    if cid is None:
        return None
    info = COHORT_REGISTRY[cid]
    return (info["doctrine_dir"], info["doctrine_files"], info["delta_dir"], info["delta_files"])
