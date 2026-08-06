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
import attachment_extract

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


def _absorb_body(row_id: int, item_staged_dir: str | None, text_file: str | None,
                  html_file: str | None) -> str | None:
    """Move the staged full plain-text and/or HTML body files (task #43) into
    the same per-row document dir attachments use, and return a JSON string
    (for raw_items.raw_ref) pointing at whichever of the two actually landed.
    A single row can have neither (a scan predating this change, or both
    writes failed on a malformed item), one, or both - callers must not
    assume either key is present."""
    if not item_staged_dir:
        return None
    src_dir = Path(item_staged_dir)
    dest_dir = paths.DOCUMENTS_RAW_ITEMS_DIR / str(row_id)
    ref: dict[str, str] = {}
    for key, filename in (("body_text", text_file), ("body_html", html_file)):
        if not filename:
            continue
        src = src_dir / filename
        if not src.is_file():
            continue  # PS reported it but it's not actually there - skip, don't fail the item
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        shutil.move(str(src), str(dest))
        ref[key] = str(dest.relative_to(paths.DOCUMENTS_DIR))
    return json.dumps(ref, ensure_ascii=False) if ref else None


def _parse_body_capture_failures(stderr_text: str) -> list[str]:
    """Shared by run()/sweep_unread() - see Save-FullBody's own docstring
    (outlook_scan.ps1, fixed 2026-08-05) for the real gap this surfaces:
    every JASPER_DIAG: body_capture_failed line stderr emits for this
    invocation, reduced to just the reason/error text."""
    out = []
    for line in stderr_text.splitlines():
        if not line.startswith("JASPER_DIAG: body_capture_failed"):
            continue
        if "error=" in line:
            out.append(line.split("error=", 1)[1])
        elif "reason=" in line:
            out.append(line.split("reason=", 1)[1])
        else:
            out.append(line)
    return out


def backfill_docx_extracted_text() -> dict:
    """One-time backfill (enhancement idea panel #7/E6): attachment_
    extract.py just got a real .docx extractor - this re-extracts text
    for every already-stored .docx attachment that predates it (114 real
    rows found live, all with extracted_text NULL). Idempotent - only
    ever selects rows still missing extracted_text
    (list_attachments_missing_extracted_text), so a partial prior run or
    a re-run after new .docx attachments arrive only picks up what's
    left. Skips (leaves NULL) a row whose stored_path is missing on disk
    or whose real extraction comes back empty - never fabricates
    content, matching attachment_extract's own fail-open discipline."""
    candidates = ws.list_attachments_missing_extracted_text((".docx",))
    updated = 0
    skipped_missing_file = 0
    skipped_empty_extraction = 0
    for att in candidates:
        full_path = paths.DOCUMENTS_DIR / att["stored_path"]
        if not full_path.is_file():
            skipped_missing_file += 1
            continue
        text = attachment_extract.extract_text(full_path)
        if not text:
            skipped_empty_extraction += 1
            continue
        ws.update_attachment_extracted_text(att["id"], text)
        updated += 1
    return {
        "candidates_found": len(candidates), "updated": updated,
        "skipped_missing_file": skipped_missing_file, "skipped_empty_extraction": skipped_empty_extraction,
    }


def _absorb_attachments(row_id: int, staged: list[dict]) -> int:
    """Move each staged file (saved by outlook_scan.ps1 via Outlook COM) into
    the real document library under this raw_item's id, and register one
    attachments row per file. Returns how many were absorbed.

    Fixed 2026-08-01 (task #29): sha256 was always captured but never
    checked against what's already stored - the same real document
    forwarded across several emails used to get copied and text-extracted
    once per email instead of once, period. Now checks
    find_attachment_by_hash() first: a byte-identical match reuses the
    EXISTING file (no new copy) and its already-extracted text (no
    re-extraction) - only a genuinely new file gets copied and extracted."""
    if not staged:
        return 0
    dest_dir = paths.DOCUMENTS_RAW_ITEMS_DIR / str(row_id)
    absorbed = 0
    for att in staged:
        src = Path(att.get("staged_path") or "")
        if not src.is_file():
            continue  # PowerShell reported it but it's not actually there - skip, don't fail the batch
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        existing = ws.find_attachment_by_hash(digest)
        if existing:
            src.unlink(missing_ok=True)
            ws.create_attachment(
                entity_type="raw_item", entity_id=str(row_id), kind="reference",
                filename=att.get("filename") or src.name,
                stored_path=existing["stored_path"],
                content_type=existing.get("content_type"),
                size_bytes=existing.get("size_bytes") or att.get("size_bytes") or 0,
                sha256_hex=digest, uploaded_by="outlook_ingest",
                extracted_text=existing.get("extracted_text"),
            )
            absorbed += 1
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{digest[:16]}_{src.name}"
        shutil.move(str(src), str(dest))
        extracted_text = attachment_extract.extract_text(dest) or None
        ws.create_attachment(
            entity_type="raw_item", entity_id=str(row_id), kind="reference",
            filename=att.get("filename") or src.name,
            stored_path=str(dest.relative_to(paths.DOCUMENTS_DIR)),
            content_type=None, size_bytes=att.get("size_bytes") or dest.stat().st_size,
            sha256_hex=digest, uploaded_by="outlook_ingest",
            extracted_text=extracted_text,
        )
        absorbed += 1
    return absorbed


