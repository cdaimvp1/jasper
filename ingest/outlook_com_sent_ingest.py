"""
outlook_com_sent_ingest.py — task #270 Phase A (2026-08-07): deterministic
Outlook Sent Items ingestion into raw_items, no LLM involved. Mirrors
outlook_com_ingest.py's run() almost line-for-line - same cursor/dedupe/
attachment/body-absorption discipline, reusing its helpers directly rather
than duplicating them (only the PowerShell script differs, and this
module's own field mapping: sent_epoch -> occurred_ts, no sender field).

Deliberately writes source="outlook_mail" (NOT a new "outlook_mail_sent"
value) and thread_key=conversation_id, exactly like the inbound ingester -
this is the load-bearing decision in the whole design (see docs/design
notes from task #270): workgraph_identity._CONTAINER_TYPE_BY_SOURCE only
maps "outlook_mail", so a sent item lands in the SAME source_containers row
its inbound counterpart already registered and cluster_and_link() attaches
it to the correct existing issue with zero new matching code. A distinct
source string would silently break this - _container_identity() would
return None for every sent item and every one would fall through to
creating a brand-new cluster instead.

meta_json='{"confirmed_direction":"outbound"}' is written on every row -
see workgraph_classify.classify_item's confirmed_direction parameter (task
#270 Phase B) for why: this ingester KNOWS the direction structurally (it
came out of Outlook's own Sent Items folder), so the OUTBOUND_CUE/
INBOUND_CUE keyword-guess path must not be allowed to override it with a
worse guess (an ordinary sent reply like "Sounds good, approved" contains
none of those cue words and would default to "inbound" - exactly backwards).

Usage:
    python outlook_com_sent_ingest.py [--max-items 500]

Runnable standalone (Windows Task Scheduler, alongside outlook_com_ingest.py)
or from scheduled_refresh.py - no worker/LLM dependency either way.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workgraph_store as ws
import paths
import config
from outlook_com_ingest import _dedupe_key, _absorb_attachments, _absorb_body, _parse_body_capture_failures

_SCRIPT = Path(__file__).resolve().parent / "outlook_scan_sent.ps1"
_SOURCE = "outlook_mail"
_CURSOR_KEY = "folder:Sent Items"


def run(max_items: int = 500, timeout: int = 120, sync_wait_seconds: int = 0) -> dict:
    """Same timeout/sync_wait_seconds shape as outlook_com_ingest.run() - see
    its own docstring for why (a large backfill pull needs a raised timeout
    with salvage-on-expiry, and Cached Exchange Mode needs a real wall-clock
    wait to catch up on a cold-started Outlook)."""
    since_epoch = ws.get_cursor(_SOURCE, _CURSOR_KEY) or "0"
    paths.ATTACHMENT_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        "powershell", "-NoProfile", "-File", str(_SCRIPT),
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
    had_error = proc.returncode not in (0,)

    body_capture_failures = _parse_body_capture_failures(proc.stderr)
    if body_capture_failures:
        total = int(ws.get_cursor(_SOURCE, "sent_body_capture_failures_total") or "0")
        ws.set_cursor(_SOURCE, "sent_body_capture_failures_total", str(total + len(body_capture_failures)))
        ws.set_cursor(_SOURCE, "last_sent_body_capture_failure", body_capture_failures[0])

    inserted = 0
    duplicates = 0
    attachments_absorbed = 0
    max_seen_ts = float(since_epoch)
    marc_identity = config.get("manager", "id") or "marc"

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue  # malformed line - skip, don't fail the whole batch

        occurred_ts = float(item.get("sent_epoch") or 0)
        if occurred_ts > max_seen_ts:
            max_seen_ts = occurred_ts

        participants = item.get("participants") or []
        stable_key = item.get("conversation_id") or item.get("entry_id") or ""
        source_ref = item.get("entry_id") or stable_key
        dedupe_key = _dedupe_key(occurred_ts, participants, source_ref)

        row_id = ws.insert_raw_item(
            source=_SOURCE,
            stable_key=stable_key,
            thread_key=stable_key,  # same conversation_id-as-thread-key convention as inbound mail
            dedupe_key=dedupe_key,
            occurred_ts=occurred_ts,
            subject=item.get("subject"),
            from_actor=marc_identity,  # structural fact, not read off the item - Marc is always the sender here
            participants_json=json.dumps(participants, ensure_ascii=False),
            body_preview=item.get("body_preview"),
            entry_id=item.get("entry_id"),
            meta_json=json.dumps({"confirmed_direction": "outbound"}),
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

    # Advance the cursor only past what we actually saw, and only forward -
    # never regress it even if this batch happened to be empty.
    if max_seen_ts > float(since_epoch):
        ws.set_cursor(_SOURCE, _CURSOR_KEY, str(max_seen_ts))

    result = {"ok": not had_error, "inserted": inserted, "duplicates": duplicates,
              "attachments_absorbed": attachments_absorbed, "cursor": max_seen_ts,
              "body_capture_failures": body_capture_failures}
    if had_error:
        result["error"] = proc.stderr.strip() or f"exit code {proc.returncode}"
    return result


def backfill(days_back: int = 90, max_items: int = 3000, timeout: int = 600, sync_wait_seconds: int = 60) -> dict:
    """One-time historical catch-up pull (task #270 Phase C) - mirrors the
    already-proven 90-day/2,849-item inbound backfill (outlook_com_ingest.py's
    own run() docstring documents that real pull). Computes since_epoch
    directly from days_back rather than reading the live cursor, and
    deliberately does NOT advance the folder:{_CURSOR_KEY} cursor afterward -
    a manual catch-up pull must never fast-forward the live daily cursor past
    mail the live run() hasn't seen yet. Safe to re-run: insert_raw_item's
    dedupe_key protects against re-inserting anything this pull (or a prior
    run() call) already has."""
    import time
    since_epoch = time.time() - (days_back * 86400)
    paths.ATTACHMENT_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        "powershell", "-NoProfile", "-File", str(_SCRIPT),
        "-SinceEpoch", str(since_epoch),
        "-MaxItems", str(max_items),
        "-StagingDir", str(paths.ATTACHMENT_STAGING_DIR),
        "-SyncWaitSeconds", str(sync_wait_seconds),
    ]
    try:
        proc = subprocess.run(args, capture_output=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        proc = subprocess.CompletedProcess(
            args, returncode=1, stdout=e.stdout or "",
            stderr=(e.stderr or "") + f"\nJASPER_DIAG: subprocess timed out after {timeout}s",
        )
    had_error = proc.returncode not in (0,)

    inserted = 0
    duplicates = 0
    attachments_absorbed = 0
    marc_identity = config.get("manager", "id") or "marc"

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue

        occurred_ts = float(item.get("sent_epoch") or 0)
        participants = item.get("participants") or []
        stable_key = item.get("conversation_id") or item.get("entry_id") or ""
        source_ref = item.get("entry_id") or stable_key
        dedupe_key = _dedupe_key(occurred_ts, participants, source_ref)

        row_id = ws.insert_raw_item(
            source=_SOURCE, stable_key=stable_key, thread_key=stable_key,
            dedupe_key=dedupe_key, occurred_ts=occurred_ts,
            subject=item.get("subject"), from_actor=marc_identity,
            participants_json=json.dumps(participants, ensure_ascii=False),
            body_preview=item.get("body_preview"), entry_id=item.get("entry_id"),
            meta_json=json.dumps({"confirmed_direction": "outbound"}),
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

    result = {"ok": not had_error, "inserted": inserted, "duplicates": duplicates,
              "attachments_absorbed": attachments_absorbed}
    if had_error:
        result["error"] = proc.stderr.strip() or f"exit code {proc.returncode}"
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-items", type=int, default=500)
    parser.add_argument("--backfill-days", type=int, default=None,
                         help="if set, runs a one-time historical backfill instead of the live cursor scan")
    args = parser.parse_args()
    if args.backfill_days is not None:
        print(json.dumps(backfill(days_back=args.backfill_days, max_items=args.max_items)))
    else:
        print(json.dumps(run(max_items=args.max_items)))
