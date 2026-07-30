"""
outlook_actions.py — real, on-demand Outlook COM actions triggered from the
UI (task #46: open the exact email; task #47 will add a real draft-reply
here too). Distinct from ingest/outlook_com_ingest.py, which is scheduled,
read-only, BULK ingestion - this is single-item, user-triggered, and has no
FastAPI dependency so it's directly unit-testable (server_lean.py's endpoint
is a thin wrapper that must call this via asyncio.to_thread - subprocess.run
blocks, and this app runs a single uvicorn worker with no --workers flag, the
exact whole-server-freeze mistake already found and fixed once this session
for /api/cockpit/refresh, task #42).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent / "ingest"
_OPEN_ITEM_SCRIPT = _SCRIPT_DIR / "outlook_open_item.ps1"


def open_email(entry_id: str) -> dict:
    """Opens the exact Outlook item (by EntryID) in a real Outlook reading
    window via COM's Display() - read+display only, never sends or modifies
    anything about the item. Raises RuntimeError (with the script's stderr)
    on failure - a bad/stale EntryID, Outlook not running, etc. - which the
    caller translates into an HTTP error."""
    if not entry_id:
        raise ValueError("entry_id is required")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(_OPEN_ITEM_SCRIPT), "-EntryID", entry_id],
        capture_output=True, encoding="utf-8", timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"exit code {proc.returncode}")
    return {"ok": True}
