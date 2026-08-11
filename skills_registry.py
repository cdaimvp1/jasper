"""
skills_registry.py — swappable action_kind -> installed-skill mapping,
with real versioning and fallback copies (2026-08-01).

Domain intelligence (which real Claude Skill answers a given action_kind, e.g.
"contract_review") lives ONLY here, in config/skills_registry.json - never
hardcoded into workgraph_recommend.py's Python or into a worker's routine doc
prose. Replacing Jasper's domain (procurement -> something else) means editing
this one file; no code change and no routine-doc rewrite required.

Each entry: {skill_name, skill_dir (relative to TEAM_DATA_DIR), label,
produces, output_kind, version, installed_at, previous_versions}. A worker
(see ingest/ACTION_BRIDGE_ROUTINE.md) looks up its action_kind here, reads
the resolved skill_dir's own SKILL.md, and follows it - this module never
inspects or runs a skill itself, it only resolves "which one, if any" and
hands back a real, existing filesystem path. Zero LLM calls, zero
fabrication: an action_kind with no registry entry returns None, and every
caller falls back to today's generic behavior.

Typed capability fields (task #320, 2026-08-11): entries also carry OPTIONAL
purpose, applies_to_work_types, required_inputs, optional_inputs,
evidence_requirements, preconditions, allowed_systems, permissions_required,
reversible, auto_run_eligible, review_required, cost_class, and
terminal_states - see install_skill()'s docstring for defaults. These were
backfilled by hand in config/skills_registry.json from each skill's own
SKILL.md; a field left blank/None there means it genuinely wasn't stated in
the skill's own docs, not that nobody looked.

Versioning/fallback (added 2026-08-01): install_skill() vendors a skill into
DATA_DIR/documents/reference/skills/<skill_name>/<version>/ - a version gets
its OWN directory, never overwritten in place. Updating an action_kind to a
newer version pushes the prior version's pointer onto previous_versions
(files untouched on disk) rather than deleting it, so rollback_skill() always
has something real to restore. Only versions pushed beyond
MAX_PREVIOUS_VERSIONS get their files pruned - a fallback nobody can roll
back to any more is disk waste, not a real safety net.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

import paths


def _long_path(p: Path) -> str:
    """Windows MAX_PATH (260-char) workaround for shutil operations: prefix
    with \\\\?\\ so a long skill name + deep template subfolder can't silently
    fail a copy regardless of whether this machine's long-paths policy is
    enabled. No-op on non-Windows."""
    s = str(Path(p).resolve())
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    return s

REGISTRY_PATH = paths.CONFIG_DIR / "skills_registry.json"
SKILLS_ROOT = paths.DOCUMENTS_REFERENCE_DIR / "skills"
MAX_PREVIOUS_VERSIONS = 3


def _load() -> dict:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(registry: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")


def get_skill_for_action(action_kind: str) -> Optional[dict]:
    """Returns {skill_name, skill_dir (absolute Path), label, produces,
    output_kind, version, installed_at, previous_versions} for `action_kind`,
    or None if nothing is registered (the normal, honest case for most action
    kinds - not an error). `skill_dir` is resolved to an absolute path here so
    every caller gets something directly usable, never a relative path they'd
    have to re-resolve."""
    entry = _load().get(action_kind)
    if not entry:
        return None
    resolved = dict(entry)
    resolved["skill_dir"] = paths.DATA_DIR / entry["skill_dir"]
    if not resolved["skill_dir"].exists():
        # Registered but not actually vendored on disk - an honest miss,
        # never a guess at what the skill would have said.
        return None
    return resolved


def install_skill(action_kind: str, source_dir: Path, *, skill_name: str, display_name: str,
                   label: str, produces: str, output_kind: str, version: str,
                   purpose: Optional[str] = None,
                   applies_to_work_types: Optional[list] = None,
                   required_inputs: Optional[list] = None,
                   optional_inputs: Optional[list] = None,
                   evidence_requirements: Optional[list] = None,
                   preconditions: Optional[list] = None,
                   allowed_systems: Optional[list] = None,
                   permissions_required: Optional[list] = None,
                   reversible: Optional[bool] = None,
                   auto_run_eligible: Optional[bool] = None,
                   review_required: Optional[bool] = None,
                   cost_class: Optional[str] = None,
                   terminal_states: Optional[list] = None) -> dict:
    """Vendors a real skill folder (already extracted from its .skill
    package) into DATA_DIR/documents/reference/skills/<skill_name>/<version>/
    and registers it under action_kind. If action_kind already points at a
    DIFFERENT version, the OLD version is left on disk untouched and its
    pointer is pushed onto previous_versions - never deleted, never
    overwritten in place, so an update always leaves a real fallback copy
    right next to the new one. Re-installing the SAME version in place
    (identical version string) is treated as a clean replace, not an update -
    no new fallback entry, since there's nothing genuinely prior to fall back
    to.

    Typed capability fields (task #320, Marc's engineering-direction doc
    Section 10 "Evolve Skills into typed capabities") - ALL optional, ALL
    additive: every existing caller that omits them keeps working exactly as
    before. Each is a directly-observable property of the skill (what work it
    applies to, what it needs, what it touches, whether it's reversible/
    auto-runnable/review-gated, its rough cost class, its terminal states) -
    never fabricated by this module; callers that don't know a value should
    pass None/omit it rather than invent one. List fields default to [] (not
    None) so callers can safely iterate them without a None-check; bool/str
    fields default to None (an honest "unknown") except terminal_states,
    which defaults to the generic ["succeeded", "failed"] run-outcome model
    every skill shares - that default is a property of THIS system's workflow
    model, not a fact asserted about the skill itself, so it's not a
    fabrication the way a guessed precondition or permission would be."""
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"skill source not found: {source_dir}")

    dest_rel = Path("documents") / "reference" / "skills" / skill_name / version
    dest_abs = paths.DATA_DIR / dest_rel
    dest_abs.parent.mkdir(parents=True, exist_ok=True)
    if dest_abs.exists():
        shutil.rmtree(_long_path(dest_abs))
    shutil.copytree(_long_path(source_dir), _long_path(dest_abs))

    registry = _load()
    old = registry.get(action_kind)
    previous_versions = old.get("previous_versions", []) if old else []
    if old and old.get("version") != version:
        previous_versions = [{
            "version": old.get("version"), "skill_dir": old.get("skill_dir"),
            "installed_at": old.get("installed_at"),
        }] + previous_versions
    # Prune fallback files beyond the cap - the pointer AND the files, since
    # a version nothing can roll back to any more is just disk waste.
    keep, prune = previous_versions[:MAX_PREVIOUS_VERSIONS], previous_versions[MAX_PREVIOUS_VERSIONS:]
    for stale in prune:
        stale_dir = stale.get("skill_dir")
        if stale_dir:
            shutil.rmtree(_long_path(paths.DATA_DIR / stale_dir), ignore_errors=True)

    registry[action_kind] = {
        "skill_name": skill_name, "skill_dir": dest_rel.as_posix(), "display_name": display_name,
        "label": label, "produces": produces, "output_kind": output_kind,
        "version": version, "installed_at": time.time(), "previous_versions": keep,
        "purpose": purpose,
        "applies_to_work_types": applies_to_work_types or [],
        "required_inputs": required_inputs or [],
        "optional_inputs": optional_inputs or [],
        "evidence_requirements": evidence_requirements or [],
        "preconditions": preconditions or [],
        "allowed_systems": allowed_systems or [],
        "permissions_required": permissions_required or [],
        "reversible": reversible,
        "auto_run_eligible": auto_run_eligible,
        "review_required": review_required,
        "cost_class": cost_class,
        "terminal_states": terminal_states if terminal_states is not None else ["succeeded", "failed"],
    }
    _save(registry)
    return registry[action_kind]


def list_all() -> dict:
    """Every registered action_kind -> its resolved entry (skill_dir as an
    absolute Path, same shape as get_skill_for_action), skipping any entry
    whose skill_dir doesn't actually exist on disk - same honest-miss rule
    as get_skill_for_action, just applied across the whole registry at
    once. Used by the UI (task #112) to offer Marc every real, runnable
    skill as a pickable action, not just the handful with a dedicated
    button - so a skill someone installs later is usable immediately, with
    no code change on either side of the API."""
    return {action_kind: resolved for action_kind in _load()
            if (resolved := get_skill_for_action(action_kind)) is not None}


def rollback_skill(action_kind: str) -> Optional[dict]:
    """Restores the most recent previous_versions entry as the active one for
    action_kind. Returns the restored entry, or None if action_kind has no
    fallback to roll back to. The version being rolled BACK FROM is pushed
    onto previous_versions in its place (never deleted), so a rollback is
    itself reversible with another rollback."""
    registry = _load()
    entry = registry.get(action_kind)
    if not entry or not entry.get("previous_versions"):
        return None
    prev, remaining = entry["previous_versions"][0], entry["previous_versions"][1:]
    demoted = {"version": entry.get("version"), "skill_dir": entry.get("skill_dir"),
               "installed_at": entry.get("installed_at")}
    registry[action_kind] = {
        **entry, "version": prev["version"], "skill_dir": prev["skill_dir"],
        "installed_at": prev["installed_at"], "previous_versions": [demoted] + remaining,
    }
    _save(registry)
    return registry[action_kind]
