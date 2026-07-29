"""
paths.py — canonical filesystem paths for the substrate.

Centralized so individual modules don't hardcode locations and so v2 can
relocate any of these (e.g. for a non-ARIA deployment) by editing one file.
"""
from __future__ import annotations

import os
from pathlib import Path


def _require_env_path(var_name: str) -> Path:
    """No silent (or even warned-but-still-working) fallback - REQUIRED.
    Confirmed 2026-07-29: a soft fallback to a local default (even with a
    warning printed) still means the process runs "successfully" against a
    plausible-looking but WRONG path unless a human notices the warning in
    the middle of other output. Checked every real invocation path in this
    repo before removing the fallback entirely: the Scheduled Task, the
    server's own launch chain, and every scheduled_refresh.py subprocess
    spawn all already set this explicitly; every test script in this repo
    bypasses paths.py entirely (reassigns workgraph_store.WORKGRAPH_DB
    directly). Nothing legitimate relies on this working unset - the only
    thing that ever hit the old fallback was a one-off script run without
    the right environment, which is exactly the mistake this exists to make
    impossible now instead of merely visible."""
    val = os.environ.get(var_name)
    if not val:
        raise RuntimeError(
            f"{var_name} is not set. Refusing to guess a data/config location - "
            f"source symphony_env.ps1/.sh first (or set {var_name} explicitly) "
            f"before running anything that touches this repo's real data."
        )
    return Path(val)


# --- repo + data roots -----------------------------------------------------
HERE = Path(__file__).parent
DATA_DIR = _require_env_path("TEAM_DATA_DIR")
# CONFIG_DIR does NOT get the hard-require treatment: unlike TEAM_DATA_DIR,
# TEAM_CONFIG_DIR is never actually set anywhere in the real launch chain
# (checked symphony_env.ps1/.sh, scheduled_refresh.py - zero references), and
# there's no evidence of a second, divergent config/ copy the way body/data
# had - the real server has always run correctly on this same fallback.
# Requiring it broke the live server on restart (confirmed 2026-07-29) for
# no real safety gain - reverted to the soft default.
CONFIG_DIR = Path(os.environ.get("TEAM_CONFIG_DIR", str(HERE / "config")))

# Substrate databases — single SQLite file per concern, all WAL.
BUS_DB = DATA_DIR / "bus.db"
TASKS_DB = DATA_DIR / "tasks.db"
WORKGRAPH_DB = DATA_DIR / "workgraph.db"

# Document library: reference materials workers can read from, output
# documents workers produce, and files uploaded from the cockpit chat panel.
# attachments rows in workgraph.db point at paths relative to this root.
DOCUMENTS_DIR = DATA_DIR / "documents"
DOCUMENTS_REFERENCE_DIR = DOCUMENTS_DIR / "reference"
DOCUMENTS_ISSUES_DIR = DOCUMENTS_DIR / "issues"
DOCUMENTS_PROJECTS_DIR = DOCUMENTS_DIR / "projects"
DOCUMENTS_CHAT_DIR = DOCUMENTS_DIR / "chat"
# Email attachments land here, keyed by raw_item id — classification hasn't
# assigned an issue yet at ingest time, so they can't go straight under
# DOCUMENTS_ISSUES_DIR. workgraph_store.list_attachments() is joined through
# raw_items -> issue at read time rather than physically re-parenting files.
DOCUMENTS_RAW_ITEMS_DIR = DOCUMENTS_DIR / "raw_items"

# Outlook COM (PowerShell) saves attachments here first, keyed by EntryID (the
# one stable identifier it has before Python assigns a raw_item row id); the
# Python ingest step moves them into DOCUMENTS_RAW_ITEMS_DIR once it knows the
# real id, then removes the staging folder either way (dup or not).
ATTACHMENT_STAGING_DIR = DATA_DIR / "raw_ingest_inbox" / "_mail_attachments_staging"

# --- workspace this team operates over -------------------------------------
# For ARIA: OneDrive root where aria_sync/ and canon_doctrine/ live.
# Generalizable: any directory the substrate observes / acts within.
WORKSPACE_ROOT = Path(os.environ.get(
    "TEAM_WORKSPACE_ROOT",
    "/Users/DA37243/Library/CloudStorage/OneDrive-SharedLibraries-EliLillyandCompany/Claude AI Assets - Documents",
))

ARIA_SYNC = WORKSPACE_ROOT / "aria_sync"
CANON_DOCTRINE = WORKSPACE_ROOT / "canon_doctrine"

# --- claude code introspection ---------------------------------------------
# Live JSONL transcripts the substrate tails for activity observation.
CC_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CC_SESSIONS_ROOT = Path.home() / ".claude" / "sessions"
CC_IMAGE_CACHE = Path.home() / ".claude" / "image-cache"

# --- ensure dirs exist -----------------------------------------------------
def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for d in (DOCUMENTS_REFERENCE_DIR, DOCUMENTS_ISSUES_DIR, DOCUMENTS_PROJECTS_DIR,
              DOCUMENTS_CHAT_DIR, DOCUMENTS_RAW_ITEMS_DIR, ATTACHMENT_STAGING_DIR):
        d.mkdir(parents=True, exist_ok=True)
