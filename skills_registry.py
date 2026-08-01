"""
skills_registry.py — swappable action_kind -> installed-skill mapping.

Domain intelligence (which real Claude Skill answers a given action_kind, e.g.
"contract_review") lives ONLY here, in config/skills_registry.json - never
hardcoded into workgraph_recommend.py's Python or into a worker's routine doc
prose. Replacing Jasper's domain (procurement -> something else) means editing
this one file; no code change and no routine-doc rewrite required.

Each entry: {skill_name, skill_dir (relative to TEAM_DATA_DIR), label,
produces, output_kind}. A worker (see ingest/ACTION_BRIDGE_ROUTINE.md) looks
up its action_kind here, reads the resolved skill_dir's own SKILL.md, and
follows it - this module never inspects or runs a skill itself, it only
resolves "which one, if any" and hands back a real, existing filesystem path.
Zero LLM calls, zero fabrication: an action_kind with no registry entry
returns None, and every caller falls back to today's generic behavior.
"""
from __future__ import annotations

import json
from typing import Optional

import paths

REGISTRY_PATH = paths.CONFIG_DIR / "skills_registry.json"


def _load() -> dict:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def get_skill_for_action(action_kind: str) -> Optional[dict]:
    """Returns {skill_name, skill_dir (absolute Path), label, produces,
    output_kind} for `action_kind`, or None if nothing is registered (the
    normal, honest case for most action kinds - not an error). `skill_dir`
    is resolved to an absolute path here so every caller gets something
    directly usable, never a relative path they'd have to re-resolve."""
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
