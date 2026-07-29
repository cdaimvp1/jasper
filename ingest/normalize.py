"""
normalize.py — deterministic drop-file consumer, no LLM involved.

Watches new_cohort/data/raw_ingest_inbox/*.json (written by relay per
GRAPH_INGEST_ROUTINE.md), extracts stable_key/thread_key per source type, and
inserts into raw_items via workgraph_store. Archives each processed file to
raw_ingest_processed/ (never deletes — auditable, matches this codebase's
general never-silently-drop convention).

Confidence note: calendar, sharepoint, AND teams_chat parsing are all now
validated against real response shapes captured this session (the Teams path
was fixed 2026-07-28 after relay's first successful live pull surfaced two
real mismatches: sender is a plain string under `from`, not an object; body
content is a plain HTML-ish string under `bodyPreview`, not a nested
`body.content`). System-event messages (member-joined/left, `messageType !=
"message"`) are filtered out as noise, not shown as empty ghost entries.

One malformed file or item must never abort the whole sweep - every parse is
wrapped so a single bad drop file is skipped (moved to raw_ingest_failed/,
not silently lost) rather than crashing the run.

Usage:
    python normalize.py                 # process everything currently in the inbox
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workgraph_store as ws
from paths import DATA_DIR

INBOX_DIR = DATA_DIR / "raw_ingest_inbox"
PROCESSED_DIR = DATA_DIR / "raw_ingest_processed"
FAILED_DIR = DATA_DIR / "raw_ingest_failed"


def _dedupe_key(occurred_ts: float, participants: list[str], source_ref: str) -> str:
    """Same shape as outlook_com_ingest.py's _dedupe_key — one canonical
    implementation would be cleaner, but these are two independent processes
    (relay's ingestion vs. this normalizer) and duplicating a 5-line pure
    function is a smaller risk than a shared-import coupling between them."""
    day = time.strftime("%Y-%m-%d", time.gmtime(occurred_ts))
    parties = ",".join(sorted(p.strip().lower() for p in participants if p and p.strip()))
    ref = (source_ref or "").strip().lower()
    digest = hashlib.sha256(f"{day}|{parties}|{ref}".encode("utf-8")).hexdigest()
    return digest[:16]


def _parse_iso_to_epoch(iso_str: str) -> float:
    """Best-effort ISO-8601 -> epoch. Handles the 'Z' suffix and the
    fractional-seconds forms seen in both calendar and Teams timestamps."""
    if not iso_str:
        return time.time()
    s = iso_str.strip().replace("Z", "+00:00")
    try:
        import datetime
        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        return time.time()


def _process_calendar(payload: dict) -> list[dict]:
    """Real shape observed this session: a list of event objects (id, subject,
    organizer, attendees[], start{dateTime,timeZone}, end{...}, summary, ...).
    thread_key: the recurring series id when present, else the event's own id
    (a non-recurring event is its own one-item series)."""
    events = payload.get("events") or []
    out = []
    for ev in events:
        eid = ev.get("id") or ""
        subject = ev.get("subject") or ""
        organizer = ev.get("organizer") or ""
        attendees = ev.get("attendees") or []
        start = (ev.get("start") or {}).get("dateTime")
        occurred_ts = _parse_iso_to_epoch(start) if start else time.time()
        series_id = ev.get("seriesMasterId") or eid  # non-recurring events lack this field
        participants = [organizer] + list(attendees)
        source_ref = eid
        out.append({
            "source": "calendar",
            "stable_key": eid,
            "thread_key": series_id,
            "dedupe_key": _dedupe_key(occurred_ts, participants, source_ref),
            "occurred_ts": occurred_ts,
            "subject": subject,
            "from_actor": organizer,
            "participants": participants,
            "body_preview": (ev.get("summary") or "")[:500],
        })
    return out


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _process_teams_chat(payload: dict) -> list[dict]:
    """VALIDATED against a real payload 2026-07-28 (relay's first successful
    live pull). Real shape: `messages_raw.value[]`, each item's sender is a
    plain STRING under `from` (not an object), body content is a plain
    (HTML-ish) STRING under `bodyPreview` (not a nested `body.content`), and
    `messageType` is `"message"` for real chat content vs. `"unknownFutureValue"`
    for system events (member-joined/left etc., carried in `eventDetail`
    instead) - those are skipped as noise, not shown as empty ghost entries.
    chat_id + message id form the stable_key; chat_id alone is the thread_key."""
    chat_id = payload.get("chat_id") or ""
    chat_meta = payload.get("chat_meta") or {}
    members = [m.get("email") or m.get("displayName") or "" for m in (chat_meta.get("members") or [])]
    messages_raw = payload.get("messages_raw") or {}

    if isinstance(messages_raw, list):
        messages = messages_raw
    elif isinstance(messages_raw, dict):
        messages = messages_raw.get("value") or messages_raw.get("messages") or []
    else:
        messages = []

    out = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("messageType") != "message":
            continue  # system event (member joined/left, etc.) - not real correspondence
        msg_id = msg.get("id") or msg.get("messageId") or ""
        if not msg_id:
            continue  # can't form a stable key - skip this one item, not the whole file
        created = msg.get("createdDateTime") or msg.get("created") or ""
        occurred_ts = _parse_iso_to_epoch(created) if created else time.time()
        sender = ""
        from_field = msg.get("from")
        if isinstance(from_field, str):
            sender = from_field
        elif isinstance(from_field, dict):
            sender = (from_field.get("user") or {}).get("displayName") or from_field.get("email") or ""
        body_preview = msg.get("bodyPreview")
        body = msg.get("body")
        if body_preview:
            body_text = body_preview
        elif isinstance(body, dict):
            body_text = body.get("content") or ""
        elif isinstance(body, str):
            body_text = body
        else:
            body_text = ""
        body_text = html.unescape(_HTML_TAG_RE.sub(" ", body_text)).strip() if body_text else ""
        stable_key = f"{chat_id}:{msg_id}"
        out.append({
            "source": "teams_chat",
            "stable_key": stable_key,
            "thread_key": chat_id,
            "dedupe_key": _dedupe_key(occurred_ts, members + [sender], stable_key),
            "occurred_ts": occurred_ts,
            "subject": chat_meta.get("topic") or None,
            "from_actor": sender,
            "participants": members,
            "body_preview": (body_text or "")[:500],
        })
    return out


def _process_sharepoint(payload: dict) -> list[dict]:
    """Real shape observed this session: file/document search results
    (id, driveId, name, webUrl, lastModifiedDateTime, summary, ...).
    thread_key: the parent folder path (best-effort from webUrl) so items in
    the same library/folder cluster together; falls back to the drive id."""
    results = payload.get("results") or []
    out = []
    for r in results:
        item_id = r.get("id") or ""
        drive_id = r.get("driveId") or ""
        web_url = r.get("webUrl") or ""
        thread_key = web_url.rsplit("/", 1)[0] if "/" in web_url else (drive_id or item_id)
        modified = r.get("lastModifiedDateTime") or ""
        occurred_ts = _parse_iso_to_epoch(modified) if modified else time.time()
        source_ref = f"{drive_id}:{item_id}"
        out.append({
            "source": "sharepoint",
            "stable_key": source_ref,
            "thread_key": thread_key,
            "dedupe_key": _dedupe_key(occurred_ts, [], source_ref),
            "occurred_ts": occurred_ts,
            "subject": r.get("name"),
            "from_actor": None,
            "participants": [],
            "body_preview": (r.get("summary") or "")[:500],
        })
    return out


_PROCESSORS = {
    "calendar": _process_calendar,
    "teams_chat": _process_teams_chat,
    "sharepoint": _process_sharepoint,
}


def process_file(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "file": path.name, "error": f"unreadable JSON: {e}"}

    source = payload.get("source")
    processor = _PROCESSORS.get(source)
    if processor is None:
        return {"ok": False, "file": path.name, "error": f"unknown source: {source!r}"}

    try:
        items = processor(payload)
    except Exception as e:
        return {"ok": False, "file": path.name, "error": f"processor raised: {e}"}

    inserted = duplicates = 0
    for item in items:
        row_id = ws.insert_raw_item(
            source=item["source"],
            stable_key=item["stable_key"],
            thread_key=item["thread_key"],
            dedupe_key=item["dedupe_key"],
            occurred_ts=item["occurred_ts"],
            subject=item.get("subject"),
            from_actor=item.get("from_actor"),
            participants_json=json.dumps(item.get("participants") or [], ensure_ascii=False),
            body_preview=item.get("body_preview"),
        )
        if row_id is None:
            duplicates += 1
        else:
            inserted += 1

    return {"ok": True, "file": path.name, "source": source, "items": len(items),
            "inserted": inserted, "duplicates": duplicates}


def run() -> list[dict]:
    ws.init_workgraph()
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for path in sorted(INBOX_DIR.glob("*.json")):
        result = process_file(path)
        results.append(result)
        dest_dir = PROCESSED_DIR if result["ok"] else FAILED_DIR
        try:
            path.rename(dest_dir / path.name)
        except Exception:
            pass  # non-fatal - the file stays in the inbox and will be retried next sweep
    return results


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, indent=2))
    failed = [r for r in out if not r["ok"]]
    sys.exit(1 if failed else 0)
