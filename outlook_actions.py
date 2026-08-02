"""
outlook_actions.py — real, on-demand Outlook COM actions triggered from the
UI (task #46: open the exact email; task #47: a real draft-reply). Distinct
from ingest/outlook_com_ingest.py, which is scheduled,
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
_DRAFT_REPLY_SCRIPT = _SCRIPT_DIR / "outlook_draft_reply.ps1"
_DRAFT_FORWARD_SCRIPT = _SCRIPT_DIR / "outlook_draft_forward.ps1"
_TIMEOUT_SECONDS = 20


def _run_powershell(args: list[str]) -> dict:
    """Shared subprocess wrapper - both callers below need the exact same
    "raise RuntimeError with a real reason, or return ok" contract. Fixed
    (adversarial review, task #61): subprocess.run's own timeout=... raises
    subprocess.TimeoutExpired, a DIFFERENT exception than the docstrings here
    promised ("Raises RuntimeError... which the caller translates into an
    HTTP error") - a genuinely slow/hung Outlook COM call (a blocked security
    prompt, a slow profile - both real, known COM failure modes) used to
    escape as an undocumented, undetailed exception instead of the honest
    500 the caller's `except RuntimeError` was built to handle."""
    try:
        proc = subprocess.run(args, capture_output=True, encoding="utf-8", timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {_TIMEOUT_SECONDS}s - Outlook may be busy or showing a prompt")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"exit code {proc.returncode}")
    return {"ok": True}


def open_email(entry_id: str) -> dict:
    """Opens the exact Outlook item (by EntryID) in a real Outlook reading
    window via COM's Display() - read+display only, never sends or modifies
    anything about the item. Raises RuntimeError (with a real reason) on
    failure - a bad/stale EntryID, Outlook not running, a timeout, etc. -
    which the caller translates into an HTTP error."""
    if not entry_id:
        raise ValueError("entry_id is required")
    return _run_powershell(["powershell", "-NoProfile", "-File", str(_OPEN_ITEM_SCRIPT), "-EntryID", entry_id])


def draft_reply(entry_id: str, reply_all: bool = False, ref_tag: str | None = None) -> dict:
    """Creates a REAL Outlook draft reply to the exact item (by EntryID) via
    COM's own Reply()/ReplyAll() - a new draft MailItem, already addressed
    and quoting the original thread - then Display()s it for review. Never
    calls Send(): this only ever puts a draft on screen, the same as a
    person clicking Reply themselves. Raises RuntimeError (with a real
    reason) on failure.

    ref_tag (task #36), when given, is prepended as a plain, quiet line at
    the top of the draft body - "Ref: JW-<issue-id>" - a real, working
    fallback matching signal if this draft comes back on a reply (see
    workgraph_signals.JASPER_REF_RE / workgraph_classify.cluster_and_link).
    Optional and additive: with no ref_tag, this behaves exactly as before."""
    if not entry_id:
        raise ValueError("entry_id is required")
    args = ["powershell", "-NoProfile", "-File", str(_DRAFT_REPLY_SCRIPT), "-EntryID", entry_id]
    if reply_all:
        args.append("-ReplyAll")
    if ref_tag:
        args.extend(["-RefTag", ref_tag])
    return _run_powershell(args)


def draft_forward(entry_id: str, ref_tag: str | None = None) -> dict:
    """Creates a REAL Outlook draft forward of the exact item (by EntryID)
    via COM's own Forward() - a new draft MailItem containing the original
    message, unaddressed - then Display()s it for review. Never calls
    Send(): this only ever puts a draft on screen, the same as a person
    clicking Forward themselves. Raises RuntimeError (with a real reason)
    on failure. ref_tag - see draft_reply's docstring above, same idea."""
    if not entry_id:
        raise ValueError("entry_id is required")
    args = ["powershell", "-NoProfile", "-File", str(_DRAFT_FORWARD_SCRIPT), "-EntryID", entry_id]
    if ref_tag:
        args.extend(["-RefTag", ref_tag])
    return _run_powershell(args)
