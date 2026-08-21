"""
normalize.py — deterministic drop-file consumer, no LLM involved.

Watches DATA_DIR/raw_ingest_inbox/*.json (paths.DATA_DIR, i.e. TEAM_DATA_DIR -
written by relay per GRAPH_INGEST_ROUTINE.md, which resolves the same INBOX var
rather than a hardcoded relative path, after a 2026-07-29 near-miss where a bare
"new_cohort/data/..." string in that routine resolved relative to relay's
subprocess cwd instead), extracts stable_key/thread_key per source type, and
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


def _calendar_series_key(ev: dict, eid: str, subject: str, organizer: str, start: str | None) -> tuple[str, str]:
    """2026-07-31 fix (meeting-grouping design pass): real captured Graph
    payloads (10 files, ~100+ events) confirmed seriesMasterId/recurrence are
    NEVER populated in practice - not a rare fallback, the only case that
    ever happens. Every real occurrence of a recurring series was getting
    its OWN thread_key (= its own id), so every occurrence became its own
    separate Issue - confirmed against real production data: 7 separate
    issues for one real "DROP-IN HOURS" series.

    thread_key already means "this source's series/thread identity" for
    every other source (mail's conversationId, Teams' chat_id) - fixing the
    VALUE computed here is sufficient; cluster_and_link already guarantees
    one thread_key -> one Issue forever via the existing thread_map, no
    other code needs to change.

    Deliberately excludes weekday from the synthetic key: the real
    DROP-IN HOURS series recurs Mon-Thu at the same time slot, but its
    attendee list isn't stable occurrence-to-occurrence - a weekday-
    inclusive key would fragment one real series into up to four. Returns
    (thread_key, thread_key_source) - the second value is purely for
    auditability (a heuristic key needs to be diagnosable later, not
    re-guessed)."""
    series_master_id = ev.get("seriesMasterId")
    if series_master_id:
        return f"graph:{series_master_id}", "graph_series_master_id"
    if organizer:
        hhmm = (start or "")[11:16]  # raw ISO substring, no timezone conversion
        norm_subject = ws.normalize_topic_key(subject)  # reuse existing normalizer, no second copy
        return f"synth:{organizer.lower()}|{norm_subject}|{hhmm}", "synthetic_calendar_series"
    return eid, "stable_key_fallback"  # a genuinely one-off event with no organizer stays its own series


def _process_calendar(payload: dict) -> list[dict]:
    """Real shape observed this session: a list of event objects (id, subject,
    organizer, attendees[], start{dateTime,timeZone}, end{...}, summary,
    isOrganizer, ...). thread_key: see _calendar_series_key.

    E7 (enhancement idea panel #7, 2026-08-03): live-verified against a real
    outlook_calendar_search response this session that location, isCancelled,
    webLink, showAs, importance, and recurrence are ALL already present in
    that same response for free (no extra read_resource call needed) -
    previously discarded entirely. Carried through as meta_json (see
    workgraph_store.insert_raw_item) rather than new dedicated columns, since
    these are calendar-only fields with no equivalent on the other three
    sources. .get() everywhere below (not required keys) so a drop file
    captured before GRAPH_INGEST_ROUTINE.md was updated to keep these still
    parses fine, just with an empty meta."""
    events = payload.get("events") or []
    out = []
    for ev in events:
        eid = ev.get("id") or ""
        subject = ev.get("subject") or ""
        organizer = ev.get("organizer") or ""
        attendees = ev.get("attendees") or []
        start = (ev.get("start") or {}).get("dateTime")
        occurred_ts = _parse_iso_to_epoch(start) if start else time.time()
        thread_key, thread_key_source = _calendar_series_key(ev, eid, subject, organizer, start)
        participants = [organizer] + list(attendees)
        # Task #414 (2026-08-21): a RECURRING occurrence's id is not unique to
        # the occurrence. Measured against a real 210-day COM scan: 1,302 events
        # carried only 791 distinct ids - one "HOLD" series accounted for 160
        # events under a single id, "School Drop off" 96, "School Pick up" 64.
        # Outlook COM returns the SERIES MASTER's EntryID for every occurrence
        # when IncludeRecurrences is on, so keying stable_key on it alone
        # collapses a year of a daily meeting into one identity. (The existing
        # relay-produced calendar rows show the same damage from the other
        # direction: 77 rows hold only 52 distinct stable_keys.)
        #
        # An occurrence is identified by its series PLUS its start, so that is
        # the key. Applied only when the event is actually recurring - a one-off
        # appointment's id is already unique and its key shape stays exactly as
        # before, which is what every existing calendar row and test relies on.
        # dedupe_key already includes the day and so was never the broken part;
        # this fixes IDENTITY, which is what thread_key/source_containers and
        # the (source, stable_key) index depend on.
        source_ref = f"{eid}:{start}" if (ev.get("recurrence") is not None and start) else eid
        is_organizer = ev.get("isOrganizer")
        meta = {
            k: v for k, v in {
                "location": ev.get("location"),
                "is_cancelled": ev.get("isCancelled"),
                "web_link": ev.get("webLink"),
                "show_as": ev.get("showAs"),
                "importance": ev.get("importance"),
                "is_recurring": ev.get("recurrence") is not None,
            }.items() if v is not None
        }
        # Lookahead-only enrichment (GRAPH_INGEST_ROUTINE.md's calendar step,
        # E7) - attendees_detailed is the read_resource response's own
        # attendees array verbatim ({name, address, type, responseStatus}),
        # full_body_html is its body.content verbatim. Absent entirely for
        # catch-up-window events (deliberately never enriched, cost
        # discipline) - .get() everywhere, no assumption either is present.
        attendees_detailed = ev.get("attendees_detailed")
        if attendees_detailed:
            meta["attendees_detailed"] = attendees_detailed
        full_body_html = ev.get("full_body_html")
        if full_body_html:
            agenda_text = html.unescape(_HTML_TAG_RE.sub(" ", full_body_html)).strip()
            agenda_text = re.sub(r"\s+", " ", agenda_text)
            if agenda_text:
                meta["full_agenda_text"] = agenda_text
        out.append({
            "source": "calendar",
            # source_ref, NOT eid - see the occurrence-identity note above.
            # eid alone is the series master's id for every occurrence.
            "stable_key": source_ref,
            "thread_key": thread_key,
            "thread_key_source": thread_key_source,
            "dedupe_key": _dedupe_key(occurred_ts, participants, source_ref),
            "occurred_ts": occurred_ts,
            "subject": subject,
            "from_actor": organizer,
            "participants": participants,
            "body_preview": (ev.get("summary") or "")[:500],
            # Real Graph field, previously discarded - NULL is a real,
            # legitimate "unknown" (confirmed inconsistently present across
            # capture calls), never silently defaulted to 0/False.
            "is_organizer": (1 if is_organizer is True else 0 if is_organizer is False else None),
            "meta": meta,
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

    Identity fix (D18, 2026-08-03): thread_key used to be the parent folder
    path (best-effort from webUrl) - the exact thing the redesign's own
    identity principle says a folder is NOT ("SharePoint folder URL is not
    artifact identity" - docs/design/CONFIDENCE_AND_IDENTITY_REDESIGN.md
    Section 3.1). Keying on the folder over-collapsed distinct documents
    that merely share a library into one container, likely a real
    contributor to D17's 40-raw-items-to-1-evidence-row attrition pattern
    for any future SharePoint volume. Each item is now its own container -
    thread_key is the same drive_id:item_id already used as stable_key, not
    a derived folder path. The parent folder becomes a `contains` relation
    at the identity-anchor layer once that exists (Section 3.2), never
    artifact identity itself."""
    results = payload.get("results") or []
    out = []
    for r in results:
        item_id = r.get("id") or ""
        drive_id = r.get("driveId") or ""
        modified = r.get("lastModifiedDateTime") or ""
        occurred_ts = _parse_iso_to_epoch(modified) if modified else time.time()
        source_ref = f"{drive_id}:{item_id}"
        # Task #414 (2026-08-21): webUrl used to be read and thrown away -
        # nothing but stable_key/thread_key/subject survived, so a SharePoint
        # raw_item reached classify with from_actor NULL, participants [], and
        # raw_ref/meta_json NULL. Measured live: ALL 100 unlinked SharePoint
        # items failed workgraph_classify._fyi_item_has_a_real_signal, and all
        # 100 failed for want of ANY input - that gate's three checks read a
        # reference, a sender/participant email domain, or an Ariba subject, and
        # a document row carries none of the three. It could never pass.
        #
        # The signal was in the discarded field the whole time: Marc's own
        # filing puts the counterparty in a PATH SEGMENT - collab.lilly.com/
        # sites/FY24LPSContracting/Shared Documents/General/Sodalis/..., .../
        # Electronic Documents/Litmus/..., .../IT Contracts for AI Pilots/
        # Veeva/... Persisting web_url is what makes that readable downstream.
        #
        # This does NOT reopen D18: thread_key stays drive_id:item_id and the
        # folder is still not artifact identity. The path is carried here as
        # SIGNAL only, which is precisely the demotion D18's own docstring
        # deferred ("the parent folder becomes a `contains` relation at the
        # identity-anchor layer once that exists"). Storing it is a
        # prerequisite for that layer; it is not that layer.
        meta = {}
        if r.get("webUrl"):
            meta["web_url"] = r["webUrl"]
        if drive_id:
            meta["drive_id"] = drive_id
        out.append({
            "source": "sharepoint",
            "stable_key": source_ref,
            "thread_key": source_ref,
            "dedupe_key": _dedupe_key(occurred_ts, [], source_ref),
            "occurred_ts": occurred_ts,
            "subject": r.get("name"),
            "from_actor": None,
            "participants": [],
            "body_preview": (r.get("summary") or "")[:500],
            "meta": meta or None,
        })
    return out


_PROCESSORS = {
    "calendar": _process_calendar,
    "teams_chat": _process_teams_chat,
    "sharepoint": _process_sharepoint,
}


def _claims_content_but_empty(source: str, payload: dict, items: list[dict]) -> str | None:
    """Phase 0 fix (D5, 2026-08-03): distinguishes a genuinely empty pull
    (a real `[]` - archive normally, nothing lost) from a shaped-but-empty
    stub that PROMISED content and delivered none - the real confirmed
    defect: a Teams payload like `{"count":21,"note":"Messages fetched from
    read_resource"}` with no `value`/`messages` list parsed as `ok:True,
    items:0` and archived, silently losing 21 real messages with no alert.
    Returns a reason string when this looks like that shape, else None."""
    if items:
        return None

    # --- Task #413 (2026-08-20): two holes the original D5 shapes missed, both
    # confirmed live against real archived drop files.
    #
    # Hole 1 - the promise arrives as a STRING, not a dict. Real file in
    # raw_ingest_failed: {"messages_count": 21, "messages_raw": "21 messages
    # fetched"}. The isinstance(dict) test below skips a bare string entirely.
    #
    # Hole 2 - the promise arrives under a DIFFERENT key, so the expected list
    # key is absent rather than wrong-typed. Real file archived as SUCCESS
    # today: {"source":"calendar","events_catchup_count":25,
    # "events_lookahead_count":25,"note":"Event details truncated in this
    # sample. Full implementation would include all 25 events from each
    # window."} - payload.get("events") is None, so the old `events is not
    # None` test returned clean, the file was archived to processed, and 50
    # real events were lost while the calendar cursor advanced past them.
    #
    # Generalized rule, still fully deterministic and no heuristics on prose:
    #   (a) any "<something>_count" field with a positive integer, or
    #   (b) the source's expected list key missing ENTIRELY
    # both mean "this payload asserts content it did not deliver". A genuinely
    # empty pull still emits its list key as [] (see _process_* above, all of
    # which read a named key), so (b) does not fire on real empty results.
    expected_list_key = {
        "calendar": "events", "sharepoint": "results", "teams_chat": "messages_raw",
    }.get(source)

    for key, value in payload.items():
        if key.endswith("_count") and isinstance(value, int) and value > 0:
            return (f"{key}={value} asserts content but 0 items parsed - "
                    f"payload promised data it did not deliver")

    if expected_list_key and expected_list_key not in payload:
        return (f"expected key {expected_list_key!r} is absent entirely (payload keys: "
                f"{sorted(payload.keys())}) - a real empty pull would still emit "
                f"{expected_list_key!r} as []")

    if source == "teams_chat":
        messages_raw = payload.get("messages_raw")
        if isinstance(messages_raw, str) and messages_raw.strip():
            return (f"messages_raw is a prose string ({messages_raw!r}) rather than a "
                    f"message list - a description of the data instead of the data")
        if isinstance(messages_raw, dict) and "value" not in messages_raw and "messages" not in messages_raw:
            if messages_raw.get("count") or messages_raw.get("note"):
                return (f"messages_raw claims content (count={messages_raw.get('count')!r}, "
                        f"note={messages_raw.get('note')!r}) but has no value/messages list")
        return None
    if source == "calendar":
        events = payload.get("events")
        if events is not None and not isinstance(events, list):
            return f"events is not a list ({type(events).__name__}) - shaped-but-unparseable payload"
        return None
    if source == "sharepoint":
        results = payload.get("results")
        if results is not None and not isinstance(results, list):
            return f"results is not a list ({type(results).__name__}) - shaped-but-unparseable payload"
        return None
    return None


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

    empty_reason = _claims_content_but_empty(source, payload, items)
    if empty_reason:
        return {"ok": False, "file": path.name, "source": source, "error": empty_reason}

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
            thread_key_source=item.get("thread_key_source"),
            is_organizer=item.get("is_organizer"),
            meta_json=json.dumps(item["meta"], ensure_ascii=False) if item.get("meta") else None,
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
        if not result["ok"]:
            # Phase 0 fix (D5): a dead-lettered file used to be silently
            # invisible - alerting must never itself abort the sweep (an
            # alert-write failure is not worse than the ingest failure it
            # would be reporting).
            try:
                ws.create_alert(
                    issue_id=None, kind="anomaly", severity="warn",
                    summary=f"ingest dead-letter: {result['file']} — {result.get('error')}",
                    source_ref=result["file"],
                )
            except Exception:
                pass
    return results


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, indent=2))
    failed = [r for r in out if not r["ok"]]
    sys.exit(1 if failed else 0)