def run(folder: str = "Careful", max_items: int = 500, timeout: int = 120, sync_wait_seconds: int = 0) -> dict:
    """timeout (2026-08-05, real need found sizing a 90-day/2,849-item
    manual backfill): was hardcoded at 120s with NO except around it at
    all - unlike every other failure mode in this function (a non-zero
    exit, a malformed JSON line), a timeout on a genuinely large pull
    used to raise TimeoutExpired uncaught, losing 100% of an otherwise-
    good batch's already-printed JSON lines with zero partial credit.
    Now salvaged the same way a non-zero exit already is - subprocess.
    TimeoutExpired's own .stdout/.stderr carry whatever PowerShell had
    already written before the kill, when capture_output=True (confirmed
    in the stdlib docs, not assumed). The 120s default is unchanged for
    the normal live scheduled cadence - only a deliberate large one-off
    call needs to raise this.

    sync_wait_seconds (2026-08-05, same real need): 0 by default, no
    behavior change for the live cadence. Every real invocation of this
    script cold-starts Outlook in a fresh subprocess and it fully quits
    again once that process exits - Cached Exchange Mode never gets to
    catch up across separate calls, only within one. Pass a generous
    value (e.g. 60-90) for a deliberate one-off catch-up pull that needs
    the most recent mail an already-stale local cache hasn't synced yet -
    see outlook_scan.ps1's own SyncWaitSeconds comment for why this is a
    plain wall-clock wait, not an event callback (none exists to wait on
    from a one-shot script)."""
    since_epoch = ws.get_cursor(_SOURCE, f"folder:{folder}") or "0"
    paths.ATTACHMENT_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        "powershell", "-NoProfile", "-File", str(_SCRIPT),
        "-FolderName", folder,
        "-SinceEpoch", str(since_epoch),
        "-MaxItems", str(max_items),
        "-StagingDir", str(paths.ATTACHMENT_STAGING_DIR),
    ]
    if sync_wait_seconds > 0:
        args += ["-SyncWaitSeconds", str(sync_wait_seconds)]
    try:
        proc = subprocess.run(args, capture_output=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        proc = subprocess.CompletedProcess(
            args, returncode=1, stdout=e.stdout or "",
            stderr=(e.stderr or "") + f"\nJASPER_DIAG: subprocess timed out after {timeout}s",
        )
    # Fixed 2026-07-29: this used to return immediately on any non-zero exit,
    # discarding every already-valid JSON line PowerShell had already printed
    # for items processed before whatever failed - one bad email (or, after
    # this session's outlook_scan.ps1 fix, a catastrophic mid-scan COM
    # failure) could silently lose an otherwise-good batch. Now the valid
    # lines are always salvaged and inserted; a non-zero exit is reported
    # alongside the salvaged counts instead of in place of them.
    had_error = proc.returncode not in (0,)

    # Task #149 (stale local Outlook cache blocking ingestion): outlook_
    # scan.ps1 always emits this diagnostic to stderr, regardless of exit
    # code - checked here unconditionally (stderr used to be read only on
    # error, discarding this on every normal run). cold_started=True means
    # THIS scan's own COM connection is what launched Outlook - its local
    # Cached Exchange Mode store had no chance to sync before the scan ran,
    # so an empty/thin result from this specific run should be treated as
    # "possibly stale," not "confirmed no new mail." Persisted via the same
    # generic (source, cursor_key) store the forward cursor itself already
    # uses - deliberately reused rather than a new table for one flag.
    cold_started = None
    for line in proc.stderr.splitlines():
        if line.startswith("JASPER_DIAG: outlook_was_running="):
            cold_started = line.rsplit("=", 1)[1].strip().lower() != "true"
            break
    if cold_started is not None:
        ws.set_cursor(_SOURCE, "last_scan_outlook_cold_started", "true" if cold_started else "false")
        # Consecutive-cold-start counter (not a one-shot flag): a single
        # cold start is normal (Outlook closed overnight, first scan of the
        # day launches it) - the real signal worth Marc's attention is
        # Outlook essentially NEVER staying open between scheduled scans,
        # which health_check.py's own check reads and flags past a real
        # threshold, not on the first occurrence.
        streak = int(ws.get_cursor(_SOURCE, "consecutive_cold_starts") or "0")
        streak = streak + 1 if cold_started else 0
        ws.set_cursor(_SOURCE, "consecutive_cold_starts", str(streak))

    # Fixed 2026-08-05, real live gap: raw_items.raw_ref was NULL for every
    # single ingested item, every source - outlook_scan.ps1's Save-FullBody
    # used to swallow a failing .Body/.HTMLBody COM access (or a WriteAllText
    # failure) completely silently, so this had no way to ever surface. Now
    # parsed the same way cold_started already is (unconditionally, off
    # stderr, regardless of exit code) and persisted as a running total so
    # health_check.py's own alerting has a real, cumulative signal to read -
    # a single failure is not alarming (a real S-MIME/encrypted message
    # genuinely can throw), but this NEVER being zero across every run is
    # exactly the "the whole full-text pipeline has been silently starved
    # since it shipped" shape this fix exists to catch.
    body_capture_failures = _parse_body_capture_failures(proc.stderr)
    if body_capture_failures:
        total = int(ws.get_cursor(_SOURCE, "body_capture_failures_total") or "0")
        ws.set_cursor(_SOURCE, "body_capture_failures_total", str(total + len(body_capture_failures)))
        ws.set_cursor(_SOURCE, "last_body_capture_failure", body_capture_failures[0])

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
            entry_id=item.get("entry_id"),
        )
        if row_id is None:
            duplicates += 1
        else:
            inserted += 1
            attachments_absorbed += _absorb_attachments(row_id, item.get("attachments") or [])
            raw_ref = _absorb_body(row_id, item.get("item_staged_dir"),
                                    item.get("body_text_file"), item.get("body_html_file"))
            if raw_ref:
                ws.set_raw_item_raw_ref(row_id, raw_ref)

        # Whether this was a fresh insert or a duplicate, the staging folder
        # PowerShell used (attachments and/or the full body, task #43) is done
        # being useful - clean it up either way so re-scans of already-seen
        # mail never accumulate orphaned staged files.
        staged_dir_str = item.get("item_staged_dir")
        if staged_dir_str:
            shutil.rmtree(Path(staged_dir_str), ignore_errors=True)

    # Advance the cursor only past what we actually saw, and only forward -
    # never regress it even if this batch happened to be empty.
    if max_seen_ts > float(since_epoch):
        ws.set_cursor(_SOURCE, f"folder:{folder}", str(max_seen_ts))

    result = {"ok": not had_error, "inserted": inserted, "duplicates": duplicates,
              "attachments_absorbed": attachments_absorbed, "cursor": max_seen_ts,
              "outlook_cold_started": cold_started,
              "body_capture_failures": body_capture_failures}
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

    # Same body-capture-failure surfacing as run() - see _parse_body_
    # capture_failures' own docstring.
    body_capture_failures = _parse_body_capture_failures(proc.stderr)
    if body_capture_failures:
        total = int(ws.get_cursor(_SOURCE, "body_capture_failures_total") or "0")
        ws.set_cursor(_SOURCE, "body_capture_failures_total", str(total + len(body_capture_failures)))
        ws.set_cursor(_SOURCE, "last_body_capture_failure", body_capture_failures[0])

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
            entry_id=item.get("entry_id"),
        )
        if row_id is None:
            duplicates += 1
        else:
            inserted += 1
            attachments_absorbed += _absorb_attachments(row_id, item.get("attachments") or [])
            raw_ref = _absorb_body(row_id, item.get("item_staged_dir"),
                                    item.get("body_text_file"), item.get("body_html_file"))
            if raw_ref:
                ws.set_raw_item_raw_ref(row_id, raw_ref)

        staged_dir_str = item.get("item_staged_dir")
        if staged_dir_str:
            shutil.rmtree(Path(staged_dir_str), ignore_errors=True)

    result = {"ok": not had_error, "unread_seen": unread_seen, "inserted": inserted,
              "duplicates": duplicates, "attachments_absorbed": attachments_absorbed,
              "body_capture_failures": body_capture_failures}
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
