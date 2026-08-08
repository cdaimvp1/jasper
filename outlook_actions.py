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
_DRAFT_COMPOSE_SCRIPT = _SCRIPT_DIR / "outlook_draft_compose.ps1"
_MARK_READ_SCRIPT = _SCRIPT_DIR / "outlook_mark_read.ps1"
_TIMEOUT_SECONDS = 120
# Raised from 20 (2026-08-06, Marc's direct report: these actions "take 2-3
# minutes" or appear to silently fail). Root cause: `New-Object -ComObject
# Outlook.Application` in each PowerShell script below routinely has to
# cold-start Outlook from scratch on this machine - ingest/outlook_com_
# ingest.py's own docstring already documents that Outlook does not stay
# running between uses here, and health_check.py tracks a real
# consecutive_cold_starts streak for the same reason. A cold start commonly
# takes well over 20s (profile load, Exchange/autodiscover, add-ins), so the
# old 20s timeout was killing the Python/PowerShell caller before Outlook
# finished launching - and since Outlook is DCOM-activated (not a child
# process of the PowerShell script), killing the caller does NOT kill the
# still-initializing OUTLOOK.EXE, which keeps warming up orphaned in the
# background. 120s matches the timeout ingest/outlook_com_ingest.py's own
# bulk-ingest calls already use successfully for the same cold-start reality.


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


def mark_read(entry_id: str) -> dict:
    """Task #275 - marks the exact Outlook item (by EntryID) read via COM's
    UnRead=$false + Save(). Never deletes, moves, or archives anything -
    that's the whole scope Marc asked for. Closure-triggered (a claim/
    issue/project actually closing), never on ingest - see workgraph_
    store's closure call sites for where this gets invoked. Raises
    RuntimeError (with a real reason) on failure, same contract as every
    other action here."""
    if not entry_id:
        raise ValueError("entry_id is required")
    return _run_powershell(["powershell", "-NoProfile", "-File", str(_MARK_READ_SCRIPT), "-EntryID", entry_id])


def draft_reply(entry_id: str, reply_all: bool = False, ref_tag: str | None = None,
                 body: str | None = None, save_only: bool = False) -> dict:
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
    Optional and additive: with no ref_tag, this behaves exactly as before.

    body/save_only (task #287, proactive drafting): body prepends real
    drafted text above the quoted thread, same HTMLBody-prepend mechanism
    as ref_tag. save_only calls MailItem.Save() instead of Display() - the
    draft lands in the Drafts folder without popping a visible compose
    window, which matters specifically for a PROACTIVE call (nothing
    should be surprising Marc with an open window while he's away from his
    machine). A human-triggered draft (the cockpit's own Draft Reply
    button) never passes save_only - Display() staying the default there
    is deliberate, not an oversight."""
    if not entry_id:
        raise ValueError("entry_id is required")
    args = ["powershell", "-NoProfile", "-File", str(_DRAFT_REPLY_SCRIPT), "-EntryID", entry_id]
    if reply_all:
        args.append("-ReplyAll")
    if ref_tag:
        args.extend(["-RefTag", ref_tag])
    if body:
        args.extend(["-Body", body])
    if save_only:
        args.append("-SaveOnly")
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


def compose_new(to_emails: list[str], subject: str, body: str = "",
                 attachment_paths: list[str] | None = None) -> dict:
    """Creates a REAL Outlook draft new-mail item addressed to `to_emails`
    via COM's own CreateItem(0)/Display() - task #35, replaces the interim
    client-only mailto: link the cockpit UI used while this wasn't built
    yet. Unlike draft_reply/draft_forward, there's no existing item to
    reply to (this is Marc selecting stakeholders and starting a fresh
    thread), so it's the one Outlook action here with no EntryID - the
    recipients themselves are the only real input. Never calls Send(): this
    only ever puts a draft on screen, the same as a person clicking New
    Email themselves. Raises RuntimeError (with a real reason) on failure.

    body/attachment_paths (2026-08-08 follow-on): the real answer to "can
    Jasper share a skill's output with stakeholders and ask them to
    review" without any new M365/Graph write permission - a real file
    attached to a real draft via the same local COM path already used
    throughout this module. Both optional and additive: with neither, this
    behaves exactly as before. The returned dict's own "missing_
    attachments" list (from the PowerShell script) must be checked by the
    caller - a path that doesn't resolve on this machine is reported, not
    silently dropped."""
    if not to_emails:
        raise ValueError("to_emails is required")
    to_field = ";".join(to_emails)
    args = ["powershell", "-NoProfile", "-File", str(_DRAFT_COMPOSE_SCRIPT), "-To", to_field, "-Subject", subject or ""]
    if body:
        args.extend(["-Body", body])
    if attachment_paths:
        args.extend(["-AttachmentPaths", ";".join(attachment_paths)])
    return _run_powershell(args)
