"""
outlook_com_ingest.py — deterministic Outlook mail ingestion, no LLM involved.

Invokes ingest/outlook_scan.ps1 (Outlook COM automation) to pull mail from the
"Careful" folder (Marc's real effective inbox — his auto-file rule routes all
real inbound there; classic Inbox is empty, confirmed this session) since the
last run's high-water mark, and writes one raw_items row per message via
workgraph_store.

Incrementality: a persisted high-water mark on ReceivedTime (ingest_cursors,
source='outlook_mail', cursor_key='careful_folder'), not EntryID (EntryIDs
aren't reliably monotonic). A second, independent dedup layer — a content-hash
dedupe_key modeled on the reference supplier-communication-log.service.ts's
commsDedupeKey (sha256 of date|sorted-participants|sourceRef) — protects
against a re-scanned item slipping through if the cursor ever gets replayed.

Usage:
    python outlook_com_ingest.py [--folder "Careful"] [--max-items 500]

Runnable standalone (Windows Task Scheduler) or synchronously from the
cockpit's "Refresh" route — no worker/LLM dependency either way.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workgraph_store as ws
import paths

_SCRIPT = Path(__file__).resolve().parent / "outlook_scan.ps1"
_SOURCE = "outlook_mail"


def _dedupe_key(occurred_ts: float, participants: list[str], source_ref: str) -> str:
    """sha256(date|sorted-participants|sourceRef), truncated - same shape as the
    reference commsDedupeKey. Date-granular (not full timestamp) so the same
    thread scanned twice on the same day still collapses to one key even if
    ReceivedTime formatting differs slightly between runs."""
    day = time.strftime("%Y-%m-%d", time.gmtime(occurred_ts))
    parties = ",".join(sorted(p.strip().lower() for p in participants if p and p.strip()))
    ref = (source_ref or "").strip().lower()
    digest = hashlib.sha256(f"{day}|{parties}|{ref}".encode("utf-8")).hexdigest()
    return digest[:16]


def _absorb_attachments(row_id: int, staged: list[dict]) -> int:
    """Move each staged file (saved by outlook_scan.ps1 via Outlook COM) into
    the real document library under this raw_item's id, and register one
    attachments row per file. Returns how many were absorbed."""
    if not staged:
        return 0
    dest_dir = paths.DOCUMENTS_RAW_ITEMS_DIR / str(row_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    absorbed = 0
    for att in staged:
        src = Path(att.get("staged_path") or "")
        if not src.is_file():
            continue  # PowerShell reported it but it's not actually there - skip, don't fail the batch
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        dest = dest_dir / f"{digest[:16]}_{src.name}"
        shutil.move(str(src), str(dest))
        ws.create_attachment(
            entity_type="raw_item", entity_id=str(row_id), kind="reference",
            filename=att.get("filename") or src.name,
            stored_path=str(dest.relative_to(paths.DOCUMENTS_DIR)),
            content_type=None, size_bytes=att.get("size_bytes") or dest.stat().st_size,
            sha256_hex=digest, uploaded_by="outlook_ingest",
        )
        absorbed += 1
    return absorbed


def run(folder: str = "Careful", max_items: int = 500) -> dict:
    since_epoch = ws.get_cursor(_SOURCE, f"folder:{folder}") or "0"
    paths.ATTACHMENT_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            "powershell", "-NoProfile", "-File", str(_SCRIPT),
            "-FolderName", folder,
            "-SinceEpoch", str(since_epoch),
            "-MaxItems", str(max_items),
            "-StagingDir", str(paths.ATTACHMENT_STAGING_DIR),
        ],
        capture_output=True, encoding="utf-8", timeout=120,
    )
    # Fixed 2026-07-29: this used to return immediately on any non-zero exit,
    # discarding every already-valid JSON line PowerShell had already printed
    # for items processed before whatever failed - one bad email (or, after
    # this session's outlook_scan.ps1 fix, a catastrophic mid-scan COM
    # failure) could silently lose an otherwise-good batch. Now the valid
    # lines are always salvaged and inserted; a non-zero exit is reported
    # alongside the salvaged counts instead of in place of them.
    had_error = proc.returncode not in (0,)

    inserted = 0
    duplicates = 0
    attachments_absorbed = 0
    max_seen_ts = float(since_epoch)

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue  # malformed line - skip, don't fail the whole batch

        occurred_ts = float(item.get("received_epoch") or 0)
        if occurred_ts > max_seen_ts:
            max_seen_ts = occurred_ts

        participants = item.get("participants") or []
        stable_key = item.get("conversation_id") or item.get("entry_id") or ""
        source_ref = item.get("entry_id") or stable_key
        dedupe_key = _dedupe_key(occurred_ts, participants, source_ref)

        row_id = ws.insert_raw_item(
            source=_SOURCE,
            stable_key=stable_key,
            thread_key=stable_key,  # v1: conversationId doubles as the thread key for mail
            dedupe_key=dedupe_key,
            occurred_ts=occurred_ts,
            subject=item.get("subject"),
            from_actor=item.get("sender"),
            participants_json=json.dumps(participants, ensure_ascii=False),
            body_preview=item.get("body_preview"),
        )
        if row_id is None:
            duplicates += 1
        else:
            inserted += 1
            attachments_absorbed += _absorb_attachments(row_id, item.get("attachments") or [])

        # Whether this was a fresh insert or a duplicate, the staging folder
        # PowerShell used (if any - most messages have no real attachments) is
        # done being useful - clean it up either way so re-scans of
        # already-seen mail never accumulate orphaned staged files.
        staged_dir_str = item.get("attachments_staged_dir")
        if staged_dir_str:
            shutil.rmtree(Path(staged_dir_str), ignore_errors=True)

    # Advance the cursor only past what we actually saw, and only forward -
    # never regress it even if this batch happened to be empty.
    if max_seen_ts > float(since_epoch):
        ws.set_cursor(_SOURCE, f"folder:{folder}", str(max_seen_ts))

    result = {"ok": not had_error, "inserted": inserted, "duplicates": duplicates,
              "attachments_absorbed": attachments_absorbed, "cursor": max_seen_ts}
    if had_error:
        result["error"] = proc.stderr.strip() or f"exit code {proc.returncode}"
    return result


def sweep_unread(folder: str = "Careful", max_items: int = 200) -> dict:
    """Backlog sweep (2026-07-29, Tia) — separate from run()/the cursor above
    on purpose. run()'s cursor is forward-marching-only: it structurally
    cannot reach mail it has already passed, unread or not (see
    outlook_scan.ps1's -UnreadOnly comment for why). This asks Outlook
    directly for everything currently unread, regardless of the cursor, and
    inserts through the exact same insert_raw_item() dedup path as run() —
    an item already ingested (by either path) is a no-op duplicate here, not
    a re-insert, so this is safe to run repeatedly/on a schedule alongside
    run() without double-counting anything. Does NOT touch or advance the
    folder:{folder} cursor — entirely orthogonal to it."""
    paths.ATTACHMENT_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            "powershell", "-NoProfile", "-File", str(_SCRIPT),
            "-FolderName", folder,
            "-UnreadOnly",
            "-MaxItems", str(max_items),
            "-StagingDir", str(paths.ATTACHMENT_STAGING_DIR),
        ],
        capture_output=True, encoding="utf-8", timeout=180,
    )
    # Same fix as run() above: salvage already-valid lines instead of
    # discarding the whole batch on a non-zero exit.
    had_error = proc.returncode not in (0,)

    inserted = 0
    duplicates = 0
    attachments_absorbed = 0
    unread_seen = 0

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        unread_seen += 1

        occurred_ts = float(item.get("received_epoch") or 0)
        participants = item.get("participants") or []
        stable_key = item.get("conversation_id") or item.get("entry_id") or ""
        source_ref = item.get("entry_id") or stable_key
        dedupe_key = _dedupe_key(occurred_ts, participants, source_ref)

        row_id = ws.insert_raw_item(
            source=_SOURCE,
            stable_key=stable_key,
            thread_key=stable_key,
            dedupe_key=dedupe_key,
            occurred_ts=occurred_ts,
            subject=item.get("subject"),
            from_actor=item.get("sender"),
            participants_json=json.dumps(participants, ensure_ascii=False),
            body_preview=item.get("body_preview"),
        )
        if row_id is None:
            duplicates += 1
        else:
            inserted += 1
            attachments_absorbed += _absorb_attachments(row_id, item.get("attachments") or [])

        staged_dir_str = item.get("attachments_staged_dir")
        if staged_dir_str:
            shutil.rmtree(Path(staged_dir_str), ignore_errors=True)

    result = {"ok": not had_error, "unread_seen": unread_seen, "inserted": inserted,
              "duplicates": duplicates, "attachments_absorbed": attachments_absorbed}
    if had_error:
        result["error"] = proc.stderr.strip() or f"exit code {proc.returncode}"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default="Careful")
    parser.add_argument("--max-items", type=int, default=500)
    parser.add_argument("--sweep-unread", action="store_true",
                         help="Backlog mode: ingest everything currently unread, ignoring the cursor.")
    args = parser.parse_args()

    ws.init_workgraph()
    if args.sweep_unread:
        result = sweep_unread(folder=args.folder, max_items=args.max_items)
    else:
        result = run(folder=args.folder, max_items=args.max_items)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)
